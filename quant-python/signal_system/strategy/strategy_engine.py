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
    """策略引擎"""

    def __init__(self, config, data_fetcher, technical_indicators):
        self.config = config
        self.data_fetcher = data_fetcher
        self.technical = technical_indicators
        self.regime_engine = MarketRegimeEngine(config=config, data_fetcher=data_fetcher)
        self.selector = StockSelector(config=config)
        self.position_manager = PositionManager(config=config)
        self.t_trading_strategy = TTradingStrategy(self.position_manager, config=config)
        self.risk_manager = RiskManager(config=config)
        self.strategy_router = StrategyRouter()
        self.bear_trap_detector = BearTrapDetector(
            ma_period=self.config.get('regime', {}).get('ma_long', 250)
        )

    def filter_by_fundamental(self, stock_list):
        """
        基本面筛选 - 三高一低过滤

        Args:
            stock_list: DataFrame, 股票列表

        Returns:
            list: 通过筛选的股票代码列表
        """
        logger.info(f"开始基本面筛选，候选股票: {len(stock_list)}")

        fundamental_config = self.config['strategy']['fundamental']
        passed_stocks = []

        for _, stock in stock_list.iterrows():
            ts_code = stock['ts_code']

            try:
                financial = self.data_fetcher.get_financial_data(ts_code)
                if financial is None:
                    continue

                daily_basic = self.data_fetcher.get_daily_basic(ts_code)
                if daily_basic is None:
                    continue

                roe = financial.get('roe', 0)
                debt_ratio = financial.get('debt_to_assets', 100)
                pe = daily_basic.get('pe', 999)
                market_cap = daily_basic.get('total_mv', 0) / 10000

                if roe < fundamental_config['min_roe']:
                    continue

                if debt_ratio > fundamental_config['max_debt_ratio']:
                    continue

                if pe > fundamental_config['max_pe'] or pe < 0:
                    continue

                if market_cap < fundamental_config['min_market_cap']:
                    continue

                if market_cap > fundamental_config['max_market_cap']:
                    continue

                passed_stocks.append({
                    'ts_code': ts_code,
                    'name': stock['name'],
                    'roe': roe,
                    'debt_ratio': debt_ratio,
                    'pe': pe,
                    'market_cap': market_cap
                })

            except Exception as e:
                logger.warning(f"筛选 {ts_code} 失败: {e}")
                continue

        logger.info(f"基本面筛选完成，通过: {len(passed_stocks)}")
        return passed_stocks

    def filter_by_volume(self, stock_codes):
        """
        成交量筛选

        Args:
            stock_codes: list, 股票代码列表

        Returns:
            list: 通过筛选的股票代码
        """
        logger.info(f"开始成交量筛选，候选股票: {len(stock_codes)}")

        volume_config = self.config['strategy']['volume']
        passed_stocks = []

        for stock_info in stock_codes:
            ts_code = stock_info['ts_code']

            try:
                df = self.data_fetcher.get_daily_data(ts_code, period=30)
                if df.empty or len(df) < 20:
                    continue

                if 'turnover_rate' in df.columns:
                    avg_turnover = df['turnover_rate'].tail(30).mean()
                else:
                    daily_basic = self.data_fetcher.get_daily_basic(ts_code)
                    if daily_basic is None:
                        continue
                    avg_turnover = daily_basic.get('turnover_rate', 0)

                if avg_turnover < volume_config['min_turnover_rate']:
                    continue

                if avg_turnover > volume_config['max_turnover_rate']:
                    continue

                stock_info['avg_turnover'] = avg_turnover
                passed_stocks.append(stock_info)

            except Exception as e:
                logger.warning(f"成交量筛选 {ts_code} 失败: {e}")
                continue

        logger.info(f"成交量筛选完成，通过: {len(passed_stocks)}")
        return passed_stocks

    def analyze_technical(self, stock_codes):
        """
        技术面分析

        Args:
            stock_codes: list, 股票信息列表

        Returns:
            list: 带技术分析结果的股票列表
        """
        logger.info(f"开始技术面分析，候选股票: {len(stock_codes)}")

        tech_config = self.config['strategy']['technical']
        analyzed_stocks = []

        for stock_info in stock_codes:
            ts_code = stock_info['ts_code']

            try:
                df = self.data_fetcher.get_daily_data(ts_code, period=300)
                if df.empty or len(df) < tech_config['ma_period']:
                    continue

                tech_result = self.technical.analyze_stock_technical(
                    df,
                    ma_period=tech_config['ma_period'],
                    macd_fast=tech_config['macd_fast'],
                    macd_slow=tech_config['macd_slow'],
                    macd_signal=tech_config['macd_signal']
                )

                if tech_result is None:
                    continue

                enriched = self._enrich_technical_context(dict(stock_info), tech_result, df)
                analyzed_stocks.append(enriched)

            except Exception as e:
                logger.warning(f"技术分析 {ts_code} 失败: {e}")
                continue

        logger.info(f"技术面分析完成: {len(analyzed_stocks)}")
        return analyzed_stocks

    def judge_market_status(self):
        """
        判断市场环境

        Returns:
            str: 'bull' (牛市), 'range' (震荡), 'bear' (熊市)
        """
        try:
            decision = self.regime_engine.analyze_current_market()
            logger.info(
                "市场状态判断: final=%s auto=%s scores=%s",
                decision['final_regime'],
                decision['auto_regime'],
                decision['scores'],
            )
            return decision['final_regime']

        except Exception as e:
            logger.error(f"判断市场环境失败: {e}")
            return 'range'

    def select_candidate_pool(self, analyzed_stocks):
        """按统一选股器规则输出候选池和过滤原因。"""
        selection_result = self.selector.select(analyzed_stocks)
        candidate_pool = []
        for item in selection_result['selected']:
            stock = dict(item['data'])
            stock['selection_score'] = item['score']
            stock['selection_passed_checks'] = item['passed_checks']
            candidate_pool.append(stock)
        return selection_result, candidate_pool

    def generate_buy_signals(self, analyzed_stocks, market_status, positions=None, portfolio_risk=None):
        """
        生成趋势入场信号

        Args:
            analyzed_stocks: list, 技术分析后的股票列表
            market_status: str, 市场状态
            positions: list, 当前持仓

        Returns:
            list: 买入或加仓信号列表
        """
        logger.info(f"生成趋势入场信号，市场状态: {market_status}")
        position_lookup = self._build_position_lookup(positions)
        candidate_limit = self.config.get('strategy', {}).get('candidate_pool_size', 10)
        entry_signals = []
        portfolio_risk = portfolio_risk or self.risk_manager.evaluate_portfolio()

        for stock in analyzed_stocks:
            signal = self._build_entry_signal(
                stock,
                market_status,
                position_lookup.get(stock['ts_code']),
                portfolio_risk=portfolio_risk,
            )
            if signal is not None:
                entry_signals.append(signal)

        entry_signals.sort(key=lambda item: item['score'], reverse=True)
        logger.info(f"生成趋势入场信号: {len(entry_signals)} 个")
        return entry_signals[:candidate_limit]

    def check_positions_for_sell(self, positions, market_status='range'):
        """
        检查持仓是否需要卖出或减仓

        Args:
            positions: list, 持仓列表 [{ts_code, buy_price, buy_date}]
            market_status: str, 市场状态

        Returns:
            list: 卖出或减仓信号列表
        """
        logger.info(f"检查持仓趋势退出信号，持仓数: {len(positions)}")
        exit_signals = []
        risk_alerts = []

        for pos in positions:
            ts_code = pos['ts_code']

            try:
                df = self.data_fetcher.get_daily_data(ts_code, period=100)
                if df.empty:
                    continue

                tech_result = self.technical.analyze_stock_technical(df)
                if tech_result is None:
                    continue

                current_price = tech_result['current_price']
                buy_price = pos['buy_price']
                profit_pct = (current_price - buy_price) / buy_price
                signal = self._build_exit_signal(
                    pos=pos,
                    tech_result=tech_result,
                    market_status=market_status,
                    profit_pct=profit_pct,
                )

                if signal is not None:
                    exit_signals.append(signal)

                risk_decision = self.risk_manager.evaluate_position(
                    profit_pct=profit_pct,
                    tech_result=tech_result,
                    market_status=market_status,
                )
                if risk_decision.action != 'HOLD':
                    risk_alerts.append({
                        'ts_code': pos['ts_code'],
                        'name': pos.get('name', ''),
                        'price': tech_result['current_price'],
                        'profit_pct': profit_pct,
                        'signal_type': risk_decision.action,
                        'action': '风险控制',
                        'strategy_name': 'risk_manager',
                        'market_status': market_status,
                        'score': 90 if risk_decision.action == 'SELL' else 78,
                        'reason': risk_decision.reasons[0],
                        'reasons': risk_decision.reasons,
                        'explanation': "风险规则触发: " + " + ".join(risk_decision.reasons),
                        'suggested_position_change': risk_decision.suggested_position_change,
                        'risk_flags': risk_decision.risk_flags,
                        'current_price': tech_result['current_price'],
                        'buy_price': pos.get('buy_price', 0.0),
                    })

            except Exception as e:
                logger.warning(f"检查 {ts_code} 趋势退出信号失败: {e}")
                continue

        logger.info(f"生成趋势退出信号: {len(exit_signals)} 个")
        return exit_signals, risk_alerts

    def generate_t_signals(self, positions, market_status='range'):
        """生成做T信号。"""
        logger.info(f"检查做T机会，持仓数: {len(positions)}")
        t_signals = []

        for pos in positions:
            ts_code = pos['ts_code']
            try:
                df = self.data_fetcher.get_daily_data(ts_code, period=100)
                if df.empty:
                    continue

                tech_result = self.technical.analyze_stock_technical(df)
                if tech_result is None:
                    continue

                price_change_pct = float(df['close'].pct_change().iloc[-1]) if len(df) >= 2 else 0.0
                signal = self.t_trading_strategy.analyze_t_opportunity(
                    position=pos,
                    market_trend=market_status,
                    indicators={
                        **tech_result,
                        'price_change_pct': price_change_pct,
                    },
                )

                if signal:
                    signal.update({
                        'name': pos.get('name', ''),
                        'price': tech_result['current_price'],
                        'strategy_name': 't_trading',
                        'market_status': market_status,
                        'explanation': signal['reason'],
                        'risk_flags': [],
                    })
                    t_signals.append(signal)
            except Exception as e:
                logger.warning(f"检查 {ts_code} 做T信号失败: {e}")
                continue

        logger.info(f"生成做T信号: {len(t_signals)} 个")
        return t_signals

    def run_daily_scan(self, positions=None):
        """
        执行每日扫描

        Args:
            positions: list, 当前持仓

        Returns:
            dict: 扫描结果
        """
        logger.info("=" * 50)
        logger.info("开始每日扫描")
        logger.info("=" * 50)

        market_status = self.judge_market_status()
        logger.info(f"市场状态: {market_status}")

        stock_list = self.data_fetcher.get_stock_list()
        if stock_list.empty:
            logger.error("获取股票列表失败")
            return None

        fundamental_passed = self.filter_by_fundamental(stock_list)
        volume_passed = self.filter_by_volume(fundamental_passed)
        analyzed_stocks = self.analyze_technical(volume_passed)
        selection_result, candidate_pool = self.select_candidate_pool(analyzed_stocks)

        portfolio_risk = self.risk_manager.evaluate_portfolio(
            self._estimate_portfolio_stats(positions or [])
        )
        buy_signals = self.generate_buy_signals(
            candidate_pool,
            market_status,
            positions=positions,
            portfolio_risk=portfolio_risk,
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
            primary_signals=buy_signals,
        )

        trade_signals = sorted(
            routed_signals + sell_signals + t_signals + risk_alerts,
            key=lambda item: item.get('score', 0),
            reverse=True
        )

        result = {
            'scan_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'market_status': market_status,
            'candidate_pool': selection_result['selected'],
            'buy_signals': routed_signals,
            'sell_signals': sell_signals,
            't_signals': t_signals,
            'risk_alerts': risk_alerts,
            'portfolio_risk': portfolio_risk.to_dict(),
            'trade_signals': trade_signals,
            'stats': {
                'total_stocks': len(stock_list),
                'fundamental_passed': len(fundamental_passed),
                'volume_passed': len(volume_passed),
                'technical_analyzed': len(analyzed_stocks),
                'candidate_pool_count': len(candidate_pool),
                'buy_signals_count': len(routed_signals),
                'sell_signals_count': len(sell_signals),
                't_signals_count': len(t_signals),
                'risk_alerts_count': len(risk_alerts),
                'trade_signals_count': len(trade_signals),
            }
        }

        logger.info("=" * 50)
        logger.info("每日扫描完成")
        logger.info("=" * 50)

        return result

    def _enrich_technical_context(self, stock_info, tech_result, df):
        current_price = tech_result['current_price']
        current_ma = tech_result['ma250']
        close_series = df['close']
        price_change_pct = float(close_series.pct_change().iloc[-1]) if len(close_series) >= 2 else 0.0
        close_vs_ma = ((current_price - current_ma) / current_ma) if current_ma else 0.0

        working_df = pd.DataFrame({'close': close_series}).copy()
        working_df['ma_long'] = close_series.rolling(self.config.get('regime', {}).get('ma_long', 250)).mean()
        bear_trap = self.bear_trap_detector.detect(
            working_df,
            {'is_divergence': tech_result.get('divergence') == 'bullish'},
        )

        stock_info.update(tech_result)
        stock_info.update({
            'price_change_pct': price_change_pct,
            'close_vs_ma_long': close_vs_ma,
            'ma_long_slope': tech_result.get('ma250_slope', 0.0),
            'recent_high_20': float(close_series.tail(20).max()) if len(close_series) >= 20 else float(close_series.max()),
            'recent_low_20': float(close_series.tail(20).min()) if len(close_series) >= 20 else float(close_series.min()),
            'close_above_recent_high': bool(
                current_price >= (
                    float(close_series.iloc[-21:-1].max()) if len(close_series) > 20 else float(close_series.max())
                )
            ) if len(close_series) > 1 else False,
            'bear_trap': bear_trap.is_bear_trap,
            'bear_trap_reason': bear_trap.reason,
        })
        return stock_info

    @staticmethod
    def _build_position_lookup(positions):
        positions = positions or []
        return {position['ts_code']: position for position in positions}

    def _build_entry_signal(self, stock, market_status, current_position=None, portfolio_risk=None):
        if market_status == 'bear':
            return None
        if current_position is None and portfolio_risk and not portfolio_risk.allowed:
            return None
        if current_position is not None and self.config.get('manual_overrides', {}).get('only_reduce_positions', False):
            return None

        signals = []
        score = int(stock.get('selection_score', 0))

        if stock['ma250_slope'] > 0:
            signals.append('年线向上')
            score += 15

        if stock['near_ma250'] and stock['is_above_ma250']:
            signals.append('回调至年线附近')
            score += 12

        if stock['divergence'] == 'bullish':
            signals.append('底背离')
            score += 18

        if stock.get('bear_trap'):
            signals.append('空头陷阱回收')
            score += 15

        if stock['macd_golden_cross']:
            signals.append('MACD金叉')
            score += 10

        if stock['volume_ratio'] > 1.5:
            signals.append('放量')
            score += 8

        if stock.get('price_change_pct', 0) < 0 and stock['volume_ratio'] <= 1.2:
            signals.append('缩量下跌')
            score += 6

        threshold = 75 if market_status == 'bull' else 85
        if score < threshold:
            return None

        if current_position is None:
            signal_type = 'BUY'
            action = '买入'
            suggested_ratio = self.position_manager.base_exposure_ratio()
            reason = '趋势战略买入点'
        else:
            signal_type = 'ADD'
            action = '加仓'
            suggested_ratio = self.position_manager.mobile_exposure_ratio()
            reason = '上涨趋势中的回调加仓'

        if suggested_ratio <= 0:
            return None

        explanation = f"{action}依据: " + " + ".join(signals)

        return {
            'ts_code': stock['ts_code'],
            'name': stock['name'],
            'price': stock['current_price'],
            'signal_type': signal_type,
            'action': action,
            'strategy_name': 'trend_following',
            'market_status': market_status,
            'signals': signals,
            'score': score,
            'roe': stock.get('roe', 0),
            'pe': stock.get('pe', 0),
            'market_cap': stock.get('market_cap', 0),
            'reason': reason,
            'explanation': explanation,
            'suggested_position_change': round(suggested_ratio, 4),
            'risk_flags': [],
        }

    def _build_exit_signal(self, pos, tech_result, market_status, profit_pct):
        sell_reasons = []
        reduce_reasons = []
        risk_decision = self.risk_manager.evaluate_position(
            profit_pct=profit_pct,
            tech_result=tech_result,
            market_status=market_status,
        )

        if risk_decision.action == 'SELL':
            sell_reasons.extend(risk_decision.reasons)
        elif risk_decision.action == 'REDUCE':
            reduce_reasons.extend(risk_decision.reasons)

        if tech_result['divergence'] == 'bearish':
            reduce_reasons.append('顶背离')

        if profit_pct > 0.20:
            reduce_reasons.append(f"盈利保护 ({profit_pct*100:.2f}%)")

        if tech_result.get('volume_ratio', 0) > 1.8 and tech_result.get('macd_death_cross'):
            reduce_reasons.append('放量出货')

        if not tech_result['is_above_ma250'] and profit_pct > 0:
            reduce_reasons.append('跌破年线先减仓观察')

        if not sell_reasons and not reduce_reasons:
            return None

        if sell_reasons:
            signal_type = 'SELL'
            action = '卖出'
            reasons = sell_reasons + [reason for reason in reduce_reasons if reason not in sell_reasons]
            suggested_position_change = -1.0
            explanation = "卖出依据: " + " + ".join(reasons)
            score = 95
        else:
            signal_type = 'REDUCE'
            action = '减仓'
            reasons = reduce_reasons
            suggested_position_change = -round(max(self.position_manager.mobile_exposure_ratio(), 0.15), 4)
            explanation = "减仓依据: " + " + ".join(reasons)
            score = 80

        return {
            'ts_code': pos['ts_code'],
            'name': pos.get('name', ''),
            'buy_price': pos['buy_price'],
            'price': tech_result['current_price'],
            'current_price': tech_result['current_price'],
            'profit_pct': profit_pct,
            'signal_type': signal_type,
            'action': action,
            'strategy_name': 'trend_following',
            'market_status': market_status,
            'score': score,
            'reasons': reasons,
            'reason': reasons[0],
            'explanation': explanation,
            'suggested_position_change': suggested_position_change,
            'risk_flags': risk_decision.risk_flags,
        }

    def _estimate_portfolio_stats(self, positions):
        if not positions:
            return {
                'portfolio_drawdown_pct': 0.0,
                'single_day_drawdown_pct': 0.0,
                'current_exposure_pct': 0.0,
            }

        exposure = 0.0
        drawdowns = []
        for position in positions:
            exposure += position.get(
                'position_ratio',
                position.get('exposure_pct', self.position_manager.base_exposure_ratio()),
            )
            buy_price = position.get('buy_price')
            current_price = position.get('current_price')
            if buy_price and current_price:
                pnl = (current_price - buy_price) / buy_price
                if pnl < 0:
                    drawdowns.append(abs(pnl))

        return {
            'portfolio_drawdown_pct': max(drawdowns) if drawdowns else 0.0,
            'single_day_drawdown_pct': max(drawdowns) if drawdowns else 0.0,
            'current_exposure_pct': min(exposure, 1.5),
        }
