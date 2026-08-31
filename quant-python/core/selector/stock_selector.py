"""Stock selector implementing the three-leg filtering model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import pandas as pd

from .fundamental import FundamentalEvaluation, evaluate_fundamental


@dataclass(frozen=True)
class StockSelectionRecord:
    """单只股票在某一轮选股中的结果。

    `data` 保留原始股票记录，方便后续阶段继续复用，不需要重新拼装。
    """

    ts_code: str
    name: str
    passed: bool
    score: int
    passed_checks: List[str]
    failed_reasons: List[str]
    data: Dict
    fundamental: Dict | None = None

    def to_dict(self) -> Dict:
        """转换成便于序列化和测试断言的字典。"""
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "passed": self.passed,
            "score": self.score,
            "passed_checks": self.passed_checks,
            "failed_reasons": self.failed_reasons,
            "data": self.data,
            "fundamental": self.fundamental or {},
        }


class StockSelector:
    """三条腿选股器。

    三条腿分别是:
    - fundamental: 基本面质量
    - turnover: 活跃度区间
    - volume_price: 量价结构是否具备启动基础
    """

    DEFAULT_CHECKS = ("fundamental", "turnover", "volume_price")

    def __init__(self, config: Dict | None = None):
        self.config = config or {}
        self.selector_config = self._resolve_selector_config(self.config)

    def select(self, stock_candidates, checks: tuple[str, ...] | None = None) -> Dict:
        """批量执行选股。

        `checks` 允许分阶段调用，例如只跑 fundamental 或 turnover。
        """
        records = self._to_records(stock_candidates)
        selected: List[Dict] = []
        rejected: List[Dict] = []

        for record in records:
            result = self.evaluate(record, checks=checks).to_dict()
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

    def evaluate(self, stock: Dict, checks: tuple[str, ...] | None = None) -> StockSelectionRecord:
        """评估单只股票是否通过指定检查项。"""
        active_checks = checks or self.DEFAULT_CHECKS
        check_handlers = {
            "fundamental": (self._check_fundamental, 40),
            "turnover": (self._check_turnover, 25),
            "volume_price": (self._check_volume_price_structure, 35),
        }
        passed_checks: List[str] = []
        failed_reasons: List[str] = []
        score = 0
        fundamental_evaluation: FundamentalEvaluation | None = None

        for check_name in active_checks:
            if check_name not in check_handlers:
                raise ValueError(f"Unknown selector check: {check_name}")

            check_handler, points = check_handlers[check_name]
            if check_name == "fundamental":
                fundamental_evaluation = self._evaluate_fundamental(stock)
                check_ok = fundamental_evaluation.passed
                detail = self._fundamental_detail(fundamental_evaluation)
            else:
                check_ok, detail = check_handler(stock)
            if check_ok:
                passed_checks.append(check_name)
                score += points
            else:
                failed_reasons.append(detail)

        return StockSelectionRecord(
            ts_code=stock.get("ts_code", ""),
            name=stock.get("name", ""),
            passed=len(passed_checks) == len(active_checks),
            score=score,
            passed_checks=passed_checks,
            failed_reasons=failed_reasons,
            data=stock,
            fundamental=(
                fundamental_evaluation.to_dict()
                if fundamental_evaluation is not None
                else None
            ),
        )

    def _evaluate_fundamental(self, stock: Dict) -> FundamentalEvaluation:
        context = str(stock.get("fundamental_context", "live"))
        return evaluate_fundamental(
            stock,
            self.config,
            as_of=stock.get("fundamental_as_of"),
            context=context,
        )

    @staticmethod
    def _fundamental_detail(evaluation: FundamentalEvaluation) -> str:
        if evaluation.status == "disabled":
            return "基本面筛选未启用"
        if evaluation.status == "unavailable":
            warning = evaluation.warnings[0] if evaluation.warnings else "数据不可用"
            return f"基本面数据不可用: {warning}"
        if evaluation.passed:
            return "基本面通过"
        labels = {
            "fundamental_roe_below_min": "ROE 低于阈值",
            "fundamental_debt_ratio_above_max": "负债率高于阈值",
            "fundamental_pe_out_of_range": "PE 不在可接受范围内",
            "fundamental_market_cap_out_of_range": "市值不在配置区间",
            "fundamental_data_unavailable": "基本面数据不可用",
        }
        return "；".join(labels.get(reason, reason) for reason in evaluation.reasons)

    def _check_fundamental(self, stock: Dict) -> tuple[bool, str]:
        """基本面检查。

        关键参数:
        - `roe_min`: 盈利能力下限
        - `debt_ratio_max`: 负债率上限
        - `pe_acceptable_max`: 估值上限
        - `market_cap_min/max`: 市值区间
        """
        evaluation = self._evaluate_fundamental(stock)
        return evaluation.passed, self._fundamental_detail(evaluation)

    def _check_turnover(self, stock: Dict) -> tuple[bool, str]:
        """换手率检查。

        `avg_turnover` 优先于单日 `turnover_rate`，因为它更能代表近期真实活跃度。
        """
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
        """量价结构检查。

        这里不直接判断“是否可以买”，而是判断是否具备继续进入策略路由的基础。
        """
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
        """兼容 DataFrame 和 list[dict] 两种输入格式。"""
        if isinstance(stock_candidates, pd.DataFrame):
            return stock_candidates.to_dict("records")
        return list(stock_candidates)

    @staticmethod
    def _resolve_selector_config(config: Dict) -> Dict:
        """解析 selector 配置。

        当前约定:
        - `strategy.fundamental` / `strategy.volume` 是基础阈值来源
        - `selector` 只覆盖 selector 自己特有或需要微调的参数
        """
        selector_cfg = (config or {}).get("selector", {})
        strategy_cfg = (config or {}).get("strategy", {})
        fundamental_cfg = strategy_cfg.get("fundamental", {})
        volume_cfg = strategy_cfg.get("volume", {})

        # 结构类参数放在 selector.*，基础阈值优先继承 strategy.*，兼容旧配置。
        return {
            "roe_min": selector_cfg.get("roe_min", fundamental_cfg.get("min_roe", 10)),
            "debt_ratio_max": selector_cfg.get("debt_ratio_max", fundamental_cfg.get("max_debt_ratio", 50)),
            "pe_excellent_max": selector_cfg.get("pe_excellent_max", 17),
            "pe_acceptable_max": selector_cfg.get("pe_acceptable_max", fundamental_cfg.get("max_pe", 30)),
            "market_cap_min": selector_cfg.get("market_cap_min", fundamental_cfg.get("min_market_cap", 50)),
            "market_cap_max": selector_cfg.get("market_cap_max", fundamental_cfg.get("max_market_cap", 500)),
            "turnover_rate_min": selector_cfg.get("turnover_rate_min", volume_cfg.get("min_turnover_rate", 1)),
            "turnover_rate_max": selector_cfg.get("turnover_rate_max", volume_cfg.get("max_turnover_rate", 3)),
            "volume_ratio_min": selector_cfg.get("volume_ratio_min", volume_cfg.get("volume_burst_ratio", 1.5)),
            "near_ma_threshold": selector_cfg.get("near_ma_threshold", 0.05),
            "price_change_soft_min": selector_cfg.get("price_change_soft_min", -0.03),
            "price_change_soft_max": selector_cfg.get("price_change_soft_max", 0.03),
        }
