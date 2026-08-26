"""Multi-timeframe Chan/MACD analysis and explainable signal scoring."""

from __future__ import annotations

from typing import Any

import pandas as pd

from models import SignalEvent, TimeframeReport
from strategy.chan import analyze_chan
from strategy.macd import analyze_macd
from strategy.market_gate import resolve_min_confirmations
from strategy.signal_policy import (
    effective_signal_execution_mode,
    resolve_signal_execution_policy,
    signal_execution_mode_with_regime,
)
from utils.time_utils import now_shanghai


DEFAULT_ORDER = ["1m", "5m", "15m", "30m", "60m", "120m", "1d"]
DEFAULT_WEIGHTS = {"1m": 1, "5m": 2, "15m": 3, "30m": 4, "60m": 5, "120m": 6, "1d": 8}
CHAN_BUY_POINTS = {"buy_1": 20, "buy_2": 25, "buy_3": 30}
CHAN_SELL_POINTS = {"sell_1": 20, "sell_2": 25, "sell_3": 30}
GOLDEN_CROSS_POINTS = {"above": 50, "near": 30, "below": 10}


class MultiTimeframeAnalyzer:
    def __init__(self, config: dict[str, Any]):
        strategy = config.get("signal_strategy", {})
        macd = strategy.get("macd", {})
        chan = strategy.get("chan", {})
        scoring = strategy.get("scoring", {})
        self.fast = int(macd.get("fast", 12))
        self.slow = int(macd.get("slow", 26))
        self.signal = int(macd.get("signal", 9))
        self.zero_axis_tolerance = float(macd.get("zero_axis_tolerance", 0.005))
        self.moderate_volume_min = float(macd.get("moderate_volume_min", 1.0))
        self.moderate_volume_max = float(macd.get("moderate_volume_max", 2.0))
        self.pullback_confirmation_bars = int(macd.get("pullback_confirmation_bars", 5))
        self.long_ma_period = int(macd.get("long_ma_period", 250))
        self.position_lookback = int(macd.get("position_lookback", 20))
        self.max_long_ma_distance = float(macd.get("max_long_ma_distance", 0.35))
        self.max_recent_return = float(macd.get("max_recent_return", 0.30))
        self.high_position_volume_ratio = float(macd.get("high_position_volume_ratio", 3.0))
        self.min_bi_bars = int(chan.get("min_bi_bars", 4))
        self.divergence_ratio = float(chan.get("divergence_ratio", 0.9))
        self.fresh_signal_bars = int(chan.get("fresh_signal_bars", 1))
        self.buy_threshold = int(scoring.get("buy_threshold", 60))
        self.sell_threshold = int(scoring.get("sell_threshold", 60))
        self.timeframe_weights = {**DEFAULT_WEIGHTS, **scoring.get("timeframe_weights", {})}
        self.context_bars = max(10, int(strategy.get("llm_context_bars", 48)))
        self.volume_unit_shares = float(config.get("market_data", {}).get("volume_unit_shares", 100))
        self.signal_execution_policy = resolve_signal_execution_policy(config)
        self.min_confirmations = resolve_min_confirmations(config)

    def _fresh_signals(self, signals: list[dict[str, Any]], frame: pd.DataFrame) -> list[dict[str, Any]]:
        if not signals or frame.empty:
            return []
        cutoff_index = max(0, len(frame) - self.fresh_signal_bars)
        cutoff = pd.Timestamp(frame["datetime"].iloc[cutoff_index])
        return [signal for signal in signals if pd.Timestamp(signal["confirmed_at"]) >= cutoff]

    def _time_sharing_summary(self, frame: pd.DataFrame) -> dict[str, Any]:
        if frame.empty:
            return {}
        latest_day = pd.Timestamp(frame["datetime"].iloc[-1]).date()
        today = frame[pd.to_datetime(frame["datetime"]).dt.date == latest_day]
        if today.empty:
            return {}
        volume = float(today["volume"].sum())
        amount = float(today.get("amount", pd.Series(dtype=float)).sum())
        if amount > 0 and volume > 0:
            average = amount / (volume * self.volume_unit_shares)
            average_type = "vwap"
        else:
            typical = (today["high"] + today["low"] + today["close"]) / 3.0
            weights = today["volume"].where(today["volume"] > 0, 1.0)
            average = float((typical * weights).sum() / weights.sum())
            average_type = "typical_price_approximation"
        first = float(today["open"].iloc[0])
        last = float(today["close"].iloc[-1])
        return {
            "trade_date": latest_day.isoformat(),
            "average_price": float(average),
            "average_type": average_type,
            "change_pct": 0.0 if first == 0 else float((last - first) / first),
            "volume": volume,
            "amount": amount,
        }

    def _base_report(
        self,
        timeframe: str,
        frame: pd.DataFrame,
        regime: str | None = None,
    ) -> tuple[TimeframeReport, pd.DataFrame]:
        source_meta = dict(frame.attrs)
        closed = frame[frame["is_closed"]].copy().reset_index(drop=True)
        enriched, indicators = analyze_macd(
            closed,
            fast=self.fast,
            slow=self.slow,
            signal=self.signal,
            zero_axis_tolerance=self.zero_axis_tolerance,
            moderate_volume_min=self.moderate_volume_min,
            moderate_volume_max=self.moderate_volume_max,
            pullback_confirmation_bars=self.pullback_confirmation_bars,
            long_ma_period=self.long_ma_period if timeframe == "1d" else None,
            position_lookback=self.position_lookback,
            max_long_ma_distance=self.max_long_ma_distance,
            max_recent_return=self.max_recent_return,
            high_position_volume_ratio=self.high_position_volume_ratio,
        )
        source_indicators = {
            "bar_count": len(closed),
            "requested_bar_count": int(source_meta.get("requested_bars", len(closed))),
            "history_complete": bool(source_meta.get("history_complete", True)),
            "source_mode": source_meta.get("source_mode", "unknown"),
            "source_warning": source_meta.get("source_warning")
            or source_meta.get("direct_error"),
        }
        if indicators.get("status") != "ok":
            source_indicators["analysis_warning"] = "MACD 数据不足，无法完成指标计算"
            latest_time = (
                pd.Timestamp(closed["datetime"].iloc[-1]).isoformat()
                if not closed.empty and "datetime" in closed.columns
                else None
            )
            latest_price = (
                float(closed["close"].iloc[-1])
                if not closed.empty and "close" in closed.columns
                else None
            )
            return (
                TimeframeReport(
                    timeframe=timeframe,
                    status="insufficient_data",
                    latest_time=latest_time,
                    latest_price=latest_price,
                    indicators=source_indicators,
                    recent_bars=self._recent_bars(enriched),
                ),
                enriched,
            )
        indicators.update(source_indicators)
        chan = analyze_chan(
            enriched,
            min_bi_bars=self.min_bi_bars,
            divergence_ratio=self.divergence_ratio,
        )
        fresh = []
        for raw_signal in self._fresh_signals(chan["signals"], enriched):
            signal = dict(raw_signal)
            execution_mode = (
                signal_execution_mode_with_regime(
                    str(signal.get("signal_type", "")),
                    self.signal_execution_policy,
                    regime,
                )
                if signal.get("side") == "buy"
                else "enabled"
            )
            signal["execution_mode"] = execution_mode
            signal["actionable"] = execution_mode == "enabled"
            signal["regime"] = regime
            fresh.append(signal)
        compact_chan = {
            "status": chan["status"],
            "merged_bar_count": len(chan["merged_bars"]),
            "fractal_count": len(chan["fractals"]),
            "stroke_count": len(chan["strokes"]),
            "center_count": len(chan["centers"]),
            "latest_center": chan["centers"][-1] if chan["centers"] else None,
            "latest_centers": chan["centers"][-5:],
            "latest_strokes": chan["strokes"][-20:],
            "latest_signals": chan["signals"][-10:],
            "fresh_signals": fresh,
        }
        if timeframe == "1m":
            indicators["time_sharing"] = self._time_sharing_summary(enriched)
        return (
            TimeframeReport(
                timeframe=timeframe,
                status="ok",
                latest_time=pd.Timestamp(enriched["datetime"].iloc[-1]).isoformat(),
                latest_price=float(enriched["close"].iloc[-1]),
                indicators=indicators,
                chan=compact_chan,
                recent_bars=self._recent_bars(enriched),
            ),
            enriched,
        )

    def _recent_bars(self, frame: pd.DataFrame) -> list[dict[str, Any]]:
        columns = [
            column
            for column in ("datetime", "open", "high", "low", "close", "volume", "dif", "dea", "hist")
            if column in frame.columns
        ]
        records: list[dict[str, Any]] = []
        for raw in frame[columns].tail(self.context_bars).to_dict("records"):
            item: dict[str, Any] = {}
            for key, value in raw.items():
                if pd.isna(value):
                    item[key] = None
                elif key == "datetime":
                    item[key] = pd.Timestamp(value).isoformat()
                elif isinstance(value, (int, float)):
                    item[key] = round(float(value), 6)
                else:
                    item[key] = value
            records.append(item)
        return records

    @staticmethod
    def _higher_report(timeframe: str, reports: dict[str, TimeframeReport]) -> TimeframeReport | None:
        try:
            start = DEFAULT_ORDER.index(timeframe) + 1
        except ValueError:
            return None
        for higher in DEFAULT_ORDER[start:]:
            report = reports.get(higher)
            if report and report.status == "ok":
                return report
        return None

    def _score(self, report: TimeframeReport, higher: TimeframeReport | None, side: str) -> tuple[int, list[str]]:
        indicators = report.indicators
        fresh = report.chan.get("fresh_signals", [])
        reasons: list[str] = []
        score = 0
        if side == "buy":
            golden_cross = bool(indicators.get("golden_cross_entry_ready"))
            if golden_cross:
                zone = str(indicators.get("golden_cross_entry_zone") or indicators.get("golden_cross_zone", "near"))
                points = GOLDEN_CROSS_POINTS.get(zone, GOLDEN_CROSS_POINTS["near"])
                score += points
                reasons.append(f"MACD {indicators.get('golden_cross_zone_label', '0轴附近金叉')} +{points}")
                confirmation_points = min(15, int(indicators.get("confirmation_count", 0)) * 5)
                if confirmation_points:
                    score += confirmation_points
                    reasons.append(f"金叉确认条件 {indicators.get('confirmation_count')} 项 +{confirmation_points}")
            elif indicators.get("golden_cross"):
                reasons.append(f"金叉{indicators.get('golden_cross_state', '等待回落确认')}")
            buy_types = [item["signal_type"] for item in fresh if item["side"] == "buy"]
            if buy_types:
                best = max(buy_types, key=lambda item: CHAN_BUY_POINTS.get(item, 0))
                points = CHAN_BUY_POINTS.get(best, 0)
                score += points
                reasons.append(f"缠论{best} +{points}")
            if higher:
                if higher.indicators.get("dif_rising"):
                    score += 15
                    reasons.append("大周期DIF向上 +15")
                if higher.indicators.get("dif", 0) > 0:
                    score += 10
                    reasons.append("大周期位于0轴上方 +10")
            if indicators.get("above_ma60") and indicators.get("ma60_up"):
                score += 10
                reasons.append("站上上行MA60 +10")
            if indicators.get("high_position_risk"):
                score -= 20
                reasons.append("高位偏离长期均线/近期涨幅过大 -20")
            if indicators.get("high_volume_risk"):
                score -= 20
                reasons.append("高位放量上涨 -20")
            if indicators.get("volume_ratio", 0) >= 1:
                score += 5
                reasons.append("量比不低于1 +5")
            if any(item["side"] == "sell" for item in fresh):
                score -= 30
                reasons.append("存在冲突卖点 -30")
        else:
            if indicators.get("zero_axis_death_cross"):
                score += 30
                reasons.append("MACD 0轴附近死叉 +30")
            sell_types = [item["signal_type"] for item in fresh if item["side"] == "sell"]
            if sell_types:
                best = max(sell_types, key=lambda item: CHAN_SELL_POINTS.get(item, 0))
                points = CHAN_SELL_POINTS.get(best, 0)
                score += points
                reasons.append(f"缠论{best} +{points}")
            if higher:
                if higher.indicators.get("dif_falling"):
                    score += 15
                    reasons.append("大周期DIF向下 +15")
                if higher.indicators.get("dif", 0) < 0:
                    score += 10
                    reasons.append("大周期位于0轴下方 +10")
            if indicators.get("below_ma60") and indicators.get("ma60_down"):
                score += 10
                reasons.append("跌破下行MA60 +10")
            if indicators.get("volume_ratio", 0) >= 1 and indicators.get("price_change", 0) < 0:
                score += 5
                reasons.append("放量下跌 +5")
            if any(item["side"] == "buy" for item in fresh):
                score -= 30
                reasons.append("存在冲突买点 -30")
        return max(0, score), reasons

    def _events(
        self,
        symbol: str,
        name: str,
        report: TimeframeReport,
        side: str,
        score: int,
        score_reasons: list[str],
        regime: str | None = None,
    ) -> list[SignalEvent]:
        indicators = report.indicators
        fresh = [item for item in report.chan.get("fresh_signals", []) if item["side"] == side]
        events: list[SignalEvent] = []
        cross = (
            indicators.get("golden_cross_entry_ready")
            if side == "buy"
            else indicators.get("zero_axis_death_cross")
        )
        entry_zone = str(indicators.get("golden_cross_entry_zone") or indicators.get("golden_cross_zone", "near"))
        if side == "buy" and entry_zone == "below":
            cross = False

        raw_cross_zone = str(indicators.get("golden_cross_zone", "near"))
        raw_cross = bool(
            side == "buy"
            and indicators.get("golden_cross")
            and not indicators.get("golden_cross_entry_ready")
            and raw_cross_zone in {"above", "near"}
        )
        cross_time = str(
            indicators.get("golden_cross_cross_time")
            or report.latest_time
            or now_shanghai().isoformat(timespec="seconds")
        )
        setup_id = f"{symbol}|{report.timeframe}|{cross_time}"
        if raw_cross:
            signal_type = f"macd_golden_cross_detected_{raw_cross_zone}"
            watch_reasons = [
                f"MACD {indicators.get('golden_cross_zone_label', '金叉')}出现",
                "等待回落触碰金叉K线实体并重新站回后再确认",
            ]
            events.append(
                SignalEvent(
                    symbol=symbol,
                    name=name,
                    timeframe=report.timeframe,
                    signal_type=signal_type,
                    side="buy",
                    price=float(report.latest_price or 0.0),
                    structure_time=cross_time,
                    confirmed_at=str(report.latest_time or cross_time),
                    score=score,
                    evidence={
                        "components": [signal_type],
                        "score_reasons": watch_reasons,
                        "indicators": indicators,
                        "chan_signal": None,
                        "latest_center": report.chan.get("latest_center"),
                        "timeframe_weight": self.timeframe_weights.get(report.timeframe, 1),
                        "notification_kind": "trade_signal",
                        "strong_signal": False,
                        "signal_level": "watch",
                        "actionable": False,
                        "execution_mode": "observe_only",
                        "regime": regime,
                        "setup_id": setup_id,
                    },
                )
            )
        if not cross and not fresh:
            return events
        threshold = self.buy_threshold if side == "buy" else self.sell_threshold
        strong_signal = score >= threshold
        standalone_confirmation = bool(side == "buy" and cross)
        if not fresh and not strong_signal and not standalone_confirmation:
            return events
        cross_component = None
        if cross:
            cross_component = (
                f"macd_golden_cross_pullback_confirmed_{entry_zone}"
                if side == "buy"
                else "zero_axis_death_cross"
            )

        if side == "buy":
            sources = list(fresh)
            if cross_component:
                sources.append(None)
        else:
            sources = fresh or [None]
        for chan_signal in sources:
            signal_type = (
                str(chan_signal["signal_type"])
                if chan_signal
                else str(cross_component)
            )
            confirmed_at = (
                str(chan_signal["confirmed_at"])
                if chan_signal
                else str(report.latest_time or now_shanghai().isoformat(timespec="seconds"))
            )
            structure_time = (
                str(chan_signal["structure_time"])
                if chan_signal
                else str(report.latest_time or confirmed_at)
            )
            components = [signal_type]
            if side != "buy" and cross_component and cross_component != signal_type:
                components.append(cross_component)
            execution_mode = (
                effective_signal_execution_mode(
                    components,
                    self.signal_execution_policy,
                    regime,
                )
                if side == "buy"
                else "enabled"
            )
            if execution_mode == "disabled":
                continue
            event_is_confirmation = bool(
                side == "buy" and chan_signal is None and cross_component
            )
            confirmation_count = int(indicators.get("confirmation_count", 0) or 0)
            confirmation_threshold_met = (
                not event_is_confirmation
                or confirmation_count >= self.min_confirmations
            )
            if (
                event_is_confirmation
                and not confirmation_threshold_met
                and execution_mode == "enabled"
            ):
                execution_mode = "observe_only"
            technical_signal_level = (
                "strong"
                if strong_signal
                else ("confirmation" if event_is_confirmation else "structure")
            )
            actionable = side != "buy" or execution_mode == "enabled"
            evidence = {
                "components": components,
                "score_reasons": score_reasons,
                "indicators": indicators,
                "chan_signal": chan_signal,
                "latest_center": report.chan.get("latest_center"),
                "timeframe_weight": self.timeframe_weights.get(report.timeframe, 1),
                "notification_kind": "trade_signal",
                "strong_signal": strong_signal,
                "technical_signal_level": technical_signal_level,
                "signal_level": technical_signal_level if actionable else "watch",
                "actionable": actionable,
                "execution_mode": execution_mode,
                "regime": regime,
            }
            if event_is_confirmation:
                evidence["setup_id"] = setup_id
                evidence["min_confirmations"] = self.min_confirmations
                evidence["confirmation_threshold_met"] = confirmation_threshold_met
            events.append(
                SignalEvent(
                    symbol=symbol,
                    name=name,
                    timeframe=report.timeframe,
                    signal_type=signal_type,
                    side=side,
                    price=float(report.latest_price or 0.0),
                    structure_time=structure_time,
                    confirmed_at=confirmed_at,
                    score=score,
                    evidence=evidence,
                )
            )
        return events

    def _event(
        self,
        symbol: str,
        name: str,
        report: TimeframeReport,
        side: str,
        score: int,
        score_reasons: list[str],
        regime: str | None = None,
    ) -> SignalEvent | None:
        events = self._events(
            symbol, name, report, side, score, score_reasons, regime
        )
        return events[0] if events else None

    def analyze(
        self,
        symbol: str,
        name: str,
        bars_by_timeframe: dict[str, pd.DataFrame],
        errors: dict[str, str] | None = None,
        regime: str | None = None,
    ) -> dict[str, Any]:
        reports: dict[str, TimeframeReport] = {}
        for timeframe in DEFAULT_ORDER:
            frame = bars_by_timeframe.get(timeframe)
            if frame is None:
                if errors and timeframe in errors:
                    reports[timeframe] = TimeframeReport(
                        timeframe=timeframe,
                        status="error",
                        error=errors[timeframe],
                    )
                continue
            try:
                report, _ = self._base_report(timeframe, frame, regime)
                reports[timeframe] = report
            except Exception as exc:
                reports[timeframe] = TimeframeReport(
                    timeframe=timeframe,
                    status="error",
                    error=str(exc),
                )

        events: list[SignalEvent] = []
        for timeframe, report in reports.items():
            if report.status != "ok":
                continue
            higher = self._higher_report(timeframe, reports)
            buy_score, buy_reasons = self._score(report, higher, "buy")
            sell_score, sell_reasons = self._score(report, higher, "sell")
            report.buy_score = buy_score
            report.sell_score = sell_score
            buy_events = self._events(
                symbol, name, report, "buy", buy_score, buy_reasons, regime
            )
            sell_events = self._events(
                symbol, name, report, "sell", sell_score, sell_reasons, regime
            )
            report.events.extend(buy_events)
            report.events.extend(sell_events)
            events.extend(buy_events)
            events.extend(sell_events)

        events.sort(
            key=lambda item: (item.score * self.timeframe_weights.get(item.timeframe, 1), item.confirmed_at),
            reverse=True,
        )
        return {
            "symbol": symbol,
            "name": name,
            "analyzed_at": now_shanghai().isoformat(timespec="seconds"),
            "timeframes": {key: value.to_dict() for key, value in reports.items()},
            "events": [event.to_payload() for event in events],
            "event_objects": events,
        }
