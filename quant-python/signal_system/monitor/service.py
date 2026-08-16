"""One-shot scans and the persistent A-share monitoring loop."""

from __future__ import annotations

from datetime import datetime, time as clock_time
import json
import logging
from pathlib import Path
import time
from typing import Any

from data.market_data import MarketDataClient, normalize_symbol
from notification.signal_notifier import SignalNotifier
from storage.signal_store import SignalStore
from strategy.multi_timeframe import DEFAULT_ORDER, MultiTimeframeAnalyzer
from utils.time_utils import now_shanghai


logger = logging.getLogger(__name__)


class SignalMonitor:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.market = MarketDataClient(config)
        self.analyzer = MultiTimeframeAnalyzer(config)
        runtime = config.get("runtime", {})
        database_path = runtime.get("database_path", "./data/signal_monitor.db")
        self.store = SignalStore(database_path)
        self.notifier = SignalNotifier(config)
        self.output_dir = Path(runtime.get("output_dir", "./output"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        monitor = config.get("monitor", {})
        self.timeframes = list(monitor.get("timeframes", DEFAULT_ORDER))
        self.watchlist = [normalize_symbol(item) for item in monitor.get("watchlist", [])]
        self.interval_seconds = int(monitor.get("interval_seconds", 60))
        self.candidate_limit = int(monitor.get("candidate_limit", 100))
        self.candidate_ttl = int(monitor.get("candidate_ttl_business_days", 5))
        self.max_scan_symbols = int(monitor.get("max_scan_symbols_per_run", 500))
        self.daily_scan_time = str(monitor.get("daily_scan_time", "15:20"))
        self.min_daily_bars = int(config.get("market_data", {}).get("min_listing_trade_days", 120))
        self._name_map: dict[str, str] | None = None

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

    def dispatch_outbox(self) -> dict[str, int]:
        summary = {"delivered": 0, "failed": 0}
        for _ in range(100):
            claimed = self.store.claim_deliveries(limit=1)
            if not claimed:
                break
            delivery = claimed[0]
            success, detail = self.notifier.send(delivery["channel"], delivery["payload"])
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
    ) -> dict[str, Any]:
        names = names or {}
        names = self._resolve_names(symbols, names)
        results: list[dict[str, Any]] = []
        new_event_count = 0
        stale_event_count = 0
        channels = self.notifier.active_channels()
        for requested_symbol in symbols:
            symbol = normalize_symbol(requested_symbol)
            try:
                bars, errors = self.market.get_multi_timeframe_bars(
                    symbol,
                    self.timeframes,
                    limit=int(self.config.get("monitor", {}).get("bar_limit", 300)),
                )
                analysis = self.analyzer.analyze(symbol, names.get(symbol, ""), bars, errors)
                event_objects = analysis.pop("event_objects")
                if notify:
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
            "symbols": len(symbols),
            "new_events": new_event_count,
            "stale_events_skipped": stale_event_count,
            "delivery": delivery,
            "results": results,
        }
        report["output_file"] = str(self._save_report("analysis", report))
        return report

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
        names: dict[str, str] = {}
        events_to_enqueue = []
        successful_symbols: set[str] = set()
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
                event_objects = analysis.pop("event_objects")
                daily_report = analysis["timeframes"].get("1d", {})
                indicators = daily_report.get("indicators", {})
                if indicators.get("zero_axis_golden_cross"):
                    candidates.append(
                        {
                            "symbol": symbol,
                            "name": name,
                            "score": int(daily_report.get("buy_score", 0)),
                            "confirmed_at": daily_report.get("latest_time"),
                            "dif": indicators.get("dif"),
                            "dea": indicators.get("dea"),
                            "zero_distance": indicators.get("zero_distance"),
                            "chan_signals": daily_report.get("chan", {}).get("fresh_signals", []),
                        }
                    )
                events_to_enqueue.extend(event_objects)
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
                logger.warning("扫描 %s 失败: %s", symbol, exc)

        if universe_mode == "watchlist":
            completed_round = len(successful_symbols) == total
            coverage = 1.0 if total == 0 else len(successful_symbols) / total
        elif not bootstrap_complete:
            for symbol in insufficient_symbols:
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
            ineligible = deferred_today | insufficient_symbols
            eligible_total = max(0, total - len(ineligible))
            coverage = 1.0 if eligible_total == 0 else len(bootstrap_success) / eligible_total
            completed_round = len(bootstrap_success) >= eligible_total
            if completed_round:
                self.store.set_state("daily_bootstrap_complete", "true")
                self.store.set_state("daily_bootstrap_success", "[]")
        else:
            eligible_total = max(0, total - len(insufficient_symbols))
            coverage = 1.0 if eligible_total == 0 else len(successful_symbols) / eligible_total
            completed_round = len(successful_symbols) >= eligible_total
        self.store.upsert_candidates(
            candidates,
            ttl_business_days=self.candidate_ttl,
            capacity=self.candidate_limit,
        )

        channels = self.notifier.active_channels()
        new_event_count = 0
        if notify:
            for event in events_to_enqueue:
                if not self._event_is_current(event):
                    continue
                if self.store.enqueue_event(event, channels):
                    new_event_count += 1
        delivery = self.dispatch_outbox() if notify and channels else {"delivered": 0, "failed": 0}
        report = {
            "mode": "scan",
            "scanned_at": now_shanghai().isoformat(timespec="seconds"),
            "universe_mode": universe_mode,
            "batch_start": cursor,
            "batch_size": len(batch),
            "universe_size": total,
            "coverage": coverage,
            "completed_round": completed_round,
            "ineligible_symbols": len(deferred_today | insufficient_symbols),
            "snapshot_histories_updated": snapshot_updates,
            "candidates": sorted(candidates, key=lambda item: item["score"], reverse=True),
            "errors": errors,
            "new_events": new_event_count,
            "delivery": delivery,
        }
        report["output_file"] = str(self._save_report("scan", report))
        return report

    def monitoring_symbols(self) -> tuple[list[str], dict[str, str]]:
        candidates = self.store.active_candidates(limit=self.candidate_limit)
        names = {normalize_symbol(item["symbol"]): item.get("name", "") for item in candidates}
        ordered = list(dict.fromkeys(self.watchlist + list(names)))
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

    def run_monitor_cycle(self, notify: bool = True) -> dict[str, Any]:
        symbols, names = self.monitoring_symbols()
        return self.analyze_symbols(symbols, notify=notify, names=names)

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
