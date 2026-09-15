"""One-shot scans and the persistent A-share monitoring loop."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time as clock_time
import copy
import json
import logging
from pathlib import Path
import time
from typing import Any

import pandas as pd

from data.data_fetcher import DataFetcher
from data.market_data import MarketDataClient, normalize_symbol
from models import SignalEvent
from notification.signal_notifier import SignalNotifier
from storage.signal_store import SignalStore
from strategy.multi_timeframe import DEFAULT_ORDER, MultiTimeframeAnalyzer
from strategy.macd import calculate_macd
from strategy.yearline import POOL_TYPE_YEARLINE, last_bar_pullback_candidate
from strategy.market_gate import (
    calculate_strict_regime,
    calculate_trend_gate,
    resolve_market_gate_settings,
    resolve_min_confirmations,
)
from strategy.signal_policy import (
    resolve_signal_execution_policy,
    signal_execution_mode_with_regime,
)
from strategy.stock_pool import evaluate_stock_pool, resolve_stock_pool_config
from core.selector.fundamental import FundamentalEvaluation, evaluate_fundamental
from utils.time_utils import now_shanghai


logger = logging.getLogger(__name__)


def _is_stale_data_error(message: str) -> bool:
    """Return True when the error means the symbol's daily bars are not
    current for the expected trade date (e.g. suspended/delisted), so
    retrying within the same trading day will not help."""
    return "日线已过期" in message or "日线为空" in message


def _fast_gate_latch_state(
    bars: pd.DataFrame,
    mode: str,
    *,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
) -> dict[str, bool]:
    """Replay the stateful fast gate (ma10_latch / macd_death_latch / any_latch)
    over closed index bars and return the latched bear flags at the last bar.

    Mirrors backtest_winrate.build_market_gate so live and backtest gates agree.
    """
    closed = bars[bars["is_closed"].fillna(False).astype(bool)].copy().reset_index(drop=True)
    state = {"ma10_latch_bear": False, "macd_latch_bear": False}
    if closed.empty:
        return state
    mode = str(mode or "none").lower().strip()
    if mode not in {"ma10_latch", "macd_death_latch", "any_latch"}:
        return state
    ma10 = closed["close"].rolling(10, min_periods=10).mean()
    ma10_prev = ma10.shift(1)
    macd = calculate_macd(
        closed["close"],
        fast=macd_fast,
        slow=macd_slow,
        signal=macd_signal,
    )
    death_cross = (macd["dif"] < macd["dea"]) & (macd["dif"].shift(1) >= macd["dea"].shift(1))
    golden_cross = (macd["dif"] > macd["dea"]) & (macd["dif"].shift(1) <= macd["dea"].shift(1))
    for index, row in closed.iterrows():
        if mode in ("ma10_latch", "any_latch"):
            cur = ma10.iloc[index]
            pre = ma10_prev.iloc[index]
            if pd.notna(cur) and pd.notna(pre):
                above = float(row["close"]) > float(cur)
                rising = float(cur) > float(pre)
                if not above and not rising:
                    state["ma10_latch_bear"] = True
                elif above and rising:
                    state["ma10_latch_bear"] = False
        if mode in ("macd_death_latch", "any_latch"):
            if bool(golden_cross.iloc[index]):
                state["macd_latch_bear"] = False
            elif bool(death_cross.iloc[index]):
                state["macd_latch_bear"] = True
    return state


class SignalMonitor:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.market = MarketDataClient(config)
        self._fundamental_fetcher: DataFetcher | None = None
        self.analyzer = MultiTimeframeAnalyzer(config)
        runtime = config.get("runtime", {})
        database_path = runtime.get("database_path", "./state/signal_monitor.db")
        self.store = SignalStore(database_path)
        self.notifier = SignalNotifier(config)
        self.output_dir = Path(runtime.get("output_dir", "./output"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._yearline_market: MarketDataClient | None = None
        monitor = config.get("monitor", {})
        self.timeframes = list(monitor.get("timeframes", DEFAULT_ORDER))
        self.watchlist = [normalize_symbol(item) for item in monitor.get("watchlist", [])]
        self.interval_seconds = int(monitor.get("interval_seconds", 60))
        self.candidate_limit = int(monitor.get("candidate_limit", 100))
        self.candidate_ttl = int(monitor.get("candidate_ttl_business_days", 5))
        self.max_scan_symbols = int(monitor.get("max_scan_symbols_per_run", 500))
        self.max_monitor_symbols = max(1, int(monitor.get("max_symbols_per_cycle", 20)))
        self.daily_scan_time = str(monitor.get("daily_scan_time", "17:00"))
        stock_pool = config.get("stock_pool", {})
        configured_listing_days = int(
            stock_pool.get(
                "min_listing_trade_days",
                config.get("market_data", {}).get("min_listing_trade_days", 120),
            )
        )
        # MACD/Chan analysis still needs a minimum warm-up even when the listing
        # filter is deliberately configured below its normal 120-day default.
        self.min_daily_bars = max(60, configured_listing_days)
        notification = config.get("notification", {})
        self.push_trade_signal = bool(notification.get("push_trade_signal", True))
        self.push_candidate_pool = bool(notification.get("push_candidate_pool", True))
        self.push_ai_analysis = bool(notification.get("push_ai_analysis", True))
        self._name_map: dict[str, str] | None = None

    def _evaluate_live_fundamental(
        self,
        symbol: str,
        as_of,
    ):
        """Evaluate optional live fundamentals without affecting default scans."""
        if not bool(self.config.get("entry_filters", {}).get("fundamental_enabled", False)):
            return FundamentalEvaluation(
                True,
                "disabled",
                (),
                (),
                {"context": "live", "data_status": "disabled"},
            )
        if self._fundamental_fetcher is None:
            self._fundamental_fetcher = DataFetcher(config=self.config)
        financial = self._fundamental_fetcher.get_financial_data(symbol)
        daily_basic = self._fundamental_fetcher.get_daily_basic(
            symbol,
            trade_date=as_of.strftime("%Y%m%d") if hasattr(as_of, "strftime") else str(as_of),
        )
        record = {
            "roe": (financial or {}).get("roe") if financial else None,
            "debt_ratio": (financial or {}).get("debt_to_assets") if financial else None,
            "pe": (daily_basic or {}).get("pe") if daily_basic else None,
            "market_cap": (
                (daily_basic or {}).get("total_mv") / 10000
                if (daily_basic or {}).get("total_mv") is not None
                else None
            ),
            "fundamental_context": "live",
            "fundamental_data_source": "latest_published_snapshot",
            "fundamental_report_period": (financial or {}).get("period") if financial else None,
        }
        return evaluate_fundamental(record, self.config, context="live")

    def _market_entry_context(self) -> dict[str, Any]:
        """Evaluate the index once per daily scan and fail closed when enabled."""
        filters = self.config.get("entry_filters", {})
        if not filters.get("market_gate_enabled", False):
            return {"enabled": False, "allows_entries": True, "regime": "disabled"}

        index_code = str(
            filters.get("market_index_code")
            or self.config.get("regime", {}).get("index_code", "000001.SH")
        )
        try:
            index_bars = self.market.get_index_bars(index_code, limit=300)
            analysis = self.analyzer.analyze(index_code, "市场指数", {"1d": index_bars})
            daily_report = analysis.get("timeframes", {}).get("1d", {})
            indicators = daily_report.get("indicators", {})
            if daily_report.get("status") != "ok" or indicators.get("ma_long") is None:
                raise ValueError("指数长期趋势数据不足")

            fresh_chan = daily_report.get("chan", {}).get("fresh_signals", [])
            bearish_structure = any(
                item.get("side") == "sell" and item.get("signal_type") in {"sell_1", "sell_2", "sell_3"}
                for item in fresh_chan
            )
            # Keep the market gate aligned with the backtest: Chan structure is
            # exposed as context, but only the configured MACD event blocks here.
            death_cross = bool(indicators.get("death_cross"))
            gate_settings = resolve_market_gate_settings(self.config)
            closed_index_bars = index_bars[
                index_bars["is_closed"].fillna(False).astype(bool)
            ].copy().reset_index(drop=True)
            trend_by_day = calculate_trend_gate(
                closed_index_bars["close"],
                gate_settings["trend_gate_enabled"],
                gate_settings["trend_fast_ma"],
                gate_settings["trend_slow_ma"],
            )
            trend_up = bool(trend_by_day.iloc[-1]) if not trend_by_day.empty else False
            macd_settings = gate_settings["macd"]
            fast_gate = _fast_gate_latch_state(
                index_bars,
                gate_settings["fast_gate_mode"],
                macd_fast=macd_settings["fast"],
                macd_slow=macd_settings["slow"],
                macd_signal=macd_settings["signal"],
            )
            fast_bear = fast_gate.get("ma10_latch_bear", False) or fast_gate.get(
                "macd_latch_bear", False
            )
            # Strict MA20/MA10 regime, aligned with backtest build_market_gate.
            if not closed_index_bars.empty:
                strict_regime = str(
                    calculate_strict_regime(
                        closed_index_bars["close"], fast_period=10, slow_period=20
                    ).iloc[-1]
                )
            else:
                strict_regime = "unknown"
            if (
                death_cross
                or indicators.get("ma_long_down")
                or not indicators.get("above_ma_long")
                or not trend_up
                or fast_bear
            ):
                regime = "bear"
            else:
                regime = strict_regime
            return {
                "enabled": True,
                "index_code": index_code,
                "allows_entries": regime != "bear",
                "regime": regime,
                "death_cross": death_cross,
                "bearish_structure": bearish_structure,
                "above_ma_long": bool(indicators.get("above_ma_long")),
                "ma_long_up": bool(indicators.get("ma_long_up")),
                "trend_up": trend_up,
                "ma10_latch_bear": fast_gate.get("ma10_latch_bear", False),
                "macd_latch_bear": fast_gate.get("macd_latch_bear", False),
            }
        except Exception as exc:
            fail_open = bool(filters.get("market_gate_fail_open", False))
            logger.warning("指数状态闸门不可用: %s", exc)
            return {
                "enabled": True,
                "index_code": index_code,
                "allows_entries": fail_open,
                "regime": "unknown",
                "error": str(exc),
            }

    @staticmethod
    def _daily_macd_notification_events(
        event_objects: list[SignalEvent],
        daily_report: dict[str, Any],
        min_confirmations: int = 0,
    ) -> list[SignalEvent]:
        """Keep raw-cross watches and actionable pullback confirmations.

        A raw golden-cross watch is always kept (no confirmation threshold);
        only the actionable pullback-confirmation events require
        confirmation_count >= min_confirmations.
        """
        indicators = daily_report.get("indicators", {})
        raw_zone = str(indicators.get("golden_cross_zone", ""))
        entry_zone = str(
            indicators.get("golden_cross_entry_zone")
            or indicators.get("golden_cross_zone", "")
        )
        raw_ready = bool(
            indicators.get("golden_cross")
            and raw_zone in {"above", "near"}
        )
        confirmed_ready = bool(
            indicators.get("golden_cross_entry_ready")
            and entry_zone in {"above", "near"}
        )
        event_confirmation_count = int(
            indicators.get("confirmation_count", 0) or 0
        )
        confirmation_met = event_confirmation_count >= min_confirmations
        filtered_events = []
        for event in event_objects:
            if event.timeframe != "1d":
                continue
            components = set(event.evidence.get("components", []))
            is_watch = event.evidence.get("signal_level") == "watch"
            is_confirmation = any(
                str(component).startswith(
                    "macd_golden_cross_pullback_confirmed_"
                )
                for component in components
            )
            if (raw_ready and is_watch) or (
                confirmed_ready and is_confirmation and confirmation_met
            ):
                filtered_events.append(event)
        return filtered_events

    def _save_report(self, prefix: str, report: dict[str, Any]) -> Path:
        timestamp = now_shanghai().strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"{prefix}_{timestamp}.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
        return path

    def _resolve_names(
        self, symbols: list[str], known: dict[str, str] | None = None
    ) -> dict[str, str]:
        """Fill in missing stock names from the (cached) A-share list."""
        known = known or {}
        resolved = {normalize_symbol(key): value for key, value in known.items()}
        missing = [
            normalize_symbol(symbol) for symbol in symbols if not resolved.get(normalize_symbol(symbol))
        ]
        if not missing:
            return resolved
        if self._name_map is None:
            try:
                stock_list = self.market.get_stock_list()
                self._name_map = {
                    normalize_symbol(str(row.get("code", ""))): str(row.get("name", ""))
                    for _, row in stock_list.iterrows()
                }
            except Exception as exc:
                logger.warning("获取股票名称失败，报告中的名称可能为空: %s", exc)
                self._name_map = {}
        for symbol in missing:
            name = self._name_map.get(symbol)
            if name:
                resolved[symbol] = name
        return resolved

    def dispatch_outbox(self, requeue_failed: bool = False) -> dict[str, int]:
        summary = {"delivered": 0, "failed": 0}
        if requeue_failed:
            self.store.requeue_failed()
        for _ in range(100):
            claimed = self.store.claim_deliveries(limit=1)
            if not claimed:
                break
            delivery = claimed[0]
            try:
                success, detail = self.notifier.send(delivery["channel"], delivery["payload"])
            except Exception as exc:
                # 发送器抛异常（如 URL 非法）不能让整轮派送中断或留下卡死的 sending 行
                logger.exception(
                    "信号 %s 通过 %s 推送异常: %s",
                    delivery["event_id"],
                    delivery["channel"],
                    exc,
                )
                success, detail = False, f"{type(exc).__name__}: {exc}"
            if success:
                if self.store.mark_delivered(
                    delivery["event_id"], delivery["channel"], delivery["claim_token"]
                ):
                    summary["delivered"] += 1
            else:
                updated = self.store.mark_failed(
                    delivery["event_id"],
                    delivery["channel"],
                    delivery["attempts"],
                    detail,
                    delivery["claim_token"],
                )
                if updated:
                    summary["failed"] += 1
                logger.warning(
                    "信号 %s 通过 %s 推送失败: %s",
                    delivery["event_id"],
                    delivery["channel"],
                    detail,
                )
        return summary

    def _event_is_current(self, event) -> bool:
        try:
            confirmed_date = datetime.fromisoformat(event.confirmed_at).date()
            return confirmed_date >= self.market.latest_expected_trade_date()
        except (TypeError, ValueError):
            return False

    def analyze_symbols(
        self,
        symbols: list[str],
        notify: bool = True,
        names: dict[str, str] | None = None,
        report_meta: dict[str, Any] | None = None,
        only_daily_above_cross: bool = False,
    ) -> dict[str, Any]:
        names = names or {}
        names = self._resolve_names(symbols, names)
        results: list[dict[str, Any]] = []
        new_event_count = 0
        stale_event_count = 0
        market_context = self._market_entry_context()
        market_regime = market_context.get("regime")
        if market_regime not in {"bull", "range", "bear"}:
            market_regime = None
        channels = self.notifier.active_channels()
        for requested_symbol in symbols:
            symbol = normalize_symbol(requested_symbol)
            try:
                bars, errors = self.market.get_multi_timeframe_bars(
                    symbol,
                    self.timeframes,
                    limit=int(self.config.get("monitor", {}).get("bar_limit", 300)),
                )
                analysis = self.analyzer.analyze(
                    symbol,
                    names.get(symbol, ""),
                    bars,
                    errors,
                    regime=market_regime,
                )
                event_objects = analysis.pop("event_objects")
                if only_daily_above_cross:
                    # 日线监控推送两级 MACD 事件：金叉观察预警，以及评分达标的回落确认。
                    daily_report = analysis.get("timeframes", {}).get("1d", {})
                    event_objects = self._daily_macd_notification_events(
                        event_objects,
                        daily_report,
                        resolve_min_confirmations(self.config),
                    )
                if notify and self.push_trade_signal:
                    for event in event_objects:
                        if not self._event_is_current(event):
                            stale_event_count += 1
                            continue
                        if self.store.enqueue_event(event, channels):
                            new_event_count += 1
                results.append(analysis)
            except Exception as exc:
                logger.exception("分析 %s 失败", symbol)
                results.append({"symbol": symbol, "status": "error", "error": str(exc)})

        delivery = self.dispatch_outbox() if notify and channels else {"delivered": 0, "failed": 0}
        report = {
            "mode": "analyze",
            "analyzed_at": now_shanghai().isoformat(timespec="seconds"),
            "signal_execution_policy": resolve_signal_execution_policy(self.config),
            "symbols": len(symbols),
            "new_events": new_event_count,
            "stale_events_skipped": stale_event_count,
            "delivery": delivery,
            "market_context": market_context,
            "results": results,
        }
        report.update(report_meta or {})
        report["output_file"] = str(self._save_report("analysis", report))
        return report

    def notify_ai_analysis(
        self,
        title: str,
        content: str,
        report_path: str = "",
        confirmed_at: str | None = None,
        action_summary: str = "",
    ) -> dict[str, Any]:
        if not self.push_ai_analysis:
            return {"enqueued": 0, "delivery": {"delivered": 0, "failed": 0}, "skipped": "disabled"}
        channels = self.notifier.active_channels()
        if not channels:
            return {"enqueued": 0, "delivery": {"delivered": 0, "failed": 0}, "skipped": "no_channels"}
        timestamp = confirmed_at or now_shanghai().isoformat(timespec="seconds")
        event = SignalEvent(
            symbol="SYSTEM",
            name=title.strip() or "AI自动解读",
            timeframe="report",
            signal_type="ai_analysis",
            side="info",
            price=0.0,
            structure_time=timestamp,
            confirmed_at=timestamp,
            score=0,
            evidence={
                "notification_kind": "ai_analysis",
                "content": content.strip()[:12000],
                "report_path": report_path,
                "action_summary": action_summary,
            },
        )
        inserted = self.store.enqueue_event(event, channels)
        delivery = self.dispatch_outbox()
        return {"enqueued": int(inserted), "delivery": delivery, "event_id": event.event_id}

    def notify_scan_summary(
        self,
        candidate_count: int,
        universe_mode: str,
        completed_round: bool,
        candidates: list[dict[str, Any]] | None = None,
        observed_candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """推送每日扫描完成通知（统一一条汇总，不逐股推送）。

        闸门未开时符合指标的候选进入候选池并标记 execution_mode=observe_only
        （列表内标注"观察，闸门未开"）；policy 级 observe_only 的信号进入
        observed_candidates：仍展示，但不作为独立入场依据。
        """
        channels = self.notifier.active_channels()
        if not channels:
            return {"enqueued": 0, "delivery": {"delivered": 0, "failed": 0}, "skipped": "no_channels"}
        timestamp = now_shanghai().isoformat(timespec="seconds")
        content = f"每日扫描完成，共 {candidate_count} 只符合日线零轴金叉条件"
        if completed_round:
            content += "（全市场轮询完成）"
        listed: list[dict[str, Any]] = []
        for item in (candidates or [])[:50]:
            listed.append(
                {
                    "symbol": str(item["symbol"]),
                    "name": str(item.get("name", "")),
                    "golden_cross_zone_label": str(item.get("golden_cross_zone_label") or ""),
                    "execution_mode": str(item.get("execution_mode") or "enabled"),
                    "observe_reason": str(item.get("observe_reason") or ""),
                }
            )
        lines = []
        for item in listed:
            line = " ".join(part for part in (item["symbol"], item["name"]) if part)
            if item["golden_cross_zone_label"]:
                line += f"（{item['golden_cross_zone_label']}）"
            if item["execution_mode"] == "observe_only":
                line += "[观察，闸门未开]"
            lines.append(line)
        if lines:
            content += "\n\n" + "\n".join(lines)
            if len(listed) < candidate_count:
                content += f"\n… 其余 {candidate_count - len(listed)} 只见股票池"
        observed_listed: list[dict[str, Any]] = []
        for item in (observed_candidates or [])[:50]:
            observed_listed.append(
                {
                    "symbol": str(item["symbol"]),
                    "name": str(item.get("name", "")),
                    "golden_cross_zone_label": str(item.get("golden_cross_zone_label") or ""),
                    "reason": str(item.get("reason") or ""),
                }
            )
        observed_lines = []
        for item in observed_listed:
            line = " ".join(part for part in (item["symbol"], item["name"]) if part)
            if item["golden_cross_zone_label"]:
                line += f"（{item['golden_cross_zone_label']}）"
            if item["reason"]:
                line += f"[{item['reason']}]"
            observed_lines.append(line)
        if observed_lines:
            content += (
                f"\n\n另有 {len(observed_candidates)} 只观察候选"
                "（闸门未开，仅记录展示，不作为独立入场依据）："
            )
            content += "\n" + "\n".join(observed_lines)
            if len(observed_listed) < len(observed_candidates):
                content += f"\n… 其余 {len(observed_candidates) - len(observed_listed)} 只见报告"
        event = SignalEvent(
            symbol="SYSTEM",
            name="每日扫描完成",
            timeframe="report",
            signal_type="scan_summary",
            side="info",
            price=0.0,
            structure_time=timestamp,
            confirmed_at=timestamp,
            score=0,
            evidence={
                "notification_kind": "scan_summary",
                "content": content,
                "universe_mode": universe_mode,
                "candidate_count": candidate_count,
                "observed_count": len(observed_candidates or []),
                "completed_round": completed_round,
                "candidates": listed,
                "observed_candidates": observed_listed,
            },
        )
        inserted = self.store.enqueue_event(event, channels)
        delivery = self.dispatch_outbox()
        return {"enqueued": int(inserted), "delivery": delivery, "event_id": event.event_id}

    def scan_zero_axis(self, notify: bool = True) -> dict[str, Any]:
        scan_config = self.config.get("scan", {})
        universe_mode = str(scan_config.get("universe_mode", "watchlist"))
        snapshot_updates = 0
        try:
            snapshot_updates = self.market.refresh_daily_histories_from_snapshot()
        except Exception as exc:
            logger.warning("批量日线增量刷新失败，将按需逐股补拉: %s", exc)
        bootstrap_complete = False
        bootstrap_success: set[str] = set()
        bootstrap_deferred: dict[str, str] = {}
        deferred_today: set[str] = set()
        insufficient_symbols: set[str] = set()
        expected_trade_date = self.market.latest_expected_trade_date()
        if universe_mode == "watchlist":
            rows = [{"code": item, "name": ""} for item in self.watchlist]
            cursor = 0
            total = len(rows)
            batch = rows
        else:
            stock_list = self.market.get_stock_list()
            rows = stock_list[["code", "name"]].to_dict("records")
            total = len(rows)
            bootstrap_complete = self.store.get_state("daily_bootstrap_complete", "false") == "true"
            if bootstrap_complete:
                cursor = 0
                batch = rows
            else:
                saved = self.store.get_state("daily_bootstrap_success", "[]") or "[]"
                try:
                    bootstrap_success = set(json.loads(saved))
                except json.JSONDecodeError:
                    bootstrap_success = set()
                universe_symbols = {normalize_symbol(row["code"]) for row in rows}
                bootstrap_success.intersection_update(universe_symbols)
                bootstrap_success = {
                    symbol
                    for symbol in bootstrap_success
                    if self.market.daily_history_is_usable(
                        symbol,
                        expected_trade_date,
                        min_bars=self.min_daily_bars,
                    )
                }
                saved_deferred = self.store.get_state("daily_bootstrap_deferred", "{}") or "{}"
                try:
                    loaded_deferred = json.loads(saved_deferred)
                    if isinstance(loaded_deferred, dict):
                        bootstrap_deferred = {
                            normalize_symbol(symbol): str(checked_date)
                            for symbol, checked_date in loaded_deferred.items()
                            if normalize_symbol(symbol) in universe_symbols
                        }
                except json.JSONDecodeError:
                    bootstrap_deferred = {}
                deferred_today = {
                    symbol
                    for symbol, checked_date in bootstrap_deferred.items()
                    if checked_date == expected_trade_date.isoformat()
                }
                deferred_today.difference_update(bootstrap_success)
                remaining = [
                    row
                    for row in rows
                    if normalize_symbol(row["code"]) not in bootstrap_success | deferred_today
                ]
                cursor = len(bootstrap_success) + len(deferred_today)
                batch = remaining[: self.max_scan_symbols]
        candidates: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        stock_pool_rejections: Counter = Counter()
        stock_pool_rejection_details: list[dict[str, Any]] = []
        stock_pool_data_errors: list[dict[str, str]] = []
        fundamental_rejections: Counter = Counter()
        fundamental_details: list[dict[str, Any]] = []
        observed_candidates: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        successful_symbols: set[str] = set()
        market_context = self._market_entry_context()
        entry_filters = self.config.get("entry_filters", {})
        signal_policy = resolve_signal_execution_policy(self.config)
        market_regime = market_context.get("regime")
        if market_regime not in {"bull", "range", "bear"}:
            market_regime = None
        position_gate_enabled = bool(entry_filters.get("position_gate_enabled", False))
        stock_pool_settings = resolve_stock_pool_config(self.config)
        stock_pool_history_limit = max(
            300,
            stock_pool_settings["amount_window"],
            stock_pool_settings["turnover_window"],
            stock_pool_settings["min_listing_trade_days"],
        )
        batch_names = self._resolve_names([normalize_symbol(str(row["code"])) for row in batch])
        for row in batch:
            symbol = normalize_symbol(row["code"])
            name = str(row.get("name", "")) or batch_names.get(symbol, "")
            names[symbol] = name
            try:
                daily = self.market.get_bars(symbol, "1d", limit=300)
                if daily.empty or "datetime" not in daily.columns:
                    raise ValueError("日线为空")
                latest_date = daily["datetime"].iloc[-1].date()
                if latest_date < expected_trade_date:
                    raise ValueError(
                        f"日线已过期: latest={latest_date}, expected={expected_trade_date}"
                    )
                if len(daily) < self.min_daily_bars:
                    if universe_mode != "watchlist":
                        insufficient_symbols.add(symbol)
                        continue
                    raise ValueError(f"日线不足{self.min_daily_bars}个交易日")
                successful_symbols.add(symbol)
                analysis = self.analyzer.analyze(symbol, name, {"1d": daily})
                analysis.pop("event_objects")
                daily_report = analysis["timeframes"].get("1d", {})
                indicators = daily_report.get("indicators", {})
                zone = str(
                    indicators.get("golden_cross_entry_zone")
                    or indicators.get("golden_cross_zone", "near")
                )
                entry_ready = bool(indicators.get("golden_cross_entry_ready", False))
                confirmation_count = int(indicators.get("confirmation_count", 0) or 0)
                min_confirmations = resolve_min_confirmations(self.config)
                confirmation_met = confirmation_count >= min_confirmations
                position_ready = bool(
                    indicators.get("above_ma_long") and indicators.get("ma_long_up")
                )
                # 状态式筛选（用户选定）：曾于 0 轴上方/附近金叉且已确认
                # （confirmed_pullback）、当前仍多头（DIF>DEA）即入池。
                # 不要求确认发生在最后一根 K 线（不再使用 confirmation_is_latest），
                # 指标池是筛选而非当日入场触发器。
                screen_state = str(indicators.get("golden_cross_state") or "none")
                current_zone = str(indicators.get("golden_cross_zone") or zone)
                current_bullish = bool(
                    float(indicators.get("dif", 0) or 0)
                    > float(indicators.get("dea", 0) or 0)
                )
                screen_ready = bool(
                    screen_state == "confirmed_pullback"
                    and zone in {"above", "near"}
                    and current_zone in {"above", "near"}
                    and current_bullish
                )
                if screen_ready and confirmation_met:
                    signal_type = (
                        "macd_golden_cross_pullback_confirmed_"
                        f"{zone}"
                    )
                    execution_mode = signal_execution_mode_with_regime(
                        signal_type,
                        signal_policy,
                        market_regime,
                    )
                    if execution_mode == "disabled":
                        continue
                    if execution_mode == "observe_only":
                        observed_candidates.append(
                            {
                                "symbol": symbol,
                                "name": name,
                                "signal_type": signal_type,
                                "execution_mode": execution_mode,
                                "regime": market_regime,
                                "confirmed_at": daily_report.get("latest_time"),
                                "price": daily_report.get("latest_price"),
                                "golden_cross_zone": zone,
                                "golden_cross_zone_label": indicators.get(
                                    "golden_cross_zone_label"
                                ),
                                "observation_stage": "detected_before_entry_filters",
                            }
                        )
                        continue
                    if not (
                        market_context.get("allows_entries", False)
                        and (not position_gate_enabled or position_ready)
                    ):
                        # 市场/仓位闸门只决定候选是否可执行，不决定指标是否成立。
                        # 熊市不开新仓 ≠ 不从市场筛选符合指标的股票：闸门未开时把
                        # 候选降级为观察记录（execution_mode=observe_only）并保留原因，
                        # 继续走股票池/基本面过滤后进入候选池，而不是静默丢弃，
                        # 避免指标池在熊市"看似 0 数据"。
                        observe_reason = (
                            "position_gate_not_ready"
                            if position_gate_enabled and not position_ready
                            else "market_gate_closed"
                        )
                        execution_mode = "observe_only"
                        entry_gate_blocked = True
                    else:
                        observe_reason = ""
                        entry_gate_blocked = False
                    stock_pool_history = daily
                    stock_pool_fetch_error = ""
                    if stock_pool_settings["enabled"]:
                        try:
                            stock_pool_history = self.market.get_stock_pool_history(
                                symbol,
                                limit=stock_pool_history_limit,
                                end=latest_date,
                            )
                        except Exception as exc:
                            stock_pool_fetch_error = str(exc)
                            stock_pool_data_errors.append(
                                {"symbol": symbol, "error": stock_pool_fetch_error}
                            )
                            logger.warning("股票池指标 %s 获取失败: %s", symbol, exc)
                    stock_pool_evaluation = evaluate_stock_pool(
                        stock_pool_history,
                        latest_date,
                        self.config,
                        name=name,
                    )
                    if stock_pool_fetch_error:
                        stock_pool_evaluation["warnings"] = list(
                            dict.fromkeys(
                                stock_pool_evaluation["warnings"]
                                + ["stock_pool_history_fetch_failed"]
                            )
                        )
                    # 自愈：收盘后第一轮扫描常在数据源回补前把"只到上一交易日"的
                    # 数据以当日 end_day 键缓存 24h，后续重跑会恒命中陈旧缓存。
                    # 检测到 stock_pool_history_stale 时失效该符号当日缓存并重拉
                    # 一次：源已回补则池子正常产出，仍未回补则按原拒绝流程记录。
                    if (
                        stock_pool_settings["enabled"]
                        and "stock_pool_history_stale" in stock_pool_evaluation["reasons"]
                    ):
                        try:
                            invalidated = self.market.invalidate_stock_pool_history(
                                symbol,
                                limit=stock_pool_history_limit,
                                end=latest_date,
                            )
                            if invalidated:
                                retried_history = self.market.get_stock_pool_history(
                                    symbol,
                                    limit=stock_pool_history_limit,
                                    end=latest_date,
                                )
                                retried_evaluation = evaluate_stock_pool(
                                    retried_history,
                                    latest_date,
                                    self.config,
                                    name=name,
                                )
                                if retried_evaluation["passed"]:
                                    stock_pool_evaluation = retried_evaluation
                                    logger.info(
                                        "股票池历史自愈成功: %s 已回补到 %s",
                                        symbol,
                                        latest_date,
                                    )
                                else:
                                    logger.info(
                                        "股票池历史自愈后仍不合格: %s (%s)",
                                        symbol,
                                        ",".join(retried_evaluation["reasons"]),
                                    )
                        except Exception as exc:
                            logger.warning("股票池历史自愈失败 %s: %s", symbol, exc)
                    if not stock_pool_evaluation["passed"]:
                        stock_pool_rejections.update(stock_pool_evaluation["reasons"])
                        stock_pool_rejection_details.append(
                            {
                                "symbol": symbol,
                                "name": name,
                                "reasons": stock_pool_evaluation["reasons"],
                                "warnings": stock_pool_evaluation["warnings"],
                                "metrics": stock_pool_evaluation["metrics"],
                            }
                        )
                        continue
                    fundamental_evaluation = self._evaluate_live_fundamental(
                        symbol,
                        latest_date,
                    )
                    if not fundamental_evaluation.passed:
                        fundamental_rejections.update(
                            fundamental_evaluation.reasons or ["fundamental_rejected"]
                        )
                        fundamental_details.append(
                            {
                                "symbol": symbol,
                                "name": name,
                                "status": fundamental_evaluation.status,
                                "reasons": list(fundamental_evaluation.reasons),
                                "warnings": list(fundamental_evaluation.warnings),
                                "metrics": fundamental_evaluation.metrics,
                            }
                        )
                        continue
                    if fundamental_evaluation.status == "unavailable":
                        fundamental_details.append(
                            {
                                "symbol": symbol,
                                "name": name,
                                "status": fundamental_evaluation.status,
                                "reasons": list(fundamental_evaluation.reasons),
                                "warnings": list(fundamental_evaluation.warnings),
                                "metrics": fundamental_evaluation.metrics,
                            }
                        )
                    zone_priority = {"above": 3, "near": 2, "below": 1}.get(zone, 0)
                    strategy_score = int(daily_report.get("buy_score", 0))
                    candidates.append(
                        {
                            "symbol": symbol,
                            "name": name,
                            "score": zone_priority * 100 + strategy_score,
                            "strategy_score": strategy_score,
                            "zone_priority": zone_priority,
                            "confirmed_at": daily_report.get("latest_time"),
                            "price": daily_report.get("latest_price"),
                            "dif": indicators.get("dif"),
                            "dea": indicators.get("dea"),
                            "hist": indicators.get("hist"),
                            "zero_distance": indicators.get("zero_distance"),
                            "volume_ratio": indicators.get("volume_ratio"),
                            "golden_cross_zone": zone,
                            "golden_cross_zone_label": indicators.get("golden_cross_zone_label"),
                            "golden_cross_quality": indicators.get("golden_cross_quality"),
                            "golden_cross_risk": indicators.get("golden_cross_risk"),
                            "golden_cross_state": indicators.get("golden_cross_state"),
                            "signal_type": signal_type,
                            "execution_mode": execution_mode,
                            "entry_gate_blocked": entry_gate_blocked,
                            "observe_reason": observe_reason,
                            "golden_cross_entry_ready": entry_ready,
                            "golden_cross_confirmation_bars": indicators.get("golden_cross_confirmation_bars"),
                            "position_ready": position_ready,
                            "position_risk_flags": indicators.get("position_risk_flags", []),
                            "market_context": market_context,
                            "stock_pool_metrics": stock_pool_evaluation["metrics"],
                            "stock_pool_warnings": stock_pool_evaluation["warnings"],
                            "fundamental_status": fundamental_evaluation.status,
                            "fundamental_metrics": fundamental_evaluation.metrics,
                            "fundamental_warnings": list(fundamental_evaluation.warnings),
                            "confirmation_items": indicators.get("confirmation_items", []),
                            "confirmation_count": indicators.get("confirmation_count", 0),
                            "chan_signals": daily_report.get("chan", {}).get("fresh_signals", []),
                        }
                    )
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
                logger.warning("扫描 %s 失败: %s", symbol, exc)

        stale_symbols = {
            item["symbol"] for item in errors if _is_stale_data_error(item["error"])
        }
        if universe_mode == "watchlist":
            completed_round = len(successful_symbols) == total
            coverage = 1.0 if total == 0 else len(successful_symbols) / total
        elif not bootstrap_complete:
            for symbol in insufficient_symbols | stale_symbols:
                bootstrap_deferred[symbol] = expected_trade_date.isoformat()
            for symbol in successful_symbols:
                bootstrap_deferred.pop(symbol, None)
            self.store.set_state(
                "daily_bootstrap_deferred",
                json.dumps(bootstrap_deferred, ensure_ascii=False, sort_keys=True),
            )
            bootstrap_success.update(successful_symbols)
            self.store.set_state(
                "daily_bootstrap_success", json.dumps(sorted(bootstrap_success), ensure_ascii=False)
            )
            ineligible = deferred_today | insufficient_symbols | stale_symbols
            eligible_total = max(0, total - len(ineligible))
            coverage = 1.0 if eligible_total == 0 else len(bootstrap_success) / eligible_total
            completed_round = len(bootstrap_success) >= eligible_total
            if completed_round:
                self.store.set_state("daily_bootstrap_complete", "true")
                self.store.set_state("daily_bootstrap_success", "[]")
        else:
            ineligible = insufficient_symbols | stale_symbols
            eligible_total = max(0, total - len(ineligible))
            coverage = 1.0 if eligible_total == 0 else len(successful_symbols) / eligible_total
            completed_round = len(successful_symbols) >= eligible_total
        if universe_mode == "all_a" and completed_round:
            # 全市场轮询完成：未再入选的股票移入失效/过期池
            self.store.sync_candidates(
                candidates,
                ttl_business_days=self.candidate_ttl,
                capacity=self.candidate_limit,
            )
        else:
            self.store.upsert_candidates(
                candidates,
                ttl_business_days=self.candidate_ttl,
                capacity=self.candidate_limit,
            )

        sorted_candidates = sorted(
            candidates,
            key=lambda item: (item.get("zone_priority", 0), item.get("strategy_score", 0)),
            reverse=True,
        )
        channels = self.notifier.active_channels()
        # 每日扫描入库后只推送一条汇总通知，不逐股推送；候选推送开关关闭时跳过汇总，仅投递队列已有事件
        delivery = {"delivered": 0, "failed": 0}
        if notify and channels:
            if self.push_candidate_pool:
                delivery = self.notify_scan_summary(
                    candidate_count=len(candidates),
                    universe_mode=universe_mode,
                    completed_round=completed_round,
                    candidates=sorted_candidates,
                    observed_candidates=observed_candidates,
                )["delivery"]
            else:
                delivery = self.dispatch_outbox()
        report = {
            "mode": "scan",
            "scanned_at": now_shanghai().isoformat(timespec="seconds"),
            "universe_mode": universe_mode,
            "batch_start": cursor,
            "batch_size": len(batch),
            "universe_size": total,
            "coverage": coverage,
            "completed_round": completed_round,
            "ineligible_symbols": len(deferred_today | insufficient_symbols | stale_symbols),
            "market_context": market_context,
            "signal_execution_policy": signal_policy,
            "observed_candidates": observed_candidates,
            "observed_count": len(observed_candidates),
            "stock_pool": {
                "config": stock_pool_settings,
                "rejections": dict(stock_pool_rejections),
                "rejected_candidates": len(stock_pool_rejection_details),
                "rejection_details": stock_pool_rejection_details,
                "data_errors": stock_pool_data_errors,
            },
            "fundamental": {
                "enabled": bool(entry_filters.get("fundamental_enabled", False)),
                "context": "live",
                "rejections": dict(fundamental_rejections),
                "rejected_candidates": sum(
                    1 for item in fundamental_details if item.get("status") == "rejected"
                ),
                "unavailable_candidates": sum(
                    1 for item in fundamental_details if item.get("status") == "unavailable"
                ),
                "details": fundamental_details,
            },
            "snapshot_histories_updated": snapshot_updates,
            "candidate_count": len(candidates),
            "candidates": sorted_candidates,
            "errors": errors,
            "delivery": delivery,
        }
        report["output_file"] = str(self._save_report("scan", report))
        return report

    def _yearline_market_client(self) -> MarketDataClient:
        """懒构建前复权(qfq)日线客户端, 与年线回测数据口径一致。

        复用一个 MarketDataClient 副本把 adjust 改为 qfq, 缓存目录指向
        现有 cache（与回测缓存的 *_qfq.pkl 同目录），不触碰生产 none 口径。
        """
        if self._yearline_market is None:
            client_config = copy.deepcopy(self.config)
            market_data = client_config.setdefault("market_data", {})
            market_data["adjust"] = "qfq"
            market_data["cache_dir"] = str(self.market.cache_dir)
            self._yearline_market = MarketDataClient(client_config)
        return self._yearline_market

    def scan_yearline(self, notify: bool = True) -> dict[str, Any]:
        """年线回踩(MA250)候选扫描 —— 研究展示池, 不进监控/不下单。

        - 与 MACD 候选共用一张候选表, 以 pool_type='yearline_pullback' 区分,
          同步/TTL/容量按池独立处理, 互不截断;
        - 信号只在收盘确认, 入场参考为下一交易日开盘(仅记录字段);
        - 动态止损为 research_only 展示字段, 不修改生产固定 8% 止损;
        - 不推送任何通知(不进入生产信号/监控链路);
        - all_a 模式与 MACD 一样分批轮询, 完成后才 sync, 未完成只 upsert。
        """
        scan_config = self.config.get("scan", {})
        universe_mode = str(scan_config.get("universe_mode", "watchlist"))
        market = self._yearline_market_client()
        expected_trade_date = market.latest_expected_trade_date()
        expected_iso = pd.Timestamp(expected_trade_date).date().isoformat()

        cursor = 0
        success_seed: set[str] = set()
        deferred_seed: dict[str, str] = {}
        deferred_today: set[str] = set()
        if universe_mode == "watchlist":
            rows = [{"code": item, "name": ""} for item in self.watchlist]
            total = len(rows)
            batch = rows
        else:
            stock_list = market.get_stock_list()
            rows = stock_list[["code", "name"]].to_dict("records")
            total = len(rows)
            universe_symbols = {normalize_symbol(str(row["code"])) for row in rows}
            saved_success = self.store.get_state("yearline_bootstrap_success", "[]") or "[]"
            try:
                success_seed = set(json.loads(saved_success))
            except json.JSONDecodeError:
                success_seed = set()
            success_seed.intersection_update(universe_symbols)
            saved_deferred = self.store.get_state("yearline_bootstrap_deferred", "{}") or "{}"
            try:
                loaded_deferred = json.loads(saved_deferred)
                if isinstance(loaded_deferred, dict):
                    deferred_seed = {
                        normalize_symbol(str(sym)): str(day)
                        for sym, day in loaded_deferred.items()
                        if normalize_symbol(str(sym)) in universe_symbols
                    }
            except json.JSONDecodeError:
                deferred_seed = {}
            deferred_today = {
                symbol
                for symbol, checked_date in deferred_seed.items()
                if checked_date == expected_iso
            }
            deferred_today.difference_update(success_seed)
            remaining = [
                row
                for row in rows
                if normalize_symbol(str(row["code"])) not in success_seed | deferred_today
            ]
            cursor = len(success_seed) + len(deferred_today)
            batch = remaining[: self.max_scan_symbols]

        candidates: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        successful_symbols: set[str] = set()
        stale_symbols: set[str] = set()
        names = self._resolve_names(
            [normalize_symbol(str(row["code"])) for row in batch]
        )
        for row in batch:
            symbol = normalize_symbol(str(row["code"]))
            name = str(row.get("name") or "") or names.get(symbol, "")
            try:
                daily = market.get_bars(symbol, "1d", limit=300)
                if daily is None or daily.empty:
                    raise ValueError("日线为空")
                columns = {str(col) for col in daily.columns}
                if not {"datetime", "open", "high", "low", "close", "volume"}.issubset(columns):
                    raise ValueError(
                        "日线缺少列: " + ",".join(sorted(
                            {"datetime", "open", "high", "low", "close", "volume"} - columns
                        ))
                    )
                latest_date = pd.Timestamp(daily["datetime"].iloc[-1]).date()
                if latest_date < pd.Timestamp(expected_trade_date).date():
                    raise ValueError(
                        f"日线已过期: latest={latest_date}, expected={pd.Timestamp(expected_trade_date).date()}"
                    )
                candidate = last_bar_pullback_candidate(symbol, name, daily)
                successful_symbols.add(symbol)
                if candidate is not None:
                    candidates.append(candidate)
            except Exception as exc:
                message = str(exc)
                if _is_stale_data_error(message):
                    stale_symbols.add(symbol)
                else:
                    successful_symbols.add(symbol)
                errors.append({"symbol": symbol, "name": name, "error": message[:300]})

        if universe_mode == "watchlist":
            completed_round = len(successful_symbols) == total
            coverage = 1.0 if total == 0 else len(successful_symbols) / total
        else:
            for symbol in stale_symbols:
                deferred_seed[symbol] = expected_iso
            for symbol in successful_symbols:
                deferred_seed.pop(symbol, None)
            self.store.set_state(
                "yearline_bootstrap_deferred",
                json.dumps(deferred_seed, ensure_ascii=False, sort_keys=True),
            )
            success_seed.update(successful_symbols)
            self.store.set_state(
                "yearline_bootstrap_success",
                json.dumps(sorted(success_seed), ensure_ascii=False),
            )
            ineligible = deferred_today | stale_symbols
            eligible_total = max(0, total - len(ineligible))
            coverage = 1.0 if eligible_total == 0 else len(success_seed) / eligible_total
            completed_round = len(success_seed) >= eligible_total
            # 跨批次累积候选: 全市场分批扫描时, 整轮完成的一次 sync 必须拿到
            # 本轮所有批次发现的候选(而不是只有最后一批), 否则会把前几批写丢。
            cached = self._load_yearline_pool_cache()
            for candidate in candidates:
                cached[normalize_symbol(str(candidate["symbol"]))] = candidate
            self.store.set_state(
                "yearline_pool_cache",
                json.dumps(list(cached.values()), ensure_ascii=False, default=str),
            )
            if cached:
                candidates = list(cached.values())

        if universe_mode == "all_a" and completed_round:
            self.store.sync_candidates(
                candidates,
                ttl_business_days=self.candidate_ttl,
                capacity=self.candidate_limit,
                pool_type=POOL_TYPE_YEARLINE,
            )
            if completed_round:
                self.store.set_state("yearline_bootstrap_success", "[]")
                self.store.set_state("yearline_bootstrap_deferred", "{}")
                self.store.set_state("yearline_pool_cache", "[]")
        else:
            self.store.upsert_candidates(
                candidates,
                ttl_business_days=self.candidate_ttl,
                capacity=self.candidate_limit,
                pool_type=POOL_TYPE_YEARLINE,
            )

        sorted_candidates = sorted(
            candidates,
            key=lambda item: (item.get("score", 0),),
            reverse=True,
        )
        # 研究池: 不发通知, 只投递队列中既有事件
        delivery = self.dispatch_outbox()
        report = {
            "mode": "scan",
            "pool_type": POOL_TYPE_YEARLINE,
            "scanned_at": now_shanghai().isoformat(timespec="seconds"),
            "universe_mode": universe_mode,
            "batch_start": cursor,
            "batch_size": len(batch),
            "universe_size": total,
            "coverage": coverage,
            "completed_round": completed_round,
            "research_only": True,
            "entry_reference": "next_day_open",
            "candidate_count": len(candidates),
            "candidates": sorted_candidates,
            "errors": errors,
            "delivery": delivery,
        }
        report["output_file"] = str(self._save_report("scan_yearline", report))
        return report

    def _load_yearline_pool_cache(self) -> dict[str, dict[str, Any]]:
        """读取全市场分批扫描累积的年线候选缓存 (key: symbol)。"""
        raw = self.store.get_state("yearline_pool_cache", "[]") or "[]"
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError:
            entries = []
        if not isinstance(entries, list):
            return {}
        return {
            normalize_symbol(str(item.get("symbol", ""))): item
            for item in entries
            if isinstance(item, dict) and item.get("symbol")
        }

    def monitoring_symbols(
        self, extra_symbols: list[str] | None = None
    ) -> tuple[list[str], dict[str, str]]:
        """监控范围 = 我的持仓 + 自选股票池(watchlist) + 指标股票池(MACD 池)。

        显式限定 pool_type='macd_zero_axis'：年线池是研究展示池, 绝不进入
        生产监控/下单链路。
        """
        candidates = self.store.active_candidates(
            limit=self.candidate_limit, pool_type="macd_zero_axis"
        )
        names = {normalize_symbol(item["symbol"]): item.get("name", "") for item in candidates}
        extra = [normalize_symbol(item) for item in (extra_symbols or []) if item]
        ordered = list(dict.fromkeys(extra + self.watchlist + list(names)))
        return ordered, self._resolve_names(ordered, names)

    def is_trading_day(self, current: datetime | None = None) -> bool:
        current = current or now_shanghai()
        trade_dates = self.market.get_trade_dates()
        if trade_dates:
            return current.date() in trade_dates
        return current.weekday() < 5

    def is_trading_session(self, current: datetime | None = None) -> bool:
        current = current or now_shanghai()
        if not self.is_trading_day(current):
            return False
        current_time = current.time()
        return clock_time(9, 30) <= current_time <= clock_time(11, 30) or clock_time(
            13, 0
        ) <= current_time <= clock_time(15, 0)

    def run_monitor_cycle(
        self, notify: bool = True, extra_symbols: list[str] | None = None
    ) -> dict[str, Any]:
        symbols, names = self.monitoring_symbols(extra_symbols)
        if not symbols:
            return self.analyze_symbols([], notify=notify, names=names)
        saved_cursor = self.store.get_state("monitor_symbol_cursor", "0") or "0"
        try:
            cursor = int(saved_cursor) % len(symbols)
        except ValueError:
            cursor = 0
        batch_size = min(self.max_monitor_symbols, len(symbols))
        batch = [symbols[(cursor + offset) % len(symbols)] for offset in range(batch_size)]
        next_cursor = (cursor + batch_size) % len(symbols)
        self.store.set_state("monitor_symbol_cursor", str(next_cursor))
        return self.analyze_symbols(
            batch,
            notify=notify,
            names=names,
            report_meta={
                "monitor_pool_size": len(symbols),
                "monitor_batch_start": cursor,
                "monitor_batch_size": batch_size,
                "monitor_next_cursor": next_cursor,
            },
            only_daily_above_cross=notify,
        )

    def run_forever(self, notify: bool = True) -> None:
        logger.info("常驻监控启动，自选股 %s 只", len(self.watchlist))
        last_cycle = 0.0
        while True:
            now = now_shanghai()
            today = now.date().isoformat()
            last_daily = self.store.get_state("last_daily_scan")
            scheduled = datetime.strptime(self.daily_scan_time, "%H:%M").time()
            if self.is_trading_day(now) and now.time() >= scheduled and last_daily != today:
                self.scan_zero_axis(notify=notify)
                self.store.set_state("last_daily_scan", today)

            if self.is_trading_session(now) and time.monotonic() - last_cycle >= self.interval_seconds:
                self.run_monitor_cycle(notify=notify)
                last_cycle = time.monotonic()
            elif notify:
                self.dispatch_outbox()
            time.sleep(min(10, max(1, self.interval_seconds // 6)))
