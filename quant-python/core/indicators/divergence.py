"""MACD divergence detection based on histogram segment area."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import pandas as pd


@dataclass(frozen=True)
class DivergenceResult:
    """背离检测结果。

    `detail` 会保留中间计算细节，便于调试和回测解释。
    """

    is_divergence: bool
    divergence_type: str
    detail: Dict

    def to_signal(self) -> str:
        """把结构化结果压缩成策略更常用的字符串标签。"""
        if not self.is_divergence:
            return "none"
        return self.divergence_type


class DivergenceDetector:
    """基于 MACD 柱面积分段的背离检测器。

    核心思路:
    - 先把 MACD 柱按正负切成连续 segment
    - 再比较最近两个 segment 的价格新高/新低和动能面积变化
    """

    def __init__(self, min_segment_length: int = 2):
        """初始化检测器。

        `min_segment_length` 用来过滤单根柱子形成的噪声 segment。
        """
        self.min_segment_length = max(int(min_segment_length), 1)

    def detect(
        self,
        price: pd.Series,
        macd_hist: pd.Series,
        divergence_type: str = "bullish",
    ) -> DivergenceResult:
        """检测指定方向的背离。"""
        if divergence_type not in {"bullish", "bearish"}:
            raise ValueError(f"Unsupported divergence type: {divergence_type}")

        price = pd.Series(price).reset_index(drop=True)
        macd_hist = pd.Series(macd_hist).reset_index(drop=True)

        # 背离至少需要两个连续动能段做比较。
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
        """依次检测 bullish / bearish，返回最终标签。"""
        bullish = self.detect(price, macd_hist, "bullish")
        if bullish.is_divergence:
            return "bullish"

        bearish = self.detect(price, macd_hist, "bearish")
        if bearish.is_divergence:
            return "bearish"

        return "none"

    def _calculate_macd_area(self, hist: pd.Series, start_idx: int, end_idx: int) -> float:
        """计算一个 MACD segment 的绝对面积。"""
        return float(hist.iloc[start_idx:end_idx + 1].abs().sum())

    def _find_macd_segments(self, hist: pd.Series, segment_type: str) -> List[Dict]:
        """按柱子正负号切分连续 segment。"""
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
        """只保留长度达标的 segment。"""
        return (segment["end"] - segment["start"] + 1) >= self.min_segment_length

    @staticmethod
    def _build_segment(hist: pd.Series, start_idx: int, end_idx: int, segment_type: str) -> Dict:
        """构建单个 segment，并记录局部峰值或谷值位置。"""
        segment = {"start": start_idx, "end": end_idx}
        window = hist.iloc[start_idx:end_idx + 1]
        if segment_type == "bullish":
            segment["trough"] = int(window.idxmin())
        else:
            segment["peak"] = int(window.idxmax())
        return segment
