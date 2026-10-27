"""Read-only market-regime to strategy guidance for individual analysis.

The payload is intentionally informational: it never enables/disables a
strategy and never changes a signal.  It packages the 2026-10-26 regime study
into the analysis output so a reader can see which strategy historically fit
the current environment, and how weak that evidence is.
"""
from __future__ import annotations

import copy
from typing import Any

VERSION = "2026-10-26"
SOURCE = "docs/backtest-results/2026-10-26-bear-strategies/memo.md"

REGIME_LABELS = {
    "bull": "牛市/上行",
    "range": "震荡",
    "bear": "熊市/下行",
    "unknown": "环境未知",
}

_GUIDANCE: dict[str, dict[str, Any]] = {
    "bull": {
        "primary_strategy": "yearline_pullback",
        "primary_name": "年线趋势",
        "secondary_strategy": None,
        "secondary_name": None,
        "avoid_strategies": [],
        "avoid_names": [],
        "confidence": "low",
        "summary": (
            "牛市环境优先年线趋势；但 8 个 bull 窗口 0/8 个正 Sharpe CI，"
            "平均收益 −2.67%，只能作为低置信度参考。"
        ),
        "evidence": [
            "2020H2 +3.18%、2022 +0.35%、2024H2 +3.57%、2024Q4 +9.81%（4 笔已平仓 + 4 笔未平仓）",
            "2018H2 −19.40%、2019H2 −11.03%、2021H1 −4.30%、2024H1 −3.52%",
        ],
        "caveats": [
            "所有正收益窗口的 Sharpe 95% CI 都跨 0。",
            "2018–2022 样本只覆盖约 22%–27% 的当前上市池，无退市股。",
        ],
    },
    "range": {
        "primary_strategy": "macd_zero_axis",
        "primary_name": "日线零轴金叉",
        "secondary_strategy": None,
        "secondary_name": None,
        "avoid_strategies": [],
        "avoid_names": [],
        "confidence": "low",
        "summary": (
            "震荡环境优先日线零轴金叉；8 个 range 窗口 0/8 个正 Sharpe CI，"
            "平均收益 +1.31%，但 2022 range 单窗口亏 29.72%，稳定性不足。"
        ),
        "evidence": [
            "2020H2 +29.12%、2024Q4 +14.43%、2024H2 +13.04%、2019H2 +6.52%",
            "2022 −29.72%、2024H1 −13.60%、2018H2 −7.40%、2021H1 −1.88%",
        ],
        "caveats": [
            "所有正收益窗口的 Sharpe 95% CI 都跨 0。",
            "range 标签同时覆盖温和震荡与下跌中继，不能把 2022 与 2020H2 当成同一种环境。",
        ],
    },
    "bear": {
        "primary_strategy": "macd_divergence",
        "primary_name": "零轴＋底背离",
        "secondary_strategy": "macd_zero_axis",
        "secondary_name": "日线零轴金叉",
        "avoid_strategies": ["yearline_pullback"],
        "avoid_names": ["年线趋势"],
        "confidence": "low",
        "summary": (
            "bear 环境优先零轴＋底背离做防守；浅熊可看日线零轴金叉；"
            "年线趋势不适用于 bear。两者都没有正 Sharpe CI，不能当成稳定收益。"
        ),
        "evidence": [
            "深熊 2018H2：底背离 −3.67%、零轴 −23.77%、年线 −29.64%；2022：底背离 +12.40%、零轴 −14.54%、年线 −3.41%",
            "7 个 bear 窗口平均：零轴 +2.90%、底背离 +1.62%、年线 −6.34%；空仓基线 0%",
            "底背离最差回撤 23.77%，零轴 28.16%，年线 33.06%",
        ],
        "caveats": [
            "7 个 bear 窗口 0/7 个正 Sharpe CI；底背离 2018H2 只有 2 笔。",
            "2018–2022 样本只覆盖约 22%–27% 的当前上市池，无退市股；总收益含期末未平仓估值。",
            "深熊/浅熊分层尚未自动化，当前只用 MA10/MA20 的 bear 标签，不能区分 2018H2/2022 与 2019H2/2021H1。",
        ],
    },
    "unknown": {
        "primary_strategy": None,
        "primary_name": "",
        "secondary_strategy": None,
        "secondary_name": None,
        "avoid_strategies": [],
        "avoid_names": [],
        "confidence": "none",
        "summary": "市场环境未知，不给出策略适配建议；请先确认指数数据是否完整。",
        "evidence": [],
        "caveats": ["环境未知时不应用历史适配结论替代当前判断。"],
    },
}


def build_regime_guidance(regime: str | None) -> dict[str, Any]:
    """Return a JSON-serializable, read-only guidance block for one regime."""
    key = str(regime) if regime in _GUIDANCE else "unknown"
    payload = copy.deepcopy(_GUIDANCE[key])
    payload.update(
        {
            "version": VERSION,
            "regime": key,
            "regime_label": REGIME_LABELS[key],
            "source": SOURCE,
            "read_only": True,
            "disclaimer": "环境适配是历史统计参考，不是交易指令；不改变任何信号或仓位规则。",
        }
    )
    return payload
