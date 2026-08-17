"""Performance report generation and export helpers."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from .records import (
    BacktestRun,
    BacktestTrade,
    PositionSnapshot,
    RegimeDecisionRecord,
    SignalRecord,
    StrategyConfigSnapshot,
    records_to_dicts,
)


class PerformanceReportBuilder:
    """标准化回测报告构建器。

    它负责把引擎产出的原始结果整理成统一结构，并额外生成:
    - 指标汇总
    - 交易/信号/持仓标准记录
    - 文件共享 payload
    - REST API 映射 payload
    """

    def __init__(self, format_version: str = "v1"):
        self.format_version = format_version

    def build_report(
        self,
        run_id: str,
        strategy_name: str,
        regime_scope: str,
        start_date: Any,
        end_date: Any,
        summary: Dict[str, Any],
        raw_trades: List[Dict[str, Any]],
        closed_trades: List[Dict[str, Any]],
        equity_curve: List[Dict[str, Any]],
        signals: Optional[List[Dict[str, Any]]] = None,
        config_snapshot: Optional[StrategyConfigSnapshot] = None,
        cost_model: Optional[Dict[str, Any]] = None,
        engine_backend: str = "internal",
        ts_code: str = "UNKNOWN",
        ending_position: Optional[Dict[str, Any]] = None,
        regime_decisions: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """构建完整标准报告。"""
        signal_records = [
            SignalRecord.from_payload(signal, index=index, default_ts_code=ts_code)
            for index, signal in enumerate(signals or [])
        ]
        trade_records = self._build_trade_records(
            run_id=run_id,
            closed_trades=closed_trades,
            ts_code=ts_code,
        )
        position_snapshots = self._build_position_snapshots(ending_position, ts_code)
        regime_records = [
            RegimeDecisionRecord.from_payload(payload)
            for payload in (regime_decisions or [])
        ]

        metrics = self.compute_metrics(
            summary=summary,
            raw_trades=raw_trades,
            trade_records=trade_records,
            equity_curve=equity_curve,
            signal_records=signal_records,
        )
        regime_breakdown = self._build_regime_breakdown(trade_records, signal_records)
        config_snapshot = config_snapshot or StrategyConfigSnapshot.from_config(
            strategy_name=strategy_name,
            config={},
        )
        run_record = BacktestRun(
            run_id=run_id,
            strategy_name=strategy_name,
            regime_scope=regime_scope,
            start_date=self._as_iso(start_date),
            end_date=self._as_iso(end_date),
            config_snapshot=config_snapshot,
            metrics=metrics,
            engine_backend=engine_backend,
            cost_model=cost_model or {},
        )

        report = {
            "format_version": self.format_version,
            "run": run_record.to_dict(),
            "metrics": metrics,
            "cost_model": cost_model or {},
            "regime_breakdown": regime_breakdown,
            "signals": records_to_dicts(signal_records),
            "positions": records_to_dicts(position_snapshots),
            "trades": records_to_dicts(trade_records),
            "equity_curve": equity_curve,
            "regime_decisions": records_to_dicts(regime_records),
        }
        report["file_share_bundle"] = self.build_file_share_bundle(report)
        report["rest_api_mapping"] = self.build_rest_api_mapping(report)
        return report

    def compute_metrics(
        self,
        summary: Dict[str, Any],
        raw_trades: List[Dict[str, Any]],
        trade_records: List[BacktestTrade],
        equity_curve: List[Dict[str, Any]],
        signal_records: List[SignalRecord],
    ) -> Dict[str, Any]:
        """计算扩展指标。

        除基础收益指标外，还会补充:
        - `turnover_rate`: 交易额 / 平均权益
        - `signal_hit_rate`: 入场信号中最终盈利的占比
        - `sharpe_ratio`: 基于日收益序列的年化 Sharpe
        - `calmar_ratio`: 年化收益 / 最大回撤
        """
        equity_frame = pd.DataFrame(equity_curve)
        average_equity = float(equity_frame["equity"].mean()) if not equity_frame.empty else 0.0
        turnover_notional = sum(abs(float(item.get("notional", 0.0))) for item in raw_trades)
        turnover_rate = (turnover_notional / average_equity) if average_equity else 0.0

        # 回测胜率只对已闭合交易计算。入场信号可能因 T+1、资金或持有期
        # 尚未成交/平仓，不能直接作为胜率分母；额外保留未配对数量供审计。
        entry_signals = [record for record in signal_records if record.signal_type in {"BUY", "ADD"}]
        profitable_trades = [record for record in trade_records if record.pnl > 0]
        completed_trade_win_rate = (
            len(profitable_trades) / len(trade_records) if trade_records else 0.0
        )
        unmatched_entry_signal_count = max(len(entry_signals) - len(trade_records), 0)

        daily_returns = []
        if not equity_frame.empty and len(equity_frame) > 1:
            equity_frame["return"] = equity_frame["equity"].pct_change()
            daily_returns = equity_frame["return"].dropna().tolist()

        sharpe_ratio = 0.0
        if daily_returns:
            returns_series = pd.Series(daily_returns)
            std = float(returns_series.std())
            if std > 0:
                sharpe_ratio = float(returns_series.mean()) / std * sqrt(252)

        max_drawdown = float(summary.get("max_drawdown", 0.0) or 0.0)
        annual_return = float(summary.get("annual_return", 0.0) or 0.0)
        calmar_ratio = (annual_return / max_drawdown) if max_drawdown else 0.0
        total_pnl = sum(record.pnl for record in trade_records)
        avg_trade_pnl = (total_pnl / len(trade_records)) if trade_records else 0.0

        metrics = {
            "initial_cash": float(summary.get("initial_cash", 0.0) or 0.0),
            "ending_equity": float(summary.get("ending_equity", 0.0) or 0.0),
            "total_return": float(summary.get("total_return", 0.0) or 0.0),
            "annual_return": annual_return,
            "max_drawdown": max_drawdown,
            "trade_count": int(summary.get("trade_count", len(trade_records)) or len(trade_records)),
            "win_rate": float(summary.get("win_rate", 0.0) or 0.0),
            "profit_loss_ratio": float(summary.get("profit_loss_ratio", 0.0) or 0.0),
            "avg_holding_days": float(summary.get("avg_holding_days", 0.0) or 0.0),
            "turnover_rate": turnover_rate,
            # Deprecated compatibility alias. New consumers should use
            # completed_trade_win_rate and inspect unmatched_entry_signal_count.
            "signal_hit_rate": completed_trade_win_rate,
            "completed_trade_win_rate": completed_trade_win_rate,
            "unmatched_entry_signal_count": unmatched_entry_signal_count,
            "sharpe_ratio": sharpe_ratio,
            "calmar_ratio": calmar_ratio,
            "average_equity": average_equity,
            "total_pnl": total_pnl,
            "avg_trade_pnl": avg_trade_pnl,
        }
        return metrics

    def export_json_report(self, report: Dict[str, Any], output_path: str) -> None:
        """导出 JSON 报告。"""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, default=str)

    def export_trade_csv(self, trades: Iterable[Dict[str, Any]], output_path: str) -> None:
        """导出交易明细 CSV，方便手工审阅。"""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = list(trades)
        fieldnames = [
            "run_id",
            "ts_code",
            "entry_time",
            "exit_time",
            "side",
            "entry_price",
            "exit_price",
            "shares",
            "holding_days",
            "pnl",
            "pnl_ratio",
            "regime",
            "strategy_name",
            "entry_reason",
            "exit_reason",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field) for field in fieldnames})

    def export_bundle(self, report: Dict[str, Any], output_path: str) -> Dict[str, str]:
        """把报告拆成 JSON/CSV 多文件输出。"""
        json_path = Path(output_path)
        self.export_json_report(report, str(json_path))

        stem = json_path.stem
        trade_csv_path = json_path.with_name(f"{stem}_trades.csv")
        signal_json_path = json_path.with_name(f"{stem}_signals.json")
        position_json_path = json_path.with_name(f"{stem}_positions.json")

        self.export_trade_csv(report.get("trades", []), str(trade_csv_path))
        self.export_json_report(
            {
                "format_version": self.format_version,
                "signals": report.get("signals", []),
            },
            str(signal_json_path),
        )
        self.export_json_report(
            {
                "format_version": self.format_version,
                "positions": report.get("positions", []),
            },
            str(position_json_path),
        )

        return {
            "report_json": str(json_path),
            "trades_csv": str(trade_csv_path),
            "signals_json": str(signal_json_path),
            "positions_json": str(position_json_path),
        }

    def build_file_share_bundle(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """构建适合文件共享场景的结构化 payload。"""
        return {
            "format_version": self.format_version,
            "channel": "file_share",
            "files": {
                "backtest_run.json": report["run"],
                "signals.json": report.get("signals", []),
                "positions.json": report.get("positions", []),
                "trades.csv": {
                    "columns": [
                        "run_id",
                        "ts_code",
                        "entry_time",
                        "exit_time",
                        "side",
                        "entry_price",
                        "exit_price",
                        "shares",
                        "holding_days",
                        "pnl",
                        "pnl_ratio",
                        "regime",
                        "strategy_name",
                    ],
                    "rows": report.get("trades", []),
                },
            },
        }

    def build_rest_api_mapping(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """构建未来 REST API 入库时的资源映射。"""
        run_id = report["run"]["run_id"]
        return {
            "format_version": self.format_version,
            "channel": "rest_api",
            "resources": {
                "/api/backtests/runs": {
                    "method": "POST",
                    "payload": report["run"],
                },
                f"/api/backtests/runs/{run_id}/signals": {
                    "method": "POST",
                    "payload": report.get("signals", []),
                },
                f"/api/backtests/runs/{run_id}/positions": {
                    "method": "POST",
                    "payload": report.get("positions", []),
                },
                f"/api/backtests/runs/{run_id}/trades": {
                    "method": "POST",
                    "payload": report.get("trades", []),
                },
            },
        }

    @staticmethod
    def generate_run_id(prefix: str = "bt") -> str:
        """生成一次回测运行的唯一 ID。"""
        return f"{prefix}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S_%f')}"

    def _build_trade_records(
        self,
        run_id: str,
        closed_trades: List[Dict[str, Any]],
        ts_code: str,
    ) -> List[BacktestTrade]:
        """把闭合交易转成标准记录对象。"""
        records = []
        for payload in closed_trades:
            records.append(
                BacktestTrade(
                    run_id=run_id,
                    ts_code=str(payload.get("ts_code", ts_code)),
                    entry_time=self._as_iso(payload.get("entry_time")),
                    exit_time=self._as_iso(payload.get("exit_time")),
                    side=str(payload.get("side", "LONG")),
                    entry_price=float(payload.get("entry_price", 0.0) or 0.0),
                    exit_price=float(payload.get("exit_price", 0.0) or 0.0),
                    shares=int(payload.get("shares", 0) or 0),
                    holding_days=int(payload.get("holding_days", 0) or 0),
                    pnl=float(payload.get("pnl", 0.0) or 0.0),
                    pnl_ratio=float(payload.get("pnl_ratio", 0.0) or 0.0),
                    regime=str(payload.get("regime") or "unknown"),
                    strategy_name=str(payload.get("strategy_name") or "unknown"),
                    entry_reason=str(payload.get("entry_reason") or ""),
                    exit_reason=str(payload.get("exit_reason") or ""),
                )
            )
        return records

    def _build_position_snapshots(self, ending_position: Optional[Dict[str, Any]], ts_code: str) -> List[PositionSnapshot]:
        """只在期末仍有持仓时生成持仓快照。"""
        if not ending_position or not ending_position.get("shares"):
            return []
        return [
            PositionSnapshot.from_position(
                snapshot_time=ending_position.get("snapshot_time") or datetime.now(UTC),
                ts_code=str(ending_position.get("ts_code") or ts_code),
                shares=int(ending_position.get("shares", 0) or 0),
                avg_cost=float(ending_position.get("avg_price", 0.0) or 0.0),
                current_price=float(ending_position.get("current_price", 0.0) or 0.0),
                base_shares=int(ending_position.get("base_shares", ending_position.get("shares", 0)) or 0),
            )
        ]

    @staticmethod
    def _build_regime_breakdown(
        trade_records: List[BacktestTrade],
        signal_records: List[SignalRecord],
    ) -> Dict[str, Dict[str, Any]]:
        """按市场状态拆分交易和信号表现。"""
        regimes = sorted({
            record.regime for record in trade_records if record.regime
        } | {
            record.regime for record in signal_records if record.regime
        })
        breakdown: Dict[str, Dict[str, Any]] = {}
        for regime in regimes:
            regime_trades = [trade for trade in trade_records if trade.regime == regime]
            wins = [trade for trade in regime_trades if trade.pnl > 0]
            losses = [trade for trade in regime_trades if trade.pnl < 0]
            avg_loss = abs(sum(trade.pnl for trade in losses) / len(losses)) if losses else 0.0
            avg_profit = sum(trade.pnl for trade in wins) / len(wins) if wins else 0.0
            breakdown[regime] = {
                "trade_count": len(regime_trades),
                "signal_count": len([signal for signal in signal_records if signal.regime == regime]),
                "win_rate": (len(wins) / len(regime_trades)) if regime_trades else 0.0,
                "profit_loss_ratio": (avg_profit / avg_loss) if avg_loss else 0.0,
                "avg_holding_days": (
                    sum(trade.holding_days for trade in regime_trades) / len(regime_trades)
                ) if regime_trades else 0.0,
                "total_pnl": sum(trade.pnl for trade in regime_trades),
            }
        return breakdown

    @staticmethod
    def _as_iso(value: Any) -> str:
        """把任意时间值转换成 ISO 字符串。"""
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)
