"""Stock selector implementing the three-leg filtering model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import pandas as pd


@dataclass(frozen=True)
class StockSelectionRecord:
    """One stock selection outcome."""

    ts_code: str
    name: str
    passed: bool
    score: int
    passed_checks: List[str]
    failed_reasons: List[str]
    data: Dict

    def to_dict(self) -> Dict:
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "passed": self.passed,
            "score": self.score,
            "passed_checks": self.passed_checks,
            "failed_reasons": self.failed_reasons,
            "data": self.data,
        }


class StockSelector:
    """Filters stocks by fundamentals, turnover and volume-price structure."""

    def __init__(self, config: Dict | None = None):
        self.config = config or {}
        self.selector_config = self._resolve_selector_config(self.config)

    def select(self, stock_candidates) -> Dict:
        records = self._to_records(stock_candidates)
        selected: List[Dict] = []
        rejected: List[Dict] = []

        for record in records:
            result = self.evaluate(record).to_dict()
            if result["passed"]:
                selected.append(result)
            else:
                rejected.append(result)

        selected.sort(key=lambda item: item["score"], reverse=True)
        return {
            "selected": selected,
            "rejected": rejected,
            "candidate_pool": [item["ts_code"] for item in selected],
        }

    def evaluate(self, stock: Dict) -> StockSelectionRecord:
        passed_checks: List[str] = []
        failed_reasons: List[str] = []
        score = 0

        fundamental_ok, fundamental_detail = self._check_fundamental(stock)
        if fundamental_ok:
            passed_checks.append("fundamental")
            score += 40
        else:
            failed_reasons.append(fundamental_detail)

        turnover_ok, turnover_detail = self._check_turnover(stock)
        if turnover_ok:
            passed_checks.append("turnover")
            score += 25
        else:
            failed_reasons.append(turnover_detail)

        structure_ok, structure_detail = self._check_volume_price_structure(stock)
        if structure_ok:
            passed_checks.append("volume_price")
            score += 35
        else:
            failed_reasons.append(structure_detail)

        return StockSelectionRecord(
            ts_code=stock.get("ts_code", ""),
            name=stock.get("name", ""),
            passed=fundamental_ok and turnover_ok and structure_ok,
            score=score,
            passed_checks=passed_checks,
            failed_reasons=failed_reasons,
            data=stock,
        )

    def _check_fundamental(self, stock: Dict) -> tuple[bool, str]:
        cfg = self.selector_config
        roe = stock.get("roe")
        debt_ratio = stock.get("debt_ratio")
        pe = stock.get("pe")
        market_cap = stock.get("market_cap")

        if roe is None or roe < cfg["roe_min"]:
            return False, f"ROE 低于阈值({cfg['roe_min']})"
        if debt_ratio is None or debt_ratio > cfg["debt_ratio_max"]:
            return False, f"负债率高于阈值({cfg['debt_ratio_max']})"
        if pe is None or pe <= 0 or pe > cfg["pe_acceptable_max"]:
            return False, f"PE 不在可接受范围内(0,{cfg['pe_acceptable_max']}]"
        if market_cap is None or market_cap < cfg["market_cap_min"] or market_cap > cfg["market_cap_max"]:
            return False, f"市值不在区间[{cfg['market_cap_min']}, {cfg['market_cap_max']}]"
        return True, "基本面通过"

    def _check_turnover(self, stock: Dict) -> tuple[bool, str]:
        cfg = self.selector_config
        turnover = stock.get("avg_turnover", stock.get("turnover_rate"))
        if turnover is None:
            return False, "缺少换手率数据"
        if turnover < cfg["turnover_rate_min"]:
            return False, f"换手率低于阈值({cfg['turnover_rate_min']})"
        if turnover > cfg["turnover_rate_max"]:
            return False, f"换手率高于阈值({cfg['turnover_rate_max']})"
        return True, "换手率通过"

    def _check_volume_price_structure(self, stock: Dict) -> tuple[bool, str]:
        cfg = self.selector_config
        volume_ratio = stock.get("volume_ratio", 0.0)
        price_change_pct = stock.get("price_change_pct", 0.0)
        close_vs_ma_long = abs(stock.get("close_vs_ma_long", 0.0))
        ma_long_slope = stock.get("ma_long_slope", 0.0)
        divergence = stock.get("divergence", "none")
        bear_trap = stock.get("bear_trap", False)

        has_accumulation = (
            volume_ratio >= cfg["volume_ratio_min"]
            and cfg["price_change_soft_min"] <= price_change_pct <= cfg["price_change_soft_max"]
        )
        has_trend_support = ma_long_slope > 0 and close_vs_ma_long <= cfg["near_ma_threshold"]
        has_reversal_signal = divergence == "bullish" or bear_trap

        if has_accumulation and (has_trend_support or has_reversal_signal):
            return True, "量价结构通过"

        return False, "量价结构不足: 未同时满足放量、位置和反转条件"

    @staticmethod
    def _to_records(stock_candidates) -> List[Dict]:
        if isinstance(stock_candidates, pd.DataFrame):
            return stock_candidates.to_dict("records")
        return list(stock_candidates)

    @staticmethod
    def _resolve_selector_config(config: Dict) -> Dict:
        selector_cfg = (config or {}).get("selector", {})
        strategy_cfg = (config or {}).get("strategy", {})
        fundamental_cfg = strategy_cfg.get("fundamental", {})
        volume_cfg = strategy_cfg.get("volume", {})

        return {
            "roe_min": selector_cfg.get("roe_min", fundamental_cfg.get("min_roe", 10)),
            "debt_ratio_max": selector_cfg.get("debt_ratio_max", fundamental_cfg.get("max_debt_ratio", 50)),
            "pe_excellent_max": selector_cfg.get("pe_excellent_max", 17),
            "pe_acceptable_max": selector_cfg.get("pe_acceptable_max", fundamental_cfg.get("max_pe", 30)),
            "market_cap_min": selector_cfg.get("market_cap_min", fundamental_cfg.get("min_market_cap", 50)),
            "market_cap_max": selector_cfg.get("market_cap_max", fundamental_cfg.get("max_market_cap", 500)),
            "turnover_rate_min": selector_cfg.get("turnover_rate_min", volume_cfg.get("min_turnover_rate", 1)),
            "turnover_rate_max": selector_cfg.get("turnover_rate_max", 3),
            "volume_ratio_min": selector_cfg.get("volume_ratio_min", volume_cfg.get("volume_burst_ratio", 1.5)),
            "near_ma_threshold": selector_cfg.get("near_ma_threshold", 0.05),
            "price_change_soft_min": selector_cfg.get("price_change_soft_min", -0.03),
            "price_change_soft_max": selector_cfg.get("price_change_soft_max", 0.03),
        }
