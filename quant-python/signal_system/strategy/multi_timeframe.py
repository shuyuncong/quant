"""Multi-timeframe Chan/MACD analysis and explainable signal scoring."""

from __future__ import annotations

from typing import Any

import pandas as pd

from models import SignalEvent, TimeframeReport
from strategy.chan import analyze_chan
from strategy.macd import analyze_macd
from utils.time_utils import now_shanghai


DEFAULT_ORDER = ["1m", "5m", "15m", "30m", "60m", "120m", "1d"]
DEFAULT_WEIGHTS = {"1m": 1, "5m": 2, "15m": 3, "30m": 4, "60m": 5, "120m": 6, "1d": 8}
CHAN_BUY_POINTS = {"buy_1": 20, "buy_2": 25, "buy_3": 30}
CHAN_SELL_POINTS = {"sell_1": 20, "sell_2": 25, "sell_3": 30}


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
        self.min_bi_bars = int(chan.get("min_bi_bars", 4))
        self.divergence_ratio = float(chan.get("divergence_ratio", 0.9))
        self.fresh_signal_bars = int(chan.get("fresh_signal_bars", 1))
        self.buy_threshold = int(scoring.get("buy_threshold", 60))
        self.sell_threshold = int(scoring.get("sell_threshold", 60))
        self.timeframe_weights = {**DEFAULT_WEIGHTS, **scoring.get("timeframe_weights", {})}
        self.volume_unit_shares = float(config.get("market_data", {}).get("volume_unit_shares", 100))

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

    def _base_report(self, timeframe: str, frame: pd.DataFrame) -> tuple[TimeframeReport, pd.DataFrame]:
        closed = frame[frame["is_closed"]].copy().reset_index(drop=True)
        enriched, indicators = analyze_macd(
            closed,
            fast=self.fast,
            slow=self.slow,
            signal=self.signal,
            zero_axis_tolerance=self.zero_axis_tolerance,
        )
        if indicators.get("status") != "ok":
            return TimeframeReport(timeframe=timeframe, status="insufficient_data"), enriched
        chan = analyze_chan(
            enriched,
            min_bi_bars=self.min_bi_bars,
            divergence_ratio=self.divergence_ratio,
        )
        fresh = self._fresh_signals(chan["signals"], enriched)
        compact_chan = {
            "status": chan["status"],
            "merged_bar_count": len(chan["merged_bars"]),
            "fractal_count": len(chan["fractals"]),
            "stroke_count": len(chan["strokes"]),
            "center_count": len(chan["centers"]),
            "latest_center": chan["centers"][-1] if chan["centers"] else None,
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
            ),
            enriched,
        )

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
            if indicators.get("zero_axis_golden_cross"):
                score += 30
                reasons.append("MACD 0轴附近金叉 +30")
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

    def _event(
        self,
        symbol: str,
        name: str,
        report: TimeframeReport,
        side: str,
        score: int,
        score_reasons: list[str],
    ) -> SignalEvent | None:
        indicators = report.indicators
        fresh = [item for item in report.chan.get("fresh_signals", []) if item["side"] == side]
        cross = indicators.get("zero_axis_golden_cross" if side == "buy" else "zero_axis_death_cross")
        if not cross and not fresh:
            return None
        if side == "buy" and score < self.buy_threshold:
            return None
        if side == "sell" and score < self.sell_threshold:
            return None
        point_order = CHAN_BUY_POINTS if side == "buy" else CHAN_SELL_POINTS
        strongest = max(fresh, key=lambda item: point_order.get(item["signal_type"], 0)) if fresh else None
        components = []
        if strongest:
            components.append(strongest["signal_type"])
        if cross:
            components.append("zero_axis_golden_cross" if side == "buy" else "zero_axis_death_cross")
        confirmed_at = max(
            [item["confirmed_at"] for item in fresh] + [report.latest_time or ""]
            if cross
            else [item["confirmed_at"] for item in fresh]
        )
        structure_time = strongest["structure_time"] if strongest else report.latest_time or confirmed_at
        evidence = {
            "components": components,
            "score_reasons": score_reasons,
            "indicators": indicators,
            "chan_signal": strongest,
            "latest_center": report.chan.get("latest_center"),
            "timeframe_weight": self.timeframe_weights.get(report.timeframe, 1),
        }
        return SignalEvent(
            symbol=symbol,
            name=name,
            timeframe=report.timeframe,
            signal_type="+".join(components),
            side=side,
            price=float(report.latest_price or 0.0),
            structure_time=structure_time,
            confirmed_at=confirmed_at,
            score=score,
            evidence=evidence,
        )

    def analyze(
        self,
        symbol: str,
        name: str,
        bars_by_timeframe: dict[str, pd.DataFrame],
        errors: dict[str, str] | None = None,
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
                report, _ = self._base_report(timeframe, frame)
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
            buy_event = self._event(symbol, name, report, "buy", buy_score, buy_reasons)
            sell_event = self._event(symbol, name, report, "sell", sell_score, sell_reasons)
            if buy_event:
                report.events.append(buy_event)
                events.append(buy_event)
            if sell_event:
                report.events.append(sell_event)
                events.append(sell_event)

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
