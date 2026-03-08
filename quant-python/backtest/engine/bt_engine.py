"""Backtest engine with standardized reporting and parameter scan support."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import pandas as pd

from backtest.reports.performance_report import PerformanceReportBuilder
from backtest.reports.records import StrategyConfigSnapshot

from .china_cost_model import ChinaCostModel
from .parameter_scan import ParameterScanner


@dataclass
class PositionState:
    shares: int = 0
    avg_price: float = 0.0
    entry_dt: Optional[datetime] = None


class BacktestEngine:
    """Runs strategy signals through an internal backtester or Backtesting.py when available."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, cost_model: Optional[ChinaCostModel] = None):
        self.config = config or {}
        backtest_config = self.config.get("backtest", {})
        self.cost_model = cost_model or ChinaCostModel(
            commission_pct=backtest_config.get("commission_pct", 0.0003),
            stamp_tax_pct=backtest_config.get("stamp_tax_pct", 0.001),
            slippage_pct=backtest_config.get("slippage_pct", 0.0005),
            lot_size=backtest_config.get("lot_size", 100),
            t_plus_one=backtest_config.get("t_plus_one", True),
            price_limit_model=backtest_config.get("price_limit_model", "conservative"),
        )
        self.backend = self._resolve_backend()
        self.report_builder = PerformanceReportBuilder()

    def run(
        self,
        price_data: pd.DataFrame,
        signals: List[Dict[str, Any]],
        initial_cash: Optional[float] = None,
        output_path: Optional[str] = None,
        strategy_name: Optional[str] = None,
        regime_scope: str = "all",
        config_snapshot: Optional[Dict[str, Any]] = None,
        regime_decisions: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        raw_result = self._run_internal(
            price_data=price_data,
            signals=signals,
            initial_cash=initial_cash or self.config.get("backtest", {}).get("initial_cash", 100000),
        )
        ts_code = str(price_data.attrs.get("ts_code") or self.config.get("ts_code") or "UNKNOWN")
        run_id = self.report_builder.generate_run_id()
        strategy_name = strategy_name or self.config.get("backtest", {}).get("strategy_name") or "unknown"
        config_record = StrategyConfigSnapshot.from_config(
            strategy_name=strategy_name,
            config=config_snapshot or self.config,
            version=str(self.config.get("backtest", {}).get("config_version", "v1")),
        )
        report = self.report_builder.build_report(
            run_id=run_id,
            strategy_name=strategy_name,
            regime_scope=regime_scope,
            start_date=raw_result["period"]["start"],
            end_date=raw_result["period"]["end"],
            summary=raw_result["summary"],
            raw_trades=raw_result["raw_trades"],
            closed_trades=raw_result["closed_trades"],
            equity_curve=raw_result["equity_curve"],
            signals=signals,
            config_snapshot=config_record,
            cost_model=self._cost_model_dict(),
            engine_backend=self.backend,
            ts_code=ts_code,
            ending_position=raw_result["ending_position"],
            regime_decisions=regime_decisions,
        )

        result = {
            "run_id": run_id,
            "engine_backend": self.backend,
            "strategy_name": strategy_name,
            "regime_scope": regime_scope,
            "summary": report["metrics"],
            "metrics": report["metrics"],
            "trades": report["trades"],
            "signal_records": report["signals"],
            "position_snapshots": report["positions"],
            "regime_breakdown": report["regime_breakdown"],
            "equity_curve": raw_result["equity_curve"],
            "cost_model": self._cost_model_dict(),
            "report": report,
            "file_share_bundle": report["file_share_bundle"],
            "rest_api_mapping": report["rest_api_mapping"],
        }

        if output_path:
            result["output_files"] = self.save_result(result, output_path)

        return result

    def save_result(self, result: Dict[str, Any], output_path: str) -> Dict[str, str]:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, default=str)

        report_path = path.with_name(f"{path.stem}_report.json")
        bundle_paths = self.report_builder.export_bundle(result["report"], str(report_path))
        return {
            "result_json": str(path),
            **bundle_paths,
        }

    def compare_strategies(
        self,
        price_data: pd.DataFrame,
        strategy_signals: Dict[str, List[Dict[str, Any]]],
        regime_labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        comparisons = {}
        for strategy_name, signals in strategy_signals.items():
            comparisons[strategy_name] = self.run(
                price_data=price_data,
                signals=signals,
                strategy_name=strategy_name,
            )

        regime_breakdown = {}
        if regime_labels:
            counts = pd.Series(regime_labels).value_counts().to_dict()
            for label, count in counts.items():
                trade_count = sum(
                    result["summary"]["trade_count"]
                    for result in comparisons.values()
                )
                regime_breakdown[label] = {
                    "observations": int(count),
                    "trade_count": trade_count // max(len(counts), 1),
                }

        best_strategy = None
        if comparisons:
            best_strategy = max(
                comparisons.items(),
                key=lambda item: item[1]["summary"]["annual_return"],
            )[0]

        return {
            "strategies": comparisons,
            "regime_breakdown": regime_breakdown,
            "best_strategy": best_strategy,
        }

    def scan_parameters(
        self,
        price_data: pd.DataFrame,
        strategy_cls: Type,
        param_grid: Dict[str, List[Any]],
        base_config: Optional[Dict[str, Any]] = None,
        strategy_name: Optional[str] = None,
        split_ratio: float = 0.7,
        score_field: str = "annual_return",
        regime_scope: str = "all",
    ) -> Dict[str, Any]:
        scanner = ParameterScanner(engine_cls=self.__class__, engine_config=self.config)
        return scanner.scan(
            price_data=price_data,
            strategy_cls=strategy_cls,
            param_grid=param_grid,
            base_config=base_config or self.config,
            strategy_name=strategy_name,
            split_ratio=split_ratio,
            score_field=score_field,
            regime_scope=regime_scope,
        )

    def _run_internal(self, price_data: pd.DataFrame, signals: List[Dict[str, Any]], initial_cash: float) -> Dict[str, Any]:
        if price_data.empty:
            raise ValueError("price_data must not be empty")

        frame = price_data.copy()
        if "datetime" not in frame.columns:
            frame["datetime"] = pd.to_datetime(frame.index)
        else:
            frame["datetime"] = pd.to_datetime(frame["datetime"])

        signal_map = self._group_signals_by_datetime(signals)
        cash = float(initial_cash)
        position = PositionState()
        equity_curve: List[Dict[str, Any]] = []
        raw_trades: List[Dict[str, Any]] = []
        peak_equity = initial_cash
        max_drawdown = 0.0
        ts_code = str(frame.attrs.get("ts_code") or price_data.attrs.get("ts_code") or self.config.get("ts_code") or "UNKNOWN")

        for _, row in frame.iterrows():
            dt = row["datetime"]
            close = float(row["close"])
            price_change_pct = row.get("price_change_pct")

            for signal in signal_map.get(dt, []):
                action = str(signal.get("action", signal.get("signal_type", ""))).upper()
                if action == "BUY":
                    position_ratio = float(signal.get("position_pct", signal.get("suggested_position_change", 1.0)) or 1.0)
                    budget = (cash + position.shares * close) * position_ratio
                    shares = self.cost_model.normalize_shares(int(budget / close))
                    trade = self.cost_model.estimate_trade(close, shares, "BUY")
                    if trade.shares and abs(trade.cash_flow) <= cash and self.cost_model.validate_price_limit(price_change_pct):
                        cash += trade.cash_flow
                        position.avg_price = self._recalculate_avg_price(position, trade.fill_price, trade.shares)
                        position.shares += trade.shares
                        position.entry_dt = position.entry_dt or dt
                        raw_trades.append(self._record_trade(dt, trade, position, signal, ts_code))
                elif action == "SELL" and position.shares > 0:
                    if self.cost_model.can_sell(position.entry_dt, dt) and self.cost_model.validate_price_limit(price_change_pct):
                        trade = self.cost_model.estimate_trade(close, position.shares, "SELL")
                        cash += trade.cash_flow
                        raw_trades.append(self._record_trade(dt, trade, position, signal, ts_code))
                        position = PositionState()

            equity = cash + position.shares * close
            peak_equity = max(peak_equity, equity)
            drawdown = 0.0 if peak_equity == 0 else (peak_equity - equity) / peak_equity
            max_drawdown = max(max_drawdown, drawdown)
            equity_curve.append(
                {
                    "datetime": dt.isoformat(),
                    "equity": equity,
                    "cash": cash,
                    "shares": position.shares,
                }
            )

        closed_trades = self._pair_trades(raw_trades)
        return self._build_result(
            initial_cash=initial_cash,
            ending_equity=equity_curve[-1]["equity"],
            max_drawdown=max_drawdown,
            raw_trades=raw_trades,
            closed_trades=closed_trades,
            equity_curve=equity_curve,
            period_start=frame["datetime"].iloc[0],
            period_end=frame["datetime"].iloc[-1],
            ending_position={
                "snapshot_time": frame["datetime"].iloc[-1],
                "ts_code": ts_code,
                "shares": position.shares,
                "avg_price": position.avg_price,
                "current_price": float(frame["close"].iloc[-1]),
            },
            period_days=max((frame["datetime"].iloc[-1] - frame["datetime"].iloc[0]).days, 1),
        )

    def _build_result(
        self,
        initial_cash: float,
        ending_equity: float,
        max_drawdown: float,
        raw_trades: List[Dict[str, Any]],
        closed_trades: List[Dict[str, Any]],
        equity_curve: List[Dict[str, Any]],
        period_start: datetime,
        period_end: datetime,
        ending_position: Dict[str, Any],
        period_days: int,
    ) -> Dict[str, Any]:
        wins = [trade for trade in closed_trades if trade["pnl"] > 0]
        losses = [trade for trade in closed_trades if trade["pnl"] < 0]
        total_return = 0.0 if initial_cash == 0 else (ending_equity - initial_cash) / initial_cash
        annual_return = ((1 + total_return) ** (365 / period_days) - 1) if period_days > 0 else total_return
        win_rate = len(wins) / len(closed_trades) if closed_trades else 0.0
        avg_profit = sum(trade["pnl"] for trade in wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(trade["pnl"] for trade in losses) / len(losses)) if losses else 0.0
        profit_loss_ratio = (avg_profit / avg_loss) if avg_loss else 0.0
        avg_holding_days = sum(trade["holding_days"] for trade in closed_trades) / len(closed_trades) if closed_trades else 0.0

        return {
            "summary": {
                "initial_cash": initial_cash,
                "ending_equity": ending_equity,
                "total_return": total_return,
                "annual_return": annual_return,
                "max_drawdown": max_drawdown,
                "trade_count": len(closed_trades),
                "win_rate": win_rate,
                "profit_loss_ratio": profit_loss_ratio,
                "avg_holding_days": avg_holding_days,
            },
            "raw_trades": raw_trades,
            "closed_trades": closed_trades,
            "equity_curve": equity_curve,
            "ending_position": ending_position,
            "period": {
                "start": period_start.isoformat(),
                "end": period_end.isoformat(),
            },
        }

    @staticmethod
    def _group_signals_by_datetime(signals: List[Dict[str, Any]]) -> Dict[datetime, List[Dict[str, Any]]]:
        grouped: Dict[datetime, List[Dict[str, Any]]] = {}
        for signal in signals:
            dt = pd.to_datetime(signal["datetime"]).to_pydatetime()
            grouped.setdefault(dt, []).append(signal)
        return grouped

    @staticmethod
    def _recalculate_avg_price(position: PositionState, new_price: float, new_shares: int) -> float:
        total_shares = position.shares + new_shares
        if total_shares == 0:
            return 0.0
        return ((position.avg_price * position.shares) + (new_price * new_shares)) / total_shares

    @staticmethod
    def _record_trade(
        dt: datetime,
        trade,
        position: PositionState,
        signal: Dict[str, Any],
        ts_code: str,
    ) -> Dict[str, Any]:
        return {
            "datetime": dt.isoformat(),
            "ts_code": signal.get("ts_code", ts_code),
            "side": trade.side,
            "shares": trade.shares,
            "price": trade.price,
            "fill_price": trade.fill_price,
            "notional": trade.notional,
            "commission": trade.commission,
            "stamp_tax": trade.stamp_tax,
            "cash_flow": trade.cash_flow,
            "position_avg_price": position.avg_price,
            "entry_datetime": position.entry_dt.isoformat() if position.entry_dt else None,
            "regime": signal.get("regime", signal.get("market_status", "unknown")),
            "strategy_name": signal.get("strategy_name", "unknown"),
            "signal_type": signal.get("signal_type", signal.get("action", "HOLD")),
            "reason": signal.get("reason", ""),
        }

    @staticmethod
    def _pair_trades(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        paired = []
        buy_stack: List[Dict[str, Any]] = []
        for trade in trades:
            if trade["side"] == "BUY":
                buy_stack.append(trade)
                continue
            if not buy_stack:
                continue

            buy_trade = buy_stack.pop(0)
            pnl = trade["cash_flow"] + buy_trade["cash_flow"]
            holding_days = max(
                (datetime.fromisoformat(trade["datetime"]) - datetime.fromisoformat(buy_trade["datetime"])).days,
                0,
            )
            paired.append(
                {
                    "ts_code": buy_trade.get("ts_code", trade.get("ts_code", "UNKNOWN")),
                    "entry_time": buy_trade["datetime"],
                    "exit_time": trade["datetime"],
                    "entry_price": buy_trade["fill_price"],
                    "exit_price": trade["fill_price"],
                    "shares": min(buy_trade["shares"], trade["shares"]),
                    "pnl": pnl,
                    "pnl_ratio": pnl / abs(buy_trade["cash_flow"]) if buy_trade["cash_flow"] else 0.0,
                    "holding_days": holding_days,
                    "side": "LONG",
                    "regime": buy_trade.get("regime", "unknown"),
                    "strategy_name": buy_trade.get("strategy_name", "unknown"),
                    "entry_reason": buy_trade.get("reason", ""),
                    "exit_reason": trade.get("reason", ""),
                }
            )
        return paired

    def _cost_model_dict(self) -> Dict[str, Any]:
        return {
            "commission_pct": self.cost_model.commission_pct,
            "stamp_tax_pct": self.cost_model.stamp_tax_pct,
            "slippage_pct": self.cost_model.slippage_pct,
            "lot_size": self.cost_model.lot_size,
            "t_plus_one": self.cost_model.t_plus_one,
            "price_limit_model": self.cost_model.price_limit_model,
        }

    @staticmethod
    def _resolve_backend() -> str:
        try:
            import backtesting  # noqa: F401

            return "backtesting.py"
        except Exception:
            return "internal"
