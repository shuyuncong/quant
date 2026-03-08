"""MACD divergence detection based on histogram segment area."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import pandas as pd


@dataclass(frozen=True)
class DivergenceResult:
    """Structured divergence detection result."""

    is_divergence: bool
    divergence_type: str
    detail: Dict

    def to_signal(self) -> str:
        if not self.is_divergence:
            return "none"
        return self.divergence_type


class DivergenceDetector:
    """Detect bullish and bearish divergence by MACD histogram area."""

    def __init__(self, min_segment_length: int = 2):
        self.min_segment_length = max(int(min_segment_length), 1)

    def detect(
        self,
        price: pd.Series,
        macd_hist: pd.Series,
        divergence_type: str = "bullish",
    ) -> DivergenceResult:
        if divergence_type not in {"bullish", "bearish"}:
            raise ValueError(f"Unsupported divergence type: {divergence_type}")

        price = pd.Series(price).reset_index(drop=True)
        macd_hist = pd.Series(macd_hist).reset_index(drop=True)

        segments = self._find_macd_segments(macd_hist, divergence_type)
        if len(segments) < 2:
            return DivergenceResult(False, divergence_type, {"segments": segments})

        prev_segment = segments[-2]
        last_segment = segments[-1]
        prev_area = self._calculate_macd_area(macd_hist, prev_segment["start"], prev_segment["end"])
        last_area = self._calculate_macd_area(macd_hist, last_segment["start"], last_segment["end"])

        if divergence_type == "bullish":
            price_condition = price.iloc[last_segment["trough"]] < price.iloc[prev_segment["trough"]]
        else:
            price_condition = price.iloc[last_segment["peak"]] > price.iloc[prev_segment["peak"]]

        area_condition = last_area < prev_area
        detail = {
            "prev_area": prev_area,
            "last_area": last_area,
            "area_ratio": (last_area / prev_area) if prev_area > 0 else 0.0,
            "prev_segment": prev_segment,
            "last_segment": last_segment,
            "price_condition": price_condition,
            "area_condition": area_condition,
        }
        return DivergenceResult(price_condition and area_condition, divergence_type, detail)

    def classify(self, price: pd.Series, macd_hist: pd.Series) -> str:
        bullish = self.detect(price, macd_hist, "bullish")
        if bullish.is_divergence:
            return "bullish"

        bearish = self.detect(price, macd_hist, "bearish")
        if bearish.is_divergence:
            return "bearish"

        return "none"

    def _calculate_macd_area(self, hist: pd.Series, start_idx: int, end_idx: int) -> float:
        return float(hist.iloc[start_idx:end_idx + 1].abs().sum())

    def _find_macd_segments(self, hist: pd.Series, segment_type: str) -> List[Dict]:
        segments: List[Dict] = []
        in_segment = False
        segment_start = None

        def matches(value: float) -> bool:
            return value < 0 if segment_type == "bullish" else value > 0

        for idx, value in enumerate(hist):
            if matches(value):
                if not in_segment:
                    in_segment = True
                    segment_start = idx
                continue

            if in_segment:
                segment = self._build_segment(hist, segment_start, idx - 1, segment_type)
                if self._is_valid_segment(segment):
                    segments.append(segment)
                in_segment = False

        if in_segment:
            segment = self._build_segment(hist, segment_start, len(hist) - 1, segment_type)
            if self._is_valid_segment(segment):
                segments.append(segment)

        return segments

    def _is_valid_segment(self, segment: Dict) -> bool:
        return (segment["end"] - segment["start"] + 1) >= self.min_segment_length

    @staticmethod
    def _build_segment(hist: pd.Series, start_idx: int, end_idx: int, segment_type: str) -> Dict:
        segment = {"start": start_idx, "end": end_idx}
        window = hist.iloc[start_idx:end_idx + 1]
        if segment_type == "bullish":
            segment["trough"] = int(window.idxmin())
        else:
            segment["peak"] = int(window.idxmax())
        return segment
