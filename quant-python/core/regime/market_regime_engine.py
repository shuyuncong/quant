"""Market regime engine for bull, bear and range detection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import pandas as pd

from .regime_override import RegimeOverride


@dataclass(frozen=True)
class MarketRegimeDecision:
    """Structured regime decision output."""

    regime: str
    auto_regime: str
    final_regime: str
    scores: Dict[str, float]
    metrics: Dict[str, float]
    reasons: List[str]
    override: Dict[str, Optional[str]]

    def to_dict(self) -> Dict:
        return {
            "regime": self.regime,
            "auto_regime": self.auto_regime,
            "final_regime": self.final_regime,
            "scores": self.scores,
            "metrics": self.metrics,
            "reasons": self.reasons,
            "reason": "; ".join(self.reasons),
            "override": self.override,
        }


class MarketRegimeEngine:
    """Detects market regime from index price action and optional manual overrides."""

    def __init__(self, config: Optional[Dict] = None, data_fetcher=None):
        self.config = config or {}
        self.data_fetcher = data_fetcher
        self.regime_config = self.config.get("regime", {})

    def analyze_current_market(self) -> Dict:
        """Fetch current index data and return a regime decision."""
        if self.data_fetcher is None:
            raise ValueError("data_fetcher is required for analyze_current_market")

        index_code = self.regime_config.get("index_code", "000001.SH")
        ma_long = self.regime_config.get("ma_long", 250)
        period = max(ma_long + 30, self.regime_config.get("lookback_bars", 300))
        index_df = self.data_fetcher.get_index_daily(index_code, period=period)
        decision = self.decide(index_df)
        payload = decision.to_dict()
        payload["index_code"] = index_code
        return payload

    def decide(
        self,
        index_df: pd.DataFrame,
        override: Optional[RegimeOverride] = None,
    ) -> MarketRegimeDecision:
        """Decide current regime from index data."""
        if index_df is None or index_df.empty:
            raise ValueError("index_df must not be empty")

        metrics = self._calculate_metrics(index_df)
        scores, reasons = self._score_regimes(metrics)
        auto_regime = self._select_auto_regime(scores)

        override = override or RegimeOverride.from_config(self.config)
        override_result = override.apply(auto_regime)
        final_regime = override_result["final_regime"]

        if override_result["is_overridden"]:
            reasons.append(
                f"人工覆盖生效: {override_result['override_mode']} -> {final_regime}"
            )

        return MarketRegimeDecision(
            regime=final_regime,
            auto_regime=auto_regime,
            final_regime=final_regime,
            scores=scores,
            metrics=metrics,
            reasons=reasons,
            override=override_result,
        )

    def _calculate_metrics(self, index_df: pd.DataFrame) -> Dict[str, float]:
        close = index_df["close"].astype(float)
        ma_short_period = self.regime_config.get("ma_short", 20)
        ma_long_period = self.regime_config.get("ma_long", 250)
        slope_window = self.regime_config.get("slope_window", 20)

        if len(close) < ma_long_period:
            raise ValueError(
                f"index_df length {len(close)} is smaller than ma_long {ma_long_period}"
            )

        ma_short = close.rolling(ma_short_period).mean()
        ma_long = close.rolling(ma_long_period).mean()

        current_price = float(close.iloc[-1])
        current_ma_short = float(ma_short.iloc[-1])
        current_ma_long = float(ma_long.iloc[-1])
        previous_ma_long = (
            float(ma_long.iloc[-slope_window])
            if len(ma_long) >= slope_window
            else current_ma_long
        )

        price_vs_ma_long = self._safe_ratio(current_price - current_ma_long, current_ma_long)
        ma_short_vs_long = self._safe_ratio(current_ma_short - current_ma_long, current_ma_long)
        ma_long_slope = self._safe_ratio(
            current_ma_long - previous_ma_long,
            abs(previous_ma_long) or 1.0,
        )

        macd_line, signal_line, macd_hist = self._calculate_macd(close)

        return {
            "current_price": current_price,
            "ma_short": current_ma_short,
            "ma_long": current_ma_long,
            "price_vs_ma_long": price_vs_ma_long,
            "ma_short_vs_long": ma_short_vs_long,
            "ma_long_slope": ma_long_slope,
            "macd": macd_line,
            "macd_signal": signal_line,
            "macd_hist": macd_hist,
        }

    def _score_regimes(self, metrics: Dict[str, float]) -> tuple[Dict[str, float], List[str]]:
        bull_score = 0.0
        bear_score = 0.0
        range_score = 0.0
        reasons: List[str] = []

        price_vs_ma_long = metrics["price_vs_ma_long"]
        ma_short_vs_long = metrics["ma_short_vs_long"]
        ma_long_slope = metrics["ma_long_slope"]
        macd_hist = metrics["macd_hist"]

        if price_vs_ma_long >= 0.02:
            bull_score += 0.35
            reasons.append("价格位于长期均线上方")
        elif price_vs_ma_long <= -0.02:
            bear_score += 0.35
            reasons.append("价格位于长期均线下方")
        else:
            range_score += 0.10
            reasons.append("价格贴近长期均线")

        if ma_short_vs_long >= 0.01:
            bull_score += 0.25
            reasons.append("短期均线高于长期均线")
        elif ma_short_vs_long <= -0.01:
            bear_score += 0.25
            reasons.append("短期均线低于长期均线")
        else:
            range_score += 0.15
            reasons.append("短期均线与长期均线收敛")

        if ma_long_slope >= 0.01:
            bull_score += 0.20
            reasons.append("长期均线斜率向上")
        elif ma_long_slope <= -0.01:
            bear_score += 0.20
            reasons.append("长期均线斜率向下")
        else:
            range_score += 0.20
            reasons.append("长期均线斜率平缓")

        if macd_hist > 0:
            bull_score += 0.10
            reasons.append("MACD 柱体为正")
        elif macd_hist < 0:
            bear_score += 0.10
            reasons.append("MACD 柱体为负")

        if abs(price_vs_ma_long) <= 0.03:
            range_score += 0.20
        if abs(ma_short_vs_long) <= 0.015:
            range_score += 0.15
        if abs(ma_long_slope) <= 0.008:
            range_score += 0.10

        scores = {
            "bull": round(bull_score, 4),
            "bear": round(bear_score, 4),
            "range": round(range_score, 4),
        }
        return scores, reasons

    def _select_auto_regime(self, scores: Dict[str, float]) -> str:
        bull_threshold = self.regime_config.get("bull_score_threshold", 0.70)
        bear_threshold = self.regime_config.get("bear_score_threshold", 0.70)
        range_threshold = self.regime_config.get("range_score_threshold", 0.60)

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        top_regime, top_score = ranked[0]
        second_score = ranked[1][1]

        if top_regime == "bull" and top_score >= bull_threshold and top_score > second_score:
            return "bull"
        if top_regime == "bear" and top_score >= bear_threshold and top_score > second_score:
            return "bear"
        if top_regime == "range" and top_score >= range_threshold:
            return "range"

        return top_regime if top_score > second_score else "range"

    @staticmethod
    def _calculate_macd(close: pd.Series) -> tuple[float, float, float]:
        ema_fast = close.ewm(span=12, adjust=False).mean()
        ema_slow = close.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal_line
        return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(hist.iloc[-1])

    @staticmethod
    def _safe_ratio(numerator: float, denominator: float) -> float:
        if denominator == 0:
            return 0.0
        return float(numerator / denominator)
