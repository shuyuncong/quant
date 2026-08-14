"""Deterministic, engineering-oriented Chan-theory structure analysis."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable

import pandas as pd


@dataclass
class MergedBar:
    index: int
    source_start: int
    source_end: int
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


@dataclass
class Fractal:
    index: int
    kind: str
    price: float
    time: str
    confirmed_at: str


@dataclass
class Stroke:
    index: int
    start_index: int
    end_index: int
    direction: str
    start_price: float
    end_price: float
    high: float
    low: float
    start_time: str
    end_time: str
    confirmed_at: str
    macd_area: float = 0.0


@dataclass
class Center:
    index: int
    start_stroke: int
    end_stroke: int
    zd: float
    zg: float
    low: float
    high: float
    start_time: str
    end_time: str


@dataclass
class ChanSignal:
    signal_type: str
    side: str
    stroke_index: int
    price: float
    structure_time: str
    confirmed_at: str
    center_index: int | None
    evidence: dict[str, Any]


def _time_text(value: Any) -> str:
    return pd.Timestamp(value).isoformat()


def _contains(left: MergedBar, high: float, low: float) -> bool:
    return (left.high >= high and left.low <= low) or (high >= left.high and low <= left.low)


def _trend(previous: MergedBar, current_high: float, current_low: float, fallback: str) -> str:
    if current_high > previous.high and current_low > previous.low:
        return "up"
    if current_high < previous.high and current_low < previous.low:
        return "down"
    return fallback


def merge_inclusions(frame: pd.DataFrame) -> list[MergedBar]:
    """Merge inclusive candles using the last explicit non-inclusive direction."""
    if frame.empty:
        return []

    bars: list[MergedBar] = []
    direction = "up"
    for source_index, (_, row) in enumerate(frame.reset_index(drop=True).iterrows()):
        high = float(row["high"])
        low = float(row["low"])
        amount = float(row.get("amount", 0.0) or 0.0)
        current = MergedBar(
            index=len(bars),
            source_start=source_index,
            source_end=source_index,
            time=_time_text(row["datetime"]),
            open=float(row["open"]),
            high=high,
            low=low,
            close=float(row["close"]),
            volume=float(row.get("volume", 0.0) or 0.0),
            amount=amount,
        )
        if not bars:
            bars.append(current)
            continue

        previous = bars[-1]
        if not _contains(previous, high, low):
            direction = _trend(previous, high, low, direction)
            current.index = len(bars)
            bars.append(current)
            continue

        if len(bars) >= 2:
            direction = _trend(bars[-2], previous.high, previous.low, direction)

        if direction == "up":
            merged_high = max(previous.high, high)
            merged_low = max(previous.low, low)
        else:
            merged_high = min(previous.high, high)
            merged_low = min(previous.low, low)

        bars[-1] = MergedBar(
            index=previous.index,
            source_start=previous.source_start,
            source_end=source_index,
            time=current.time,
            open=previous.open,
            high=merged_high,
            low=merged_low,
            close=current.close,
            volume=previous.volume + current.volume,
            amount=previous.amount + current.amount,
        )
    return bars


def find_fractals(bars: list[MergedBar]) -> list[Fractal]:
    candidates: list[Fractal] = []
    for index in range(1, len(bars) - 1):
        left, middle, right = bars[index - 1], bars[index], bars[index + 1]
        is_top = (
            middle.high > left.high
            and middle.high > right.high
            and middle.low > left.low
            and middle.low > right.low
        )
        is_bottom = (
            middle.low < left.low
            and middle.low < right.low
            and middle.high < left.high
            and middle.high < right.high
        )
        if is_top:
            candidates.append(
                Fractal(index, "top", middle.high, middle.time, right.time)
            )
        elif is_bottom:
            candidates.append(
                Fractal(index, "bottom", middle.low, middle.time, right.time)
            )

    filtered: list[Fractal] = []
    for candidate in candidates:
        if not filtered or filtered[-1].kind != candidate.kind:
            filtered.append(candidate)
            continue
        previous = filtered[-1]
        more_extreme = (
            candidate.price > previous.price
            if candidate.kind == "top"
            else candidate.price < previous.price
        )
        if more_extreme:
            filtered[-1] = candidate
    return filtered


def build_strokes(fractals: list[Fractal], min_bi_bars: int = 4) -> list[Stroke]:
    endpoints: list[Fractal] = []
    for candidate in fractals:
        if not endpoints:
            endpoints.append(candidate)
            continue
        previous = endpoints[-1]
        if candidate.kind == previous.kind:
            more_extreme = (
                candidate.price > previous.price
                if candidate.kind == "top"
                else candidate.price < previous.price
            )
            if more_extreme:
                endpoints[-1] = candidate
            continue
        if candidate.index - previous.index < min_bi_bars:
            continue
        valid_price = (
            previous.kind == "bottom" and candidate.price > previous.price
        ) or (previous.kind == "top" and candidate.price < previous.price)
        if valid_price:
            endpoints.append(candidate)

    strokes: list[Stroke] = []
    for index, (start, end) in enumerate(zip(endpoints, endpoints[1:])):
        direction = "up" if start.kind == "bottom" else "down"
        strokes.append(
            Stroke(
                index=index,
                start_index=start.index,
                end_index=end.index,
                direction=direction,
                start_price=start.price,
                end_price=end.price,
                high=max(start.price, end.price),
                low=min(start.price, end.price),
                start_time=start.time,
                end_time=end.time,
                confirmed_at=end.confirmed_at,
            )
        )
    return strokes


def calculate_stroke_macd_area(strokes: list[Stroke], frame: pd.DataFrame) -> None:
    if "hist" not in frame.columns:
        return
    times = pd.to_datetime(frame["datetime"])
    hist = pd.to_numeric(frame["hist"], errors="coerce").fillna(0.0)
    for stroke in strokes:
        start = pd.Timestamp(stroke.start_time)
        end = pd.Timestamp(stroke.end_time)
        mask = (times >= start) & (times <= end)
        values = hist[mask]
        if stroke.direction == "up":
            values = values[values > 0]
        else:
            values = values[values < 0]
        stroke.macd_area = float(values.abs().sum())


def lock_confirmed_strokes(strokes: list[Stroke]) -> list[Stroke]:
    """A stroke endpoint is immutable only after the following stroke is accepted."""
    return [
        replace(stroke, confirmed_at=strokes[index + 1].confirmed_at)
        for index, stroke in enumerate(strokes[:-1])
    ]


def find_centers(strokes: list[Stroke]) -> list[Center]:
    centers: list[Center] = []
    index = 0
    while index <= len(strokes) - 3:
        seed = strokes[index : index + 3]
        zd = max(stroke.low for stroke in seed)
        zg = min(stroke.high for stroke in seed)
        if zd >= zg:
            index += 1
            continue

        end = index + 2
        expanded_low = min(stroke.low for stroke in seed)
        expanded_high = max(stroke.high for stroke in seed)
        cursor = end + 1
        while cursor < len(strokes):
            stroke = strokes[cursor]
            if stroke.high < zd or stroke.low > zg:
                break
            expanded_low = min(expanded_low, stroke.low)
            expanded_high = max(expanded_high, stroke.high)
            end = cursor
            cursor += 1

        centers.append(
            Center(
                index=len(centers),
                start_stroke=index,
                end_stroke=end,
                zd=zd,
                zg=zg,
                low=expanded_low,
                high=expanded_high,
                start_time=strokes[index].start_time,
                end_time=strokes[end].end_time,
            )
        )
        index = max(index + 1, cursor)
    return centers


def _latest_center_before(centers: Iterable[Center], stroke_index: int) -> Center | None:
    eligible = [center for center in centers if center.end_stroke < stroke_index]
    return eligible[-1] if eligible else None


def detect_chan_signals(
    strokes: list[Stroke],
    centers: list[Center],
    divergence_ratio: float = 0.9,
) -> list[ChanSignal]:
    signals: list[ChanSignal] = []
    first_by_side: dict[str, ChanSignal] = {}

    for index, current in enumerate(strokes):
        if index >= 2:
            previous = strokes[index - 2]
            center = _latest_center_before(centers, index)
            comparable = previous.direction == current.direction and previous.macd_area > 0
            weaker = comparable and current.macd_area < previous.macd_area * divergence_ratio
            if current.direction == "down" and current.low < previous.low and weaker:
                if center is not None and current.end_price < center.zd:
                    signal = ChanSignal(
                        "buy_1",
                        "buy",
                        index,
                        current.end_price,
                        current.end_time,
                        current.confirmed_at,
                        center.index if center else None,
                        {
                            "previous_low": previous.low,
                            "current_low": current.low,
                            "previous_macd_area": previous.macd_area,
                            "current_macd_area": current.macd_area,
                        },
                    )
                    signals.append(signal)
                    first_by_side["buy"] = signal
            elif current.direction == "up" and current.high > previous.high and weaker:
                if center is not None and current.end_price > center.zg:
                    signal = ChanSignal(
                        "sell_1",
                        "sell",
                        index,
                        current.end_price,
                        current.end_time,
                        current.confirmed_at,
                        center.index if center else None,
                        {
                            "previous_high": previous.high,
                            "current_high": current.high,
                            "previous_macd_area": previous.macd_area,
                            "current_macd_area": current.macd_area,
                        },
                    )
                    signals.append(signal)
                    first_by_side["sell"] = signal

        buy_1 = first_by_side.get("buy")
        if buy_1 and index == buy_1.stroke_index + 2 and current.direction == "down":
            if current.low >= buy_1.price:
                signals.append(
                    ChanSignal(
                        "buy_2",
                        "buy",
                        index,
                        current.end_price,
                        current.end_time,
                        current.confirmed_at,
                        buy_1.center_index,
                        {"buy_1_price": buy_1.price, "retest_low": current.low},
                    )
                )

        sell_1 = first_by_side.get("sell")
        if sell_1 and index == sell_1.stroke_index + 2 and current.direction == "up":
            if current.high <= sell_1.price:
                signals.append(
                    ChanSignal(
                        "sell_2",
                        "sell",
                        index,
                        current.end_price,
                        current.end_time,
                        current.confirmed_at,
                        sell_1.center_index,
                        {"sell_1_price": sell_1.price, "retest_high": current.high},
                    )
                )

    for center in centers:
        departure_index = center.end_stroke + 1
        pullback_index = departure_index + 1
        if pullback_index >= len(strokes):
            continue
        departure = strokes[departure_index]
        pullback = strokes[pullback_index]
        if (
            departure.direction == "up"
            and departure.end_price > center.zg
            and pullback.direction == "down"
            and pullback.low > center.zg
        ):
            signals.append(
                ChanSignal(
                    "buy_3",
                    "buy",
                    pullback_index,
                    pullback.end_price,
                    pullback.end_time,
                    pullback.confirmed_at,
                    center.index,
                    {"center_zg": center.zg, "pullback_low": pullback.low},
                )
            )
        elif (
            departure.direction == "down"
            and departure.end_price < center.zd
            and pullback.direction == "up"
            and pullback.high < center.zd
        ):
            signals.append(
                ChanSignal(
                    "sell_3",
                    "sell",
                    pullback_index,
                    pullback.end_price,
                    pullback.end_time,
                    pullback.confirmed_at,
                    center.index,
                    {"center_zd": center.zd, "pullback_high": pullback.high},
                )
            )

    priority = {"buy_3": 3, "sell_3": 3, "buy_2": 2, "sell_2": 2, "buy_1": 1, "sell_1": 1}
    unique: dict[tuple[str, str], ChanSignal] = {}
    for signal in signals:
        key = (signal.signal_type, signal.confirmed_at)
        unique[key] = signal
    return sorted(
        unique.values(),
        key=lambda signal: (signal.confirmed_at, priority.get(signal.signal_type, 0)),
    )


def analyze_chan(
    frame: pd.DataFrame,
    min_bi_bars: int = 4,
    divergence_ratio: float = 0.9,
) -> dict[str, Any]:
    if frame.empty or len(frame) < 10:
        return {
            "status": "insufficient_data",
            "merged_bars": [],
            "fractals": [],
            "strokes": [],
            "provisional_stroke": None,
            "centers": [],
            "signals": [],
        }
    merged = merge_inclusions(frame)
    fractals = find_fractals(merged)
    provisional_strokes = build_strokes(fractals, min_bi_bars=min_bi_bars)
    calculate_stroke_macd_area(provisional_strokes, frame)
    strokes = lock_confirmed_strokes(provisional_strokes)
    centers = find_centers(strokes)
    signals = detect_chan_signals(strokes, centers, divergence_ratio=divergence_ratio)
    return {
        "status": "ok",
        "merged_bars": [asdict(item) for item in merged],
        "fractals": [asdict(item) for item in fractals],
        "strokes": [asdict(item) for item in strokes],
        "provisional_stroke": asdict(provisional_strokes[-1]) if provisional_strokes else None,
        "centers": [asdict(item) for item in centers],
        "signals": [asdict(item) for item in signals],
    }
