import logging
from pathlib import Path
import sys
from datetime import datetime

import pandas as pd

QUANT_ROOT = Path(__file__).resolve().parents[2]
if str(QUANT_ROOT) not in sys.path:
    sys.path.append(str(QUANT_ROOT))

from core.detectors.bear_trap import BearTrapDetector
from core.position.position_manager import PositionManager
from core.position.t_trading import TTradingStrategy
from core.regime.market_regime_engine import MarketRegimeEngine
from core.risk.risk_manager import RiskManager
from core.router.strategy_router import StrategyRouter
from core.selector.stock_selector import StockSelector

logger = logging.getLogger(__name__)


class StrategyEngine:
    def __init__(self, config, data_fetcher, technical_indicators):
        self.config = config
        self.data_fetcher = data_fetcher
        self.technical = technical_indicators
        self.regime_engine = MarketRegimeEngine(config=config, data_fetcher=data_fetcher)
        self.selector = StockSelector(config=config)
        self.position_manager = PositionManager(config=config)
        self.t_trading_strategy = TTradingStrategy(self.position_manager, config=config)
        self.risk_manager = RiskManager(config=config)
        self.strategy_router = StrategyRouter(config=config, position_manager=self.position_manager)
        self.bear_trap_detector = BearTrapDetector(
            ma_period=self.config.get("regime", {}).get("ma_long", 250)
        )

    def build_selection_inputs(self, stock_list):
        logger.info("Preparing selection inputs: %s", len(stock_list))

        technical_config = self.config.get("strategy", {}).get("technical", {})
        analysis_period = max(int(technical_config.get("ma_period", 250)), 300)
        selection_inputs = []

        for _, stock in stock_list.iterrows():
            ts_code = stock["ts_code"]

            try:
                financial = self.data_fetcher.get_financial_data(ts_code)
                daily_basic = self.data_fetcher.get_daily_basic(ts_code)
                df = self.data_fetcher.get_daily_data(ts_code, period=analysis_period)

                if financial is None or daily_basic is None or df is None or df.empty or len(df) < 20:
                    continue

                if "turnover_rate" in df.columns:
                    avg_turnover = float(df["turnover_rate"].tail(30).mean())
                else:
                    avg_turnover = daily_basic.get("turnover_rate")

                selection_inputs.append(
                    {
                        "ts_code": ts_code,
                        "name": stock.get("name", ts_code),
                        "roe": financial.get("roe"),
                        "debt_ratio": financial.get("debt_to_assets"),
                        "pe": daily_basic.get("pe"),
                        "market_cap": (daily_basic.get("total_mv") or 0) / 10000,
                        "turnover_rate": daily_basic.get("turnover_rate"),
                        "avg_turnover": avg_turnover,
                        "_daily_data": df,
                    }
                )

            except Exception as e:
                logger.warning("Failed to prepare selection input for %s: %s", ts_code, e)
                continue

        logger.info("Prepared selection inputs: %s", len(selection_inputs))
        return selection_inputs

    def analyze_technical(self, stock_codes):
        logger.info("Starting technical analysis for %s stocks", len(stock_codes))

        tech_config = self.config["strategy"]["technical"]
        analyzed_stocks = []

        for stock_info in stock_codes:
            ts_code = stock_info["ts_code"]

            try:
                df = stock_info.get("_daily_data")
                if df is None:
                    df = self.data_fetcher.get_daily_data(ts_code, period=300)
                if df.empty or len(df) < tech_config["ma_period"]:
                    continue

                tech_result = self.technical.analyze_stock_technical(
                    df,
                    ma_period=tech_config["ma_period"],
                    macd_fast=tech_config["macd_fast"],
                    macd_slow=tech_config["macd_slow"],
                    macd_signal=tech_config["macd_signal"],
                )

                if tech_result is None:
                    continue

                enriched = self._enrich_technical_context(dict(stock_info), tech_result, df)
                enriched.pop("_daily_data", None)
                analyzed_stocks.append(enriched)

            except Exception as e:
                logger.warning("Technical analysis failed for %s: %s", ts_code, e)
                continue

        logger.info("Technical analysis finished: %s", len(analyzed_stocks))
        return analyzed_stocks

    def judge_market_status(self):
        try:
            decision = self.regime_engine.analyze_current_market()
            logger.info(
                "Market regime decision: final=%s auto=%s scores=%s",
                decision["final_regime"],
                decision["auto_regime"],
                decision["scores"],
            )
            return decision["final_regime"]
        except Exception as e:
            logger.error("Failed to judge market regime: %s", e)
            return "range"

    def select_candidate_pool(self, analyzed_stocks):
        selection_result = self.selector.select(analyzed_stocks)
        candidate_pool = []
        for item in selection_result["selected"]:
            stock = dict(item["data"])
            stock["selection_score"] = item["score"]
            stock["selection_passed_checks"] = item["passed_checks"]
            candidate_pool.append(stock)
        return selection_result, candidate_pool

    @staticmethod
    def _extract_selected_records(selection_result):
        return [dict(item["data"]) for item in selection_result["selected"]]

    def generate_buy_signals(self, analyzed_stocks, market_status, positions=None, portfolio_risk=None):
        logger.info("Generating trend entry signals for market status: %s", market_status)
        position_lookup = self._build_position_lookup(positions)
        candidate_limit = self.config.get("strategy", {}).get("candidate_pool_size", 10)
        entry_signals = []
        portfolio_risk = portfolio_risk or self.risk_manager.evaluate_portfolio()

        for stock in analyzed_stocks:
            signal = self.strategy_router.trend_following.generate(
                stock,
                market_status,
                current_position=position_lookup.get(stock["ts_code"]),
                portfolio_risk=portfolio_risk,
            )
            if signal is not None:
                entry_signals.append(signal)

        entry_signals.sort(key=lambda item: item["score"], reverse=True)
        logger.info("Generated trend entry signals: %s", len(entry_signals))
        return entry_signals[:candidate_limit]

    def check_positions_for_sell(self, positions, market_status="range"):
        logger.info("Checking exit signals for %s positions", len(positions))
        exit_signals = []
        risk_alerts = []

        for pos in positions:
            ts_code = pos["ts_code"]

            try:
                df = self.data_fetcher.get_daily_data(ts_code, period=100)
                if df.empty:
                    continue

                tech_result = self.technical.analyze_stock_technical(df)
                if tech_result is None:
                    continue

                current_price = tech_result["current_price"]
                buy_price = pos["buy_price"]
                profit_pct = (current_price - buy_price) / buy_price
                holding_days = self._estimate_holding_days(pos)
                volatility_pct = self._estimate_volatility_pct(df)
                signal = self._build_exit_signal(
                    pos=pos,
                    tech_result=tech_result,
                    market_status=market_status,
                    profit_pct=profit_pct,
                    holding_days=holding_days,
                    volatility_pct=volatility_pct,
                )

                if signal is not None:
                    exit_signals.append(signal)

                risk_decision = self.risk_manager.evaluate_position(
                    profit_pct=profit_pct,
                    tech_result=tech_result,
                    market_status=market_status,
                    holding_days=holding_days,
                    volatility_pct=volatility_pct,
                )
                if risk_decision.action != "HOLD":
                    risk_alerts.append(
                        {
                            "ts_code": pos["ts_code"],
                            "name": pos.get("name", ""),
                            "price": tech_result["current_price"],
                            "profit_pct": profit_pct,
                            "signal_type": risk_decision.action,
                            "action": "风险控制",
                            "strategy_name": "risk_manager",
                            "market_status": market_status,
                            "score": 90 if risk_decision.action == "SELL" else 78,
                            "reason": risk_decision.reasons[0],
                            "reasons": risk_decision.reasons,
                            "explanation": "风险规则触发: " + " + ".join(risk_decision.reasons),
                            "suggested_position_change": risk_decision.suggested_position_change,
                            "risk_flags": risk_decision.risk_flags,
                            "current_price": tech_result["current_price"],
                            "buy_price": pos.get("buy_price", 0.0),
                        }
                    )

            except Exception as e:
                logger.warning("Exit signal check failed for %s: %s", ts_code, e)
                continue

        logger.info("Generated exit signals: %s", len(exit_signals))
        return exit_signals, risk_alerts

    def generate_t_signals(self, positions, market_status="range"):
        logger.info("Checking T-trading opportunities for %s positions", len(positions))
        t_signals = []

        for pos in positions:
            ts_code = pos["ts_code"]
            try:
                df = self.data_fetcher.get_daily_data(ts_code, period=100)
                if df.empty:
                    continue

                tech_result = self.technical.analyze_stock_technical(df)
                if tech_result is None:
                    continue

                price_change_pct = float(df["close"].pct_change().iloc[-1]) if len(df) >= 2 else 0.0
                signal = self.t_trading_strategy.analyze_t_opportunity(
                    position=pos,
                    market_trend=market_status,
                    indicators={
                        **tech_result,
                        "price_change_pct": price_change_pct,
                    },
                )

                if signal:
                    signal.update(
                        {
                            "name": pos.get("name", ""),
                            "price": tech_result["current_price"],
                            "strategy_name": "t_trading",
                            "market_status": market_status,
                            "explanation": signal["reason"],
                            "risk_flags": [],
                        }
                    )
                    t_signals.append(signal)
            except Exception as e:
                logger.warning("T-trading signal check failed for %s: %s", ts_code, e)
                continue

        logger.info("Generated T-trading signals: %s", len(t_signals))
        return t_signals

    def run_daily_scan(self, positions=None):
        logger.info("=" * 50)
        logger.info("Starting daily scan")
        logger.info("=" * 50)

        market_status = self.judge_market_status()
        logger.info("Market status: %s", market_status)

        stock_list = self.data_fetcher.get_stock_list()
        if stock_list.empty:
            logger.error("Failed to get stock list")
            return None

        selection_inputs = self.build_selection_inputs(stock_list)
        fundamental_result = self.selector.select(selection_inputs, checks=("fundamental",))
        turnover_input = self._extract_selected_records(fundamental_result)
        turnover_result = self.selector.select(turnover_input, checks=("turnover",))
        technical_input = self._extract_selected_records(turnover_result)
        analyzed_stocks = self.analyze_technical(technical_input)
        selection_result, candidate_pool = self.select_candidate_pool(analyzed_stocks)

        portfolio_risk = self.risk_manager.evaluate_portfolio(
            self._estimate_portfolio_stats(positions or [])
        )

        sell_signals = []
        risk_alerts = []
        t_signals = []
        if positions:
            sell_signals, risk_alerts = self.check_positions_for_sell(positions, market_status=market_status)
            t_signals = self.generate_t_signals(positions, market_status=market_status)

        routed_signals = self.strategy_router.route_signals(
            market_status=market_status,
            candidate_pool=candidate_pool,
            positions=positions or [],
            portfolio_risk=portfolio_risk,
        )

        trade_signals = sorted(
            routed_signals + sell_signals + t_signals + risk_alerts,
            key=lambda item: item.get("score", 0),
            reverse=True,
        )

        result = {
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market_status": market_status,
            "candidate_pool": selection_result["selected"],
            "buy_signals": routed_signals,
            "sell_signals": sell_signals,
            "t_signals": t_signals,
            "risk_alerts": risk_alerts,
            "portfolio_risk": portfolio_risk.to_dict(),
            "trade_signals": trade_signals,
            "stats": {
                "total_stocks": len(stock_list),
                "fundamental_passed": len(turnover_input),
                "volume_passed": len(technical_input),
                "technical_analyzed": len(analyzed_stocks),
                "candidate_pool_count": len(candidate_pool),
                "buy_signals_count": len(routed_signals),
                "sell_signals_count": len(sell_signals),
                "t_signals_count": len(t_signals),
                "risk_alerts_count": len(risk_alerts),
                "trade_signals_count": len(trade_signals),
            },
        }

        logger.info("=" * 50)
        logger.info("Daily scan completed")
        logger.info("=" * 50)

        return result

    def _enrich_technical_context(self, stock_info, tech_result, df):
        current_price = tech_result["current_price"]
        current_ma = tech_result["ma250"]
        close_series = df["close"]
        price_change_pct = float(close_series.pct_change().iloc[-1]) if len(close_series) >= 2 else 0.0
        close_vs_ma = ((current_price - current_ma) / current_ma) if current_ma else 0.0

        working_df = pd.DataFrame({"close": close_series}).copy()
        working_df["ma_long"] = close_series.rolling(self.config.get("regime", {}).get("ma_long", 250)).mean()
        bear_trap = self.bear_trap_detector.detect(
            working_df,
            {"is_divergence": tech_result.get("divergence") == "bullish"},
        )

        stock_info.update(tech_result)
        stock_info.update(
            {
                "price_change_pct": price_change_pct,
                "close_vs_ma_long": close_vs_ma,
                "ma_long_slope": tech_result.get("ma250_slope", 0.0),
                "recent_high_20": float(close_series.tail(20).max()) if len(close_series) >= 20 else float(close_series.max()),
                "recent_low_20": float(close_series.tail(20).min()) if len(close_series) >= 20 else float(close_series.min()),
                "close_above_recent_high": bool(
                    current_price >= (
                        float(close_series.iloc[-21:-1].max()) if len(close_series) > 20 else float(close_series.max())
                    )
                )
                if len(close_series) > 1
                else False,
                "bear_trap": bear_trap.is_bear_trap,
                "bear_trap_reason": bear_trap.reason,
            }
        )
        return stock_info

    @staticmethod
    def _build_position_lookup(positions):
        positions = positions or []
        return {position["ts_code"]: position for position in positions}

    def _build_exit_signal(
        self,
        pos,
        tech_result,
        market_status,
        profit_pct,
        holding_days=None,
        volatility_pct=None,
    ):
        sell_reasons = []
        reduce_reasons = []
        risk_decision = self.risk_manager.evaluate_position(
            profit_pct=profit_pct,
            tech_result=tech_result,
            market_status=market_status,
            holding_days=holding_days,
            volatility_pct=volatility_pct,
        )

        if risk_decision.action == "SELL":
            sell_reasons.extend(risk_decision.reasons)
        elif risk_decision.action == "REDUCE":
            reduce_reasons.extend(risk_decision.reasons)

        if tech_result["divergence"] == "bearish":
            reduce_reasons.append("顶背离")

        if profit_pct > 0.20:
            reduce_reasons.append(f"盈利保护 ({profit_pct * 100:.2f}%)")

        if tech_result.get("volume_ratio", 0) > 1.8 and tech_result.get("macd_death_cross"):
            reduce_reasons.append("放量出货")

        if not tech_result["is_above_ma250"] and profit_pct > 0:
            reduce_reasons.append("跌破年线先减仓观察")

        if not sell_reasons and not reduce_reasons:
            return None

        if sell_reasons:
            signal_type = "SELL"
            action = "卖出"
            reasons = sell_reasons + [reason for reason in reduce_reasons if reason not in sell_reasons]
            suggested_position_change = -1.0
            explanation = "卖出依据: " + " + ".join(reasons)
            score = 95
        else:
            signal_type = "REDUCE"
            action = "减仓"
            reasons = reduce_reasons
            suggested_position_change = -round(max(self.position_manager.mobile_exposure_ratio(), 0.15), 4)
            explanation = "减仓依据: " + " + ".join(reasons)
            score = 80

        return {
            "ts_code": pos["ts_code"],
            "name": pos.get("name", ""),
            "buy_price": pos["buy_price"],
            "price": tech_result["current_price"],
            "current_price": tech_result["current_price"],
            "profit_pct": profit_pct,
            "signal_type": signal_type,
            "action": action,
            "strategy_name": "trend_following",
            "market_status": market_status,
            "score": score,
            "reasons": reasons,
            "reason": reasons[0],
            "explanation": explanation,
            "suggested_position_change": suggested_position_change,
            "risk_flags": risk_decision.risk_flags,
        }

    def _estimate_portfolio_stats(self, positions):
        if not positions:
            return {
                "portfolio_drawdown_pct": 0.0,
                "single_day_drawdown_pct": 0.0,
                "current_exposure_pct": 0.0,
            }

        exposure = 0.0
        drawdowns = []
        for position in positions:
            exposure += position.get(
                "position_ratio",
                position.get("exposure_pct", self.position_manager.base_exposure_ratio()),
            )
            buy_price = position.get("buy_price")
            current_price = position.get("current_price")
            if buy_price and current_price:
                pnl = (current_price - buy_price) / buy_price
                if pnl < 0:
                    drawdowns.append(abs(pnl))

        return {
            "portfolio_drawdown_pct": max(drawdowns) if drawdowns else 0.0,
            "single_day_drawdown_pct": max(drawdowns) if drawdowns else 0.0,
            "current_exposure_pct": min(exposure, 1.5),
        }

    @staticmethod
    def _estimate_holding_days(position):
        buy_date = position.get("buy_date")
        if not buy_date:
            return None

        try:
            buy_dt = datetime.fromisoformat(str(buy_date))
        except ValueError:
            return None

        return max((datetime.now() - buy_dt).days, 0)

    @staticmethod
    def _estimate_volatility_pct(df, window=20):
        if df is None or df.empty or "close" not in df.columns:
            return None

        returns = df["close"].pct_change().dropna().tail(window)
        if returns.empty:
            return None

        return float(returns.std(ddof=0) * (252 ** 0.5))
