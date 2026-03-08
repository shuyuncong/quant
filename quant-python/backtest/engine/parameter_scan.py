"""Limited parameter scan with in-sample and out-of-sample evaluation."""

from __future__ import annotations

from copy import deepcopy
from itertools import product
from typing import Any, Dict, List, Optional, Type

import pandas as pd


class ParameterScanner:
    """受限参数扫描器。

    它不是无限搜索，而是在组合数和样本内外差异上加 guardrail，
    目标是尽快找出“更稳健”的参数，而不是仅仅找最高收益。
    """

    def __init__(self, engine_cls, engine_config: Optional[Dict[str, Any]] = None):
        self.engine_cls = engine_cls
        self.engine_config = engine_config or {}
        backtest_cfg = self.engine_config.get("backtest", {})
        self.max_combinations = int(backtest_cfg.get("max_parameter_combinations", 24))
        self.max_gap = float(backtest_cfg.get("max_in_out_sample_gap", 0.20))

    def scan(
        self,
        price_data: pd.DataFrame,
        strategy_cls: Type,
        param_grid: Dict[str, List[Any]],
        base_config: Optional[Dict[str, Any]] = None,
        strategy_name: Optional[str] = None,
        split_ratio: float = 0.7,
        score_field: str = "annual_return",
        regime_scope: str = "all",
    ) -> Dict[str, Any]:
        """执行一次参数扫描，并输出稳健性比较结果。"""
        combinations = self._build_combinations(param_grid)
        in_sample, out_of_sample = self._split_price_data(price_data, split_ratio)
        results = []

        for params in combinations:
            effective_config = self._apply_params(base_config or {}, params)
            strategy = strategy_cls(effective_config)
            strategy_name = strategy_name or strategy_cls.__name__

            in_result = self._run_once(
                price_data=in_sample,
                strategy=strategy,
                config=effective_config,
                strategy_name=strategy_name,
                regime_scope=regime_scope,
            )
            out_result = self._run_once(
                price_data=out_of_sample,
                strategy=strategy_cls(effective_config),
                config=effective_config,
                strategy_name=strategy_name,
                regime_scope=regime_scope,
            ) if not out_of_sample.empty else None

            # 样本内高分但样本外失真太大，会被打上过拟合风险标记。
            in_score = float(in_result["summary"].get(score_field, 0.0) or 0.0)
            out_score = float(out_result["summary"].get(score_field, 0.0) or 0.0) if out_result else 0.0
            generalization_gap = in_score - out_score
            trade_count_gap = (
                int(in_result["summary"].get("trade_count", 0))
                - int(out_result["summary"].get("trade_count", 0))
            ) if out_result else int(in_result["summary"].get("trade_count", 0))

            risk_flags = []
            if abs(generalization_gap) > self.max_gap:
                risk_flags.append("generalization_gap_exceeded")
            if out_result and in_score > 0 and out_score < 0:
                risk_flags.append("out_of_sample_sign_flip")
            if out_result and int(out_result["summary"].get("trade_count", 0)) == 0:
                risk_flags.append("no_out_of_sample_trades")

            results.append(
                {
                    "params": params,
                    "in_sample": in_result["summary"],
                    "out_of_sample": out_result["summary"] if out_result else {},
                    "comparison": {
                        "score_field": score_field,
                        "in_sample_score": in_score,
                        "out_of_sample_score": out_score,
                        "generalization_gap": generalization_gap,
                        "trade_count_gap": trade_count_gap,
                        "robust_score": out_score - max(abs(generalization_gap) - self.max_gap, 0.0),
                        "risk_flags": risk_flags,
                        "is_stable": not risk_flags,
                    },
                }
            )

        results.sort(
            key=lambda item: (
                item["comparison"]["is_stable"],
                item["comparison"]["robust_score"],
                item["out_of_sample"].get(score_field, 0.0),
            ),
            reverse=True,
        )
        best = results[0] if results else None

        return {
            "parameter_grid": param_grid,
            "tested_combinations": len(results),
            "sample_split": {
                "split_ratio": split_ratio,
                "in_sample_rows": len(in_sample),
                "out_of_sample_rows": len(out_of_sample),
                "in_sample_start": self._stringify_index(in_sample, first=True),
                "in_sample_end": self._stringify_index(in_sample, first=False),
                "out_of_sample_start": self._stringify_index(out_of_sample, first=True),
                "out_of_sample_end": self._stringify_index(out_of_sample, first=False),
            },
            "guardrails": {
                "max_parameter_combinations": self.max_combinations,
                "max_in_out_sample_gap": self.max_gap,
                "score_field": score_field,
            },
            "comparisons": results,
            "best_params": best["params"] if best else {},
            "best_comparison": best["comparison"] if best else {},
        }

    def _run_once(
        self,
        price_data: pd.DataFrame,
        strategy,
        config: Dict[str, Any],
        strategy_name: str,
        regime_scope: str,
    ) -> Dict[str, Any]:
        """在给定参数下执行一次完整回测。"""
        engine = self.engine_cls(config=config)
        signals = strategy.generate_signals(price_data)
        return engine.run(
            price_data=price_data,
            signals=signals,
            strategy_name=strategy_name,
            regime_scope=regime_scope,
            config_snapshot=config,
        )

    def _build_combinations(self, param_grid: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
        """生成参数组合，并限制最大组合数。"""
        if not param_grid:
            return [{}]
        keys = list(param_grid.keys())
        values = [param_grid[key] for key in keys]
        combinations = []
        for index, combo in enumerate(product(*values)):
            if index >= self.max_combinations:
                break
            combinations.append(dict(zip(keys, combo)))
        return combinations

    @staticmethod
    def _split_price_data(price_data: pd.DataFrame, split_ratio: float) -> tuple[pd.DataFrame, pd.DataFrame]:
        """把数据切成样本内和样本外两段。"""
        if price_data.empty:
            raise ValueError("price_data must not be empty")
        ratio = min(max(split_ratio, 0.5), 0.9)
        split_index = max(int(len(price_data) * ratio), 1)
        in_sample = price_data.iloc[:split_index].copy()
        out_of_sample = price_data.iloc[split_index:].copy()
        return in_sample, out_of_sample

    @staticmethod
    def _apply_params(base_config: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """把点路径参数写回配置副本，例如 `strategy.ma_period=120`。"""
        effective_config = deepcopy(base_config)
        for key, value in params.items():
            current = effective_config
            segments = key.split(".")
            for segment in segments[:-1]:
                current = current.setdefault(segment, {})
            current[segments[-1]] = value
        return effective_config

    @staticmethod
    def _stringify_index(frame: pd.DataFrame, first: bool) -> Optional[str]:
        """把 DataFrame 边界时间转成可序列化字符串。"""
        if frame.empty:
            return None
        row = frame.iloc[0] if first else frame.iloc[-1]
        if "datetime" in frame.columns:
            value = row["datetime"]
        else:
            value = row.name
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)
