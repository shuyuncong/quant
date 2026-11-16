#!/usr/bin/env python3
"""Build the unified HTML backtest hub.

Scans a curated set of backtest result directories, normalizes their equity
curves to a 200k initial capital over 2020-01-02..2026-10-30, and writes a
self-contained hub plus one HTML page per comparison. Legacy directories are
indexed in a catalog and get a small detail page.

Usage:
    quant-python/.venv/bin/python scripts/build_backtest_hub.py
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "docs" / "backtest-results"
HUB = RESULTS / "2026-11-15-backtest-hub"
START = pd.Timestamp("2020-01-02")
END = pd.Timestamp("2026-10-30")
INITIAL = 200_000.0
COLORS = ["c1", "c2", "c3", "c4", "c5", "c6"]


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig")


def _date_index(df: pd.DataFrame) -> pd.DataFrame:
    col = df.columns[0]
    df = df.copy()
    df[col] = pd.to_datetime(df[col], errors="coerce")
    df = df.dropna(subset=[col]).set_index(col).sort_index()
    return df


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def normalize(series: pd.Series, start: pd.Timestamp = START, end: pd.Timestamp = END) -> pd.Series:
    s = _numeric(series).dropna()
    s.index = pd.to_datetime(s.index)
    s = s[(s.index >= start) & (s.index <= end)]
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if len(s) < 2:
        return s
    return s / float(s.iloc[0]) * INITIAL


def points(series: pd.Series) -> list[list[Any]]:
    return [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in series.items()]


def weekly(series: pd.Series) -> pd.Series:
    if len(series) < 2:
        return series
    w = series.resample("W-FRI").last().dropna()
    if len(w) and series.index[0] < w.index[0]:
        w = pd.concat([series.iloc[[0]], w])
    return w[~w.index.duplicated(keep="last")]


def curve_metrics(series: pd.Series) -> dict[str, Any]:
    if len(series) < 2:
        return {
            "total_return": None,
            "annualized_return": None,
            "max_drawdown": None,
            "sharpe": None,
            "volatility": None,
            "win_rate": None,
            "profit_factor": None,
            "final_equity": float(series.iloc[-1]) if len(series) else None,
            "annual": {},
            "annual_drawdown": {},
        }
    returns = series.pct_change().dropna()
    total = float(series.iloc[-1] / series.iloc[0] - 1.0)
    periods = len(series) - 1
    annualized = (1.0 + total) ** (252.0 / periods) - 1.0
    vol = float(returns.std(ddof=1) * math.sqrt(252.0)) if len(returns) > 1 else 0.0
    sharpe = (
        float(returns.mean() / returns.std(ddof=1) * math.sqrt(252.0))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else None
    )
    drawdown = float((series / series.cummax() - 1.0).min())
    positive = returns[returns > 0]
    negative = returns[returns < 0]
    daily_pf = (
        float(positive.sum() / abs(negative.sum()))
        if len(positive) and len(negative)
        else None
    )
    annual: dict[str, float] = {}
    annual_drawdown: dict[str, float] = {}
    previous = float(series.iloc[0])
    for year, group in series.groupby(series.index.year):
        end = float(group.iloc[-1])
        annual[str(int(year))] = end / previous - 1.0
        path = pd.concat(
            [pd.Series([previous], index=[group.index[0] - pd.Timedelta(days=1)]), group]
        )
        annual_drawdown[str(int(year))] = float((path / path.cummax() - 1.0).min())
        previous = end
    return {
        "total_return": total,
        "annualized_return": float(annualized),
        "max_drawdown": drawdown,
        "sharpe": sharpe,
        "volatility": vol,
        "win_rate": float((returns > 0).mean()),
        "profit_factor": daily_pf,
        "final_equity": float(series.iloc[-1]),
        "annual": annual,
        "annual_drawdown": annual_drawdown,
    }


def trade_metrics(
    trades: pd.DataFrame | None,
    date_col: str = "entry_date",
    return_col: str = "net_return",
    pnl_col: str | None = "net_pnl",
    hold_col: str | None = "hold_days",
    return_scale: float = 1.0,
) -> dict[str, Any]:
    if trades is None or trades.empty:
        return {}
    df = trades.copy()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df[(df[date_col] >= START) & (df[date_col] <= END)]
    returns = _numeric(df[return_col]).dropna() * return_scale
    if returns.empty:
        return {}
    if pnl_col and pnl_col in df.columns:
        pnl = _numeric(df[pnl_col]).dropna()
        positive = pnl[pnl > 0]
        negative = pnl[pnl < 0]
        profit_factor = (
            float(positive.sum() / abs(negative.sum()))
            if len(positive) and len(negative)
            else None
        )
    else:
        positive = returns[returns > 0]
        negative = returns[returns < 0]
        profit_factor = (
            float(positive.sum() / abs(negative.sum()))
            if len(positive) and len(negative)
            else None
        )
    avg_hold = (
        float(_numeric(df[hold_col]).dropna().mean())
        if hold_col and hold_col in df.columns
        else None
    )
    return {
        "win_rate": float((returns > 0).mean()),
        "profit_factor": profit_factor,
        "trades": int(len(returns)),
        "average_hold_days": avg_hold,
    }


def _wide(path: Path, columns: list[str]) -> dict[str, pd.Series]:
    df = _date_index(_read_csv(path))
    return {column: normalize(df[column]) for column in columns if column in df.columns}


def _long(path: Path, strategy_col: str, value_col: str, strategies: list[str]) -> dict[str, pd.Series]:
    df = _read_csv(path)
    date_col = df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col])
    out: dict[str, pd.Series] = {}
    for name in strategies:
        sub = df[df[strategy_col] == name].set_index(date_col)[value_col]
        out[name] = normalize(sub)
    return out


def _strat(
    name: str,
    description: str,
    series: pd.Series,
    color: str,
    basis: str = "trade",
    trade_stats: dict[str, Any] | None = None,
    metrics_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cm = curve_metrics(series)
    metrics = {
        "total_return": cm["total_return"],
        "annualized_return": cm["annualized_return"],
        "max_drawdown": cm["max_drawdown"],
        "sharpe": cm["sharpe"],
        "volatility": cm["volatility"],
        "win_rate": cm["win_rate"] if basis == "daily" else None,
        "profit_factor": cm["profit_factor"] if basis == "daily" else None,
        "trades": None,
        "average_hold_days": None,
        "final_equity": cm["final_equity"],
    }
    if trade_stats:
        for key in ("win_rate", "profit_factor", "trades", "average_hold_days"):
            if key in trade_stats:
                metrics[key] = trade_stats[key]
    if metrics_override:
        for key, value in metrics_override.items():
            if value is not None:
                metrics[key] = value
    return {
        "name": name,
        "description": description,
        "color": color,
        "basis": basis,
        "curve": points(series),
        "curve_weekly": points(weekly(series)),
        "metrics": metrics,
        "annual": cm["annual"],
        "annual_drawdown": cm["annual_drawdown"],
        "start": series.index[0].strftime("%Y-%m-%d") if len(series) else None,
        "end": series.index[-1].strftime("%Y-%m-%d") if len(series) else None,
    }


def _comparison(
    cid: str,
    category: str,
    title: str,
    subtitle: str,
    note: str,
    metric_note: str,
    strategies: list[dict[str, Any]],
    source_dir: str,
    source_files: list[str],
) -> dict[str, Any]:
    return {
        "id": cid,
        "category": category,
        "title": title,
        "subtitle": subtitle,
        "note": note,
        "metric_note": metric_note,
        "strategies": strategies,
        "source_dir": source_dir,
        "source_files": source_files,
    }


def build_first_board_full_vs_third() -> dict[str, Any]:
    base_dir = RESULTS / "2026-11-14-first-board-four-strategies"
    cap_dir = RESULTS / "2026-11-15-first-board-low-open-maxweight33"
    base = _long(base_dir / "curves.csv", "strategy", "equity", ["s1_first_board_low_open"])[
        "s1_first_board_low_open"
    ]
    cap = _long(cap_dir / "curves.csv", "strategy", "equity", ["s1_first_board_low_open"])[
        "s1_first_board_low_open"
    ]
    base_trades = _read_csv(base_dir / "trades.csv")
    base_trades = base_trades[base_trades["strategy"] == "s1_first_board_low_open"]
    cap_trades = _read_csv(cap_dir / "trades.csv")
    return _comparison(
        "first-board-low-open-full-vs-third",
        "first-board",
        "首板低开：满仓 vs 单票≤1/3",
        "同一选股、同一卖出规则，仅仓位不同；2020-01-02 起，初始 20 万。",
        "满仓版只在空仓时买入，单只候选可占 100%；1/3 版把单票上限压到总资产 1/3。"
        "两版本交易集合相同，差异只来自仓位和复利。",
        "股票策略为交易口径：胜率=盈利交易占比，盈亏比=总盈利/总亏损；交易数按 2020 年后入场统计。",
        [
            _strat(
                "首板低开·满仓版",
                "源码原版：昨日首板、今日低开 3-4%、60 日相对位置≤50%；"
                "空仓时把总资产按当日候选数等分，只有 1 只候选时单票满仓；"
                "次日 11:28 有浮盈止盈、14:50 清仓。",
                base,
                COLORS[0],
                trade_stats=trade_metrics(base_trades),
            ),
            _strat(
                "首板低开·单票≤1/3",
                "同一选股与卖出规则；买入时单票不超过当前总资产 1/3。"
                "1 只候选只投 1/3，2 只各 1/3，3 只以上恢复等分；持有期不再平衡。",
                cap,
                COLORS[1],
                trade_stats=trade_metrics(cap_trades),
            ),
        ],
        "2026-11-14-first-board-four-strategies / 2026-11-15-first-board-low-open-maxweight33",
        ["2026-11-14-first-board-four-strategies/memo.md", "2026-11-15-first-board-low-open-maxweight33/memo.md"],
    )


def build_first_board_four() -> dict[str, Any]:
    source = RESULTS / "2026-11-14-first-board-four-strategies"
    names = [
        "s1_first_board_low_open",
        "s2_consecutive_leader",
        "s7_chase_first_board",
        "s13_mixed",
    ]
    curves = _long(source / "curves.csv", "strategy", "equity", names)
    raw = _read_csv(source / "curves.csv")
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw = raw.dropna(subset=["date"])
    curves["benchmark"] = normalize(
        raw[raw["strategy"] == "s1_first_board_low_open"].set_index("date")["benchmark"]
    )
    trades = _read_csv(source / "trades.csv")
    descriptions = {
        "s1_first_board_low_open": (
            "首板低开：昨日首板、今日低开 3-4%、60 日相对位置≤50%；"
            "空仓时按候选等分，次日 11:28 有浮盈止盈、14:50 清仓。"
        ),
        "s2_consecutive_leader": (
            "连板龙头：昨日最高连板（≥2 板），按 5 日平均换手率升序取最多 10 只；"
            "T+1 有盈利或 T+2 收盘卖出；单票 10%、最多 10 仓；涨停持有、跌停顺延。"
        ),
        "s7_chase_first_board": (
            "追首板涨停：主板首板、次日高开 0-6%、成交额 7-19 亿、流通市值 70-520 亿、左压放量；"
            "可用资金等分买入，次日收盘前清仓。"
        ),
        "s13_mixed": (
            "高开/低开/弱转强混合：首板高开、首板低开、昨日触板未封三个桶；"
            "按当日候选等分买入；止盈（高于成本且未涨停）或跌破 MA5 止损。"
        ),
        "benchmark": "等权主板+创业板，每日再平衡，无成本，作为市场参照。",
    }
    labels = {
        "s1_first_board_low_open": "1 首板低开",
        "s2_consecutive_leader": "2 连板龙头",
        "s7_chase_first_board": "7 追首板涨停",
        "s13_mixed": "13 高开/低开/弱转强",
        "benchmark": "等权 A 股基准",
    }
    strategies = []
    for idx, name in enumerate(names):
        strategies.append(
            _strat(
                labels[name],
                descriptions[name],
                curves[name],
                COLORS[idx],
                trade_stats=trade_metrics(trades[trades["strategy"] == name]),
            )
        )
    strategies.append(
        _strat(
            labels["benchmark"],
            descriptions["benchmark"],
            curves["benchmark"],
            COLORS[4],
            basis="daily",
        )
    )
    return _comparison(
        "first-board-four-strategies",
        "first-board",
        "首板 / 连板四策略",
        "四个聚宽首板/连板策略的日线机械复现；2020-01-02 起，初始 20 万。",
        "日线复现缺少集合竞价量、盘中 11:25/14:50 价格和历史 ST 名单；"
        "策略 2/7/13 对执行时点尤其敏感，不能把这里的负收益直接等同于源码失效。",
        "股票策略为交易口径；基准为日频口径。"
        "源码标题收益（如 304%、1138.61%）不可直接比较。",
        strategies,
        "2026-11-14-first-board-four-strategies",
        ["2026-11-14-first-board-four-strategies/memo.md", "2026-11-14-first-board-four-strategies/summary.json"],
    )


def build_first_board_pullback() -> dict[str, Any]:
    source = RESULTS / "2026-11-10-first-board-pullback"
    strategies = []
    for idx, hold in enumerate([1, 3, 10]):
        sub = source / f"hold{hold}"
        curve = normalize(_date_index(_read_csv(sub / "portfolio_curve.csv"))["equity"])
        trades = _read_csv(sub / "trades.csv")
        stats = trade_metrics(
            trades,
            return_col="net_return_pct",
            pnl_col=None,
            hold_col="hold_days",
            return_scale=0.01,
        )
        strategies.append(
            _strat(
                f"持有 {hold} 个交易日",
                "首板放量涨停后缩量回调：回调日收盘确认，次日开盘买；"
                f"最多持有 {hold} 个交易日；-5% 止损/+10% 止盈；20 只×5% 仓位。",
                curve,
                COLORS[idx],
                trade_stats=stats,
            )
        )
    return _comparison(
        "first-board-pullback-holds",
        "first-board",
        "首板放量回调：持有 1 / 3 / 10 日",
        "同一事件与入场规则，只改变最长持有期；2020-01-02 起，初始 20 万。",
        "事件研究 + 组合回测；持有期越长，交易次数越少，单笔尾部风险越大。"
        "止损/止盈触发后次日开盘卖出，跌停顺延。",
        "股票策略为交易口径；盈亏比由交易净收益计算（无逐笔金额时按等权近似）。",
        strategies,
        "2026-11-10-first-board-pullback",
        ["2026-11-10-first-board-pullback/memo.md", "2026-11-10-first-board-pullback/summary.json"],
    )


def build_open_gap() -> dict[str, Any]:
    source = RESULTS / "2026-11-14-open-gap-momentum"
    strategy_curve = normalize(_date_index(_read_csv(source / "portfolio_curve.csv"))["equity"])
    curves = _wide(source / "curves.csv", ["strategy", "ew_all_a", "index_399101"])
    trades = _read_csv(source / "trades_open_1.csv")
    stats = trade_metrics(trades, return_col="net_return", pnl_col=None, hold_col="hold_sessions")
    return _comparison(
        "open-gap-momentum",
        "open-gap",
        "开盘缺口动量：负期望案例",
        "开盘涨幅≤4%、昨日涨幅≤6%、前20日涨幅≥0%；按开盘涨幅降序每天最多 2 只；2020-01-02 起，初始 20 万。",
        "主口径为当日开盘买、次日开盘卖；实际整手 2 槽组合从 2019 年 100 万跌到 2026-10-30 的 536 元。"
        "本页按 2020-01-02 起算：20 万缩放到约 5.75 万（2020 年初实际净值已仅剩 1,863 元/100 万）。"
        "策略曲线接近归零，图表仅用于展示亏损路径。",
        "策略为交易口径；两条基准为日频口径。该策略在本地数据上没有正期望，是负面对照。",
        [
            _strat(
                "开盘缺口策略（T+1 开盘卖）",
                "开盘涨幅≤4%、昨日涨幅≤6%、前20日涨幅≥0%；按开盘涨幅降序取前 2；"
                "当日开盘买、次日开盘卖；2 个半仓槽，100 股整手。",
                strategy_curve,
                COLORS[0],
                trade_stats=stats,
            ),
            _strat(
                "全 A 等权基准",
                "全 A 等权、每日再平衡、开盘到开盘、无成本。",
                curves["ew_all_a"],
                COLORS[1],
                basis="daily",
            ),
            _strat(
                "中小板综指 399101",
                "中小板综指买入持有，收盘到收盘，仅作参照。",
                curves["index_399101"],
                COLORS[2],
                basis="daily",
            ),
        ],
        "2026-11-14-open-gap-momentum",
        ["2026-11-14-open-gap-momentum/memo.md", "2026-11-14-open-gap-momentum/summary.json"],
    )


def build_etf_return_sleeve() -> dict[str, Any]:
    source = RESULTS / "2026-11-13-etf-return-sleeve"
    names = ["baseline", "E1_cash_sleeve", "N3_carve10", "E1N3_cash_carve10"]
    curves = _wide(source / "curves.csv", names)
    descriptions = {
        "baseline": "ETF 生产基准：40% 核心 + 卫星≤35% + 回填 35%，10% 目标波动，周度调仓，5bp 滑点。",
        "E1_cash_sleeve": "把策略闲置权重放入货币 ETF 511880，保留 2% 现金缓冲。",
        "N3_carve10": "10% 纳指 carve（513100），其余缩放到 90%，总敞口不增加。",
        "E1N3_cash_carve10": "E1 现金 sleeve + 10% 纳指 carve 的组合。",
    }
    labels = {
        "baseline": "ETF 生产基准",
        "E1_cash_sleeve": "E1 现金 sleeve",
        "N3_carve10": "N3 10% 纳指 carve",
        "E1N3_cash_carve10": "E1 + 10% 纳指 carve",
    }
    strategies = [
        _strat(labels[name], descriptions[name], curves[name], COLORS[idx], basis="daily")
        for idx, name in enumerate(names)
    ]
    return _comparison(
        "etf-return-sleeve",
        "etf",
        "ETF 收益增强：现金 sleeve 与纳指 carve",
        "2020-01-02 起，初始 20 万（原回测 25 万，按比例缩放）；周度调仓。",
        "ETF 配置策略没有离散交易胜率；表中胜率/盈亏比为日频口径。"
        "N3 单独未通过验收，E1+N3 组合通过但依赖 2024-2026 纳指强势。",
        "全部为日频口径：胜率=正收益交易日占比，盈亏比=正日收益之和/负日收益绝对值之和。",
        strategies,
        "2026-11-13-etf-return-sleeve",
        ["2026-11-13-etf-return-sleeve/memo.md", "2026-11-13-etf-return-sleeve/metrics.csv"],
    )


def build_etf_rsrs() -> dict[str, Any]:
    source = RESULTS / "2026-11-14-etf-rsrs-slope"
    names = ["baseline", "S1_slope_r2_25", "R1_rsrs_gate", "R2_rsrs_and_ma200"]
    curves = _wide(source / "curves.csv", names)
    descriptions = {
        "baseline": "ETF 生产基准：40% 核心 + 卫星≤35% + 回填 35%，10% 目标波动，周度调仓。",
        "S1_slope_r2_25": "卫星排序改为 25 日对数价格回归的年化斜率×R²，其余不变。",
        "R1_rsrs_gate": "用 RSRS 替换 MA200 市场闸门；高于 +0.7 开、低于 -0.7 关、中间保持。",
        "R2_rsrs_and_ma200": "RSRS 与 MA200 同时为开才开闸。",
    }
    labels = {
        "baseline": "ETF 生产基准",
        "S1_slope_r2_25": "S1 斜率×R² 25 日",
        "R1_rsrs_gate": "R1 RSRS 闸门",
        "R2_rsrs_and_ma200": "R2 RSRS + MA200",
    }
    strategies = [
        _strat(labels[name], descriptions[name], curves[name], COLORS[idx], basis="daily")
        for idx, name in enumerate(names)
    ]
    return _comparison(
        "etf-rsrs-slope",
        "etf",
        "ETF 择时：斜率×R² 与 RSRS",
        "2020-01-02 起，初始 20 万（原回测 25 万，按比例缩放）；周度调仓。",
        "5 条可选/敏感性臂全部被拒绝；这里展示 baseline 与 3 条代表臂。"
        "全期 Sharpe 差全部为负，证据是“没有改善”，不是“显著更差”。",
        "全部为日频口径：胜率=正收益交易日占比，盈亏比=正日收益之和/负日收益绝对值之和。",
        strategies,
        "2026-11-14-etf-rsrs-slope",
        ["2026-11-14-etf-rsrs-slope/memo.md", "2026-11-14-etf-rsrs-slope/metrics.csv"],
    )


def _pct(value: str | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.replace("%", "").replace("−", "-").replace(",", "").strip()
        if not text or text in {"-", "—"}:
            return None
        try:
            return float(text) / 100.0
        except ValueError:
            return None
    return float(value)


def build_smallcap_three() -> dict[str, Any]:
    source = RESULTS / "2026-11-06-three-strategies"
    curves = _wide(source / "three_strategies_curves.csv", ["quality_baseline", "etf_benchmark", "smallcap_baseline"])
    metrics = _read_csv(source / "metrics.csv")
    metrics["strategy"] = metrics["strategy"].astype(str)
    metric_map = {row["strategy"]: row for _, row in metrics.iterrows()}
    labels = {
        "quality_baseline": "质量 A/B baseline",
        "etf_benchmark": "ETF 生产基准",
        "smallcap_baseline": "小市值研究基线",
    }
    descriptions = {
        "quality_baseline": "当前中证 1000 快照池；100→30 最小市值；月度；无质量过滤；"
        "2026 快照有选择/幸存者偏差。",
        "etf_benchmark": "ETF 生产基准：40% 核心 + 卫星≤35% + 回填 35%；10% 目标波动；周度。",
        "smallcap_baseline": "PIT 排名 801-1800；严格 50 池；静态 30；周度；无止损。",
    }
    rows = {
        "quality_baseline": "质量 A/B baseline",
        "etf_benchmark": "ETF 生产基准",
        "smallcap_baseline": "小市值研究基线",
    }
    strategies = []
    for idx, name in enumerate(["quality_baseline", "etf_benchmark", "smallcap_baseline"]):
        row = metric_map.get(rows[name])
        override = {}
        if row is not None:
            override = {
                "win_rate": _pct(row.get("win_rate")),
                "profit_factor": float(row["payoff"]) if pd.notna(row.get("payoff")) else None,
            }
        strategies.append(
            _strat(
                labels[name],
                descriptions[name],
                curves[name],
                COLORS[idx],
                basis="trade",
                metrics_override=override,
            )
        )
    return _comparison(
        "smallcap-three-strategies",
        "smallcap",
        "小市值 / 质量三策略（含选择偏差）",
        "2020-01-02 起，初始 20 万；曲线原为 100 起点，按比例缩放。",
        "质量 baseline 使用 2026 中证 1000 快照回测历史，存在幸存者/选择偏差；"
        "PIT 校正后收益大幅下降。这里保留原始对比，同时提供 PIT 校正页。",
        "胜率与盈亏比来自 metrics.csv（交易口径，盈亏比为平均盈利/平均亏损）；"
        "没有逐笔交易数，交易数显示为 —。",
        strategies,
        "2026-11-06-three-strategies",
        ["2026-11-06-three-strategies/index.html", "2026-11-06-three-strategies/metrics.csv"],
    )


def build_pit_quality_vs_smallcap() -> dict[str, Any]:
    quality_dir = RESULTS / "2026-11-06-pit-quality-baseline"
    small_dir = RESULTS / "2026-11-06-pit-smallcap"
    quality_curve = normalize(_date_index(_read_csv(quality_dir / "curves.csv"))["equity"])
    small_wide = _date_index(_read_csv(small_dir / "curves.csv"))
    small_curve = normalize(small_wide["v1_static30_strict50"])
    quality_summary = json.loads((quality_dir / "summary.json").read_text(encoding="utf-8"))
    small_summary = json.loads((small_dir / "summary.json").read_text(encoding="utf-8"))
    qm = quality_summary["metrics"]
    sm = small_summary["variants"]["v1_static30_strict50"]
    quality_override = {
        "win_rate": qm.get("trade_win_rate_pct", 0) / 100.0 if qm.get("trade_win_rate_pct") is not None else None,
        "profit_factor": qm.get("payoff"),
        "trades": qm.get("closed_trades"),
    }
    small_override = {
        "win_rate": sm.get("trade_win_rate_pct", 0) / 100.0 if sm.get("trade_win_rate_pct") is not None else None,
        "profit_factor": sm.get("payoff"),
        "trades": sm.get("closed_trades"),
    }
    return _comparison(
        "pit-quality-vs-smallcap",
        "smallcap",
        "PIT 校正：质量 vs 小市值",
        "把退市股补进全 A 后的 PIT 口径；2020-01-02 起，初始 20 万。",
        "这是比三策略快照更可信的口径：质量 baseline 年化约 6.7%（252 日折算；memo 日历 CAGR 6.4%）、"
        "最大回撤约 -48.5%；严格 50 小市值 baseline 年化为负。不要用快照回测的 31% 年化做决策。",
        "胜率/盈亏比来自 summary.json（交易口径，盈亏比为平均盈利/平均亏损）；"
        "交易数为 closed_trades。",
        [
            _strat(
                "PIT 质量 baseline",
                "PIT 全 A 池；100→30 最小市值；月度；无质量过滤；补退市股。",
                quality_curve,
                COLORS[0],
                metrics_override=quality_override,
            ),
            _strat(
                "PIT 小市值 strict50",
                "PIT 排名 801-1800；严格 50 池；静态 30；周度；无止损；补退市股。",
                small_curve,
                COLORS[1],
                metrics_override=small_override,
            ),
        ],
        "2026-11-06-pit-quality-baseline / 2026-11-06-pit-smallcap",
        ["2026-11-06-pit-quality-baseline/summary.json", "2026-11-06-pit-smallcap/summary.json"],
    )


CATEGORIES = [
    {"id": "best", "name": "家族最优"},
    {"id": "state", "name": "走势状态机"},
    {"id": "first-board", "name": "首板 / 连板"},
    {"id": "open-gap", "name": "开盘缺口 / 动量"},
    {"id": "etf", "name": "ETF 配置"},
    {"id": "smallcap", "name": "小市值 / 质量"},
]


def category_for(name: str) -> str:
    if "first-board" in name:
        return "首板 / 连板"
    if "open-gap" in name:
        return "开盘缺口 / 动量"
    if "etf" in name:
        return "ETF 配置"
    if any(key in name for key in ("smallcap", "quality", "pit", "three-strategies", "simple-vs-etf")):
        return "小市值 / 质量"
    if "nasdaq" in name:
        return "海外 / 其他"
    return "其他 / 归档"


def build_catalog(rich_by_dir: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(RESULTS.iterdir(), key=lambda p: p.name, reverse=True):
        if not path.is_dir() or path.name == HUB.name:
            continue
        files = []
        for candidate in ("memo.md", "report.md", "README.md", "summary.json", "metrics.csv", "curves.csv", "index.html", "compare.html"):
            file_path = path / candidate
            if file_path.exists():
                files.append({"label": candidate, "path": f"{path.name}/{candidate}"})
        rows.append(
            {
                "dir": path.name,
                "date": path.name[:10],
                "category": category_for(path.name),
                "files": files,
                "page": f"catalog/{path.name}/index.html",
                "rich_id": rich_by_dir.get(path.name),
            }
        )
    return rows
def build_state_machine() -> dict[str, Any]:
    source = RESULTS / "2026-11-16-first-board-state-machine"
    raw = pd.read_csv(source / "curves.csv")
    raw["date"] = pd.to_datetime(raw["date"])
    curves = raw.pivot(index="date", columns="strategy", values="equity")
    trades = pd.read_csv(source / "trades.csv", dtype={"symbol": str})
    labels = {
        "base": "状态机·全部",
        "skip_turn_down": "跳过 turn_down",
        "only_down": "仅 down 状态",
        "skip_up": "跳过 up 状态",
        "half_down": "down 状态半仓",
    }
    descriptions = {
        "base": "等权 A 股状态机不筛选；T-1 状态，T 开盘买、T+1 收盘卖。",
        "skip_turn_down": "跳过 turn_down（高位转弱）状态；其余同基线。",
        "only_down": "只在 down（下跌趋势）状态交易，其余时间空仓。",
        "skip_up": "跳过 up（上涨趋势）状态；其余同基线。",
        "half_down": "down 状态只投 50% 现金，其余状态满仓。",
    }
    strategies = []
    for idx, name in enumerate(["base", "skip_turn_down", "only_down", "skip_up", "half_down"]):
        stats = trade_metrics(
            trades[trades["strategy"] == name],
            date_col="entry_date",
            return_col="net_return",
            pnl_col=None,
            hold_col=None,
        )
        strategies.append(
            _strat(
                labels[name],
                descriptions[name],
                normalize(curves[name]),
                COLORS[idx],
                trade_stats=stats,
            )
        )
    return _comparison(
        "state-machine",
        "state",
        "首板低开 × 走势状态机",
        "等权主板+创业板日线状态机；固定 MA20/MA60/五日斜率；2020-01-02 起、20 万初始。",
        "状态定义在首次运行前固定：up/down/turn_up/turn_down/range；信号日只用 T-1 及以前数据。"
        "结论：状态效应不稳定；只有「跳过 turn_down」在设计和留出段都改善，但样本仅 7/4 笔，"
        "bootstrap P(Δ>0)=0.946，低于 0.95 验收线，不构成可上线结论。",
        "交易口径；2020-2024 为设计段，2025-2026 为留出段。"
        "小级别进场验证见 2026-11-16-first-board-low-open-15m：15m 确认反而破坏首板低开边际。",
        strategies,
        "2026-11-16-first-board-state-machine",
        [
            "2026-11-16-first-board-state-machine/memo.md",
            "2026-11-16-first-board-state-machine/summary.json",
            "2026-11-16-first-board-low-open-15m/memo.md",
        ],
    )


def build_best_of_family(comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    """One champion per family on a single 2020-2026 curve set."""
    picks: list[tuple[str, str, str, str, str]] = [
        # (comparison-id, strategy-name, shown label, family tag, basis)
        ("first-board-low-open-full-vs-third", "首板低开·单票≤1/3", "首板低开·单票≤1/3", "首板/连板", "trade"),
        ("etf-return-sleeve", "E1 + 10% 纳指 carve", "E1+10%纳指carve", "ETF 配置", "daily"),
        ("pit-quality-vs-smallcap", "PIT 质量 baseline", "PIT 质量 baseline", "小市值/质量", "trade"),
        ("open-gap-momentum", "全 A 等权基准", "等权 A 股基准", "基准", "daily"),
    ]
    strategies: list[dict[str, Any]] = []
    for color_idx, (cid, name, label, family, basis) in enumerate(picks):
        comp = next(c for c in comparisons if c["id"] == cid)
        strat = next(s for s in comp["strategies"] if s["name"] == name)
        strategies.append(
            {
                **strat,
                "name": label,
                "color": COLORS[color_idx],
                "basis": basis,
                "family": family,
            }
        )
    return _comparison(
        "best-of-family",
        "best",
        "各家族最优策略对比",
        "每个研究家族选一个代表策略放同一张图；2020-01-02 起，初始 20 万。",
        "选择规则：首板/连板取唯一正期望的首板低开（1/3 版，风险更可控）；"
        "ETF 配置取通过验收的 E1+10% 纳指 carve；"
        "小市值/质量取可信口径的 PIT 质量 baseline（快照版有选择偏差，不选）；"
        "基准取等权 A 股。开盘缺口动量家族已证伪，不进入本页。",
        "胜率/盈亏比口径：股票策略为交易口径，ETF 与基准为日频口径。年化按 252 个交易日折算。",
        strategies,
        "best-of-family",
        [
            "2026-11-15-first-board-low-open-maxweight33/memo.md",
            "2026-11-13-etf-return-sleeve/memo.md",
            "2026-11-06-pit-quality-baseline/summary.json",
        ],
    )


def build_data() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    family_sources = [
        build_first_board_full_vs_third(),
        build_etf_return_sleeve(),
        build_pit_quality_vs_smallcap(),
        build_open_gap(),
    ]
    comparisons = [
        build_best_of_family(family_sources),
        build_state_machine(),
        build_first_board_full_vs_third(),
        build_first_board_four(),
        build_first_board_pullback(),
        build_open_gap(),
        build_etf_return_sleeve(),
        build_etf_rsrs(),
        build_smallcap_three(),
        build_pit_quality_vs_smallcap(),
    ]
    rich_by_dir = {}
    for comp in comparisons:
        for source in comp["source_dir"].split(" / "):
            rich_by_dir[source.strip()] = comp["id"]
    catalog = build_catalog(rich_by_dir)
    return comparisons, catalog



# ---------------------------------------------------------------------------
# HTML / CSV output
# ---------------------------------------------------------------------------

from datetime import datetime, timezone
from html import escape
from urllib.parse import quote

from backtest_hub_template import HTML_TEMPLATE, MINI_TEMPLATE


def _json_for_script(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text.replace("</", "<\\/")


def _hub_comparison(comp: dict[str, Any]) -> dict[str, Any]:
    item = dict(comp)
    item["strategies"] = [
        {key: value for key, value in strategy.items() if key != "curve"}
        for strategy in comp["strategies"]
    ]
    return item


def _write_comparison(comp: dict[str, Any]) -> None:
    out = HUB / comp["id"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "data.json").write_text(
        json.dumps(comp, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    curve_rows = []
    metric_rows = []
    annual_rows = []
    for strategy in comp["strategies"]:
        for date, value in strategy["curve"]:
            curve_rows.append(
                {"date": date, "strategy": strategy["name"], "equity": value}
            )
        metrics = strategy["metrics"]
        metric_rows.append(
            {
                "strategy": strategy["name"],
                "basis": strategy["basis"],
                "start": strategy.get("start"),
                "end": strategy.get("end"),
                **metrics,
            }
        )
        for year, value in strategy["annual"].items():
            annual_rows.append(
                {"strategy": strategy["name"], "year": int(year), "return": value}
            )
    pd.DataFrame(curve_rows).to_csv(out / "curves.csv", index=False)
    pd.DataFrame(metric_rows).to_csv(out / "metrics.csv", index=False)
    pd.DataFrame(annual_rows).to_csv(out / "annual.csv", index=False)
    title = escape(f"{comp['title']} · 回测详情", quote=False)
    data = {
        "mode": "single",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "initial": INITIAL,
        "start": START.strftime("%Y-%m-%d"),
        "end": END.strftime("%Y-%m-%d"),
        "comparison": comp,
        "categories": CATEGORIES,
    }
    html = (
        HTML_TEMPLATE.replace("__TITLE__", title)
        .replace("__DATA__", _json_for_script(data))
        .replace("__GENERATED__", data["generated_at"])
    )
    (out / "index.html").write_text(html, encoding="utf-8")


def _write_hub(comparisons: list[dict[str, Any]], catalog: list[dict[str, Any]]) -> None:
    HUB.mkdir(parents=True, exist_ok=True)
    (HUB / "data").mkdir(exist_ok=True)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hub_data = {
        "mode": "hub",
        "generated_at": generated,
        "initial": INITIAL,
        "start": START.strftime("%Y-%m-%d"),
        "end": END.strftime("%Y-%m-%d"),
        "categories": CATEGORIES,
        "comparisons": [_hub_comparison(comp) for comp in comparisons],
        "catalog": catalog,
    }
    (HUB / "data" / "manifest.json").write_text(
        json.dumps(hub_data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    html = (
        HTML_TEMPLATE.replace("__TITLE__", "回测中心 · 2020–2026")
        .replace("__DATA__", _json_for_script(hub_data))
        .replace("__GENERATED__", generated)
    )
    (HUB / "index.html").write_text(html, encoding="utf-8")


def _write_catalog_pages(catalog: list[dict[str, Any]]) -> None:
    for row in catalog:
        out = HUB / "catalog" / row["dir"]
        out.mkdir(parents=True, exist_ok=True)
        files = []
        for file in row["files"]:
            href = "../../../" + quote(file["path"], safe="/")
            files.append(f'<li><a href="{escape(href, quote=True)}">{escape(file["label"])}</a></li>')
        rich = ""
        if row.get("rich_id"):
            rich = (
                '<div class="card"><h2>完整对比</h2>'
                f'<a href="../../{escape(row["rich_id"], quote=True)}/index.html">打开对比页 →</a>'
                '</div>'
            )
        body = (
            MINI_TEMPLATE
            .replace("__TITLE__", escape(row["dir"]))
            .replace("__CATEGORY__", escape(row["category"]))
            .replace("__DATE__", escape(row["date"]))
            .replace("__FILES__", "".join(files) or '<li class="note">没有登记文件。</li>')
            .replace("__RICH__", rich)
        )
        (out / "index.html").write_text(body, encoding="utf-8")


def _write_root_redirect() -> None:
    html = """<!doctype html>
<meta charset="utf-8">
<title>回测中心</title>
<meta http-equiv="refresh" content="0; url=2026-11-15-backtest-hub/overview.html">
<p><a href="2026-11-15-backtest-hub/overview.html">进入回测总览</a></p>
"""
    (RESULTS / "index.html").write_text(html, encoding="utf-8")


def _write_readme() -> None:
    text = """# 回测中心

打开 `overview.html` 是总览页；`index.html` 是按策略类别分页签的对比浏览器。
每个对比页包含 2020-01-02 至 2026-10-30、初始 20 万的净值曲线、核心指标、年度收益和策略说明。
`catalog/` 下是每个历史结果目录的独立详情页；“全部目录”页签可以搜索。

## 结构

- `overview.html`：总览（卡片、总曲线、区间指标、年度收益）。
- `index.html`：分类对比浏览器（按策略类别/对比回测分页签）。
- `<comparison-id>/`：一个对比回测的独立页面、`data.json`、`curves.csv`、`metrics.csv`、`annual.csv`。
- `catalog/<result-dir>/index.html`：历史目录详情页，链接原始 memo/report/summary/curves。
- `data/manifest.json`：总览嵌入数据的副本，便于检查。

## 重新生成

```bash
cd /home/ubuntu/development/quant
quant-python/.venv/bin/python scripts/build_backtest_hub.py
```

标准化口径：每个策略曲线在 2020-01-02 归一化到 20 万；年化按 252 个交易日折算；
股票策略的胜率/盈亏比为交易口径，ETF/基准为日频口径。2026 为年初至今。
"""
    (HUB / "README.md").write_text(text, encoding="utf-8")


def main() -> None:
    comparisons, catalog = build_data()
    HUB.mkdir(parents=True, exist_ok=True)
    for comp in comparisons:
        _write_comparison(comp)
    _write_hub(comparisons, catalog)
    overview = build_overview(comparisons)
    (HUB / "overview.html").write_text(_render_overview(overview), encoding="utf-8")
    _write_catalog_pages(catalog)
    _write_root_redirect()
    _write_readme()
    print(f"hub: {HUB / 'index.html'}")
    print(f"comparisons: {len(comparisons)}")
    print(f"catalog pages: {len(catalog)}")




# ---------------------------------------------------------------------------
# Overview data
# ---------------------------------------------------------------------------


def _find_strategy(
    comparisons: list[dict[str, Any]], comp_id: str, name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    comp = next(c for c in comparisons if c["id"] == comp_id)
    strategy = next(s for s in comp["strategies"] if s["name"] == name)
    return comp, strategy


def _overview_series(
    comparisons: list[dict[str, Any]], comp_id: str, name: str, label: str, color: str
) -> dict[str, Any]:
    comp, strategy = _find_strategy(comparisons, comp_id, name)
    return {
        "id": strategy["name"],
        "name": label,
        "color": color,
        "values": [value for _, value in strategy["curve"]],
        "metrics": strategy["metrics"],
        "annual": strategy["annual"],
        "annual_drawdown": strategy["annual_drawdown"],
        "compare_id": comp_id,
        "basis": strategy["basis"],
    }


def build_overview(comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    specs = [
        ("first-board-low-open-full-vs-third", "首板低开·满仓版", "首板低开·满仓", "#60a5fa", "质量快照"),
        ("first-board-low-open-full-vs-third", "首板低开·单票≤1/3", "首板低开·1/3", "#fbbf24", ""),
        ("etf-return-sleeve", "ETF 生产基准", "ETF 生产基准", "#34d399", ""),
        ("smallcap-three-strategies", "质量 A/B baseline", "质量 A/B 快照", "#f472b6", "quality"),
        ("pit-quality-vs-smallcap", "PIT 质量 baseline", "质量 PIT", "#94a3b8", "quality pit"),
        ("pit-quality-vs-smallcap", "PIT 小市值 strict50", "PIT 小市值", "#fb7185", ""),
        ("open-gap-momentum", "开盘缺口策略（T+1 开盘卖）", "开盘缺口策略", "#22d3ee", "negative"),
        ("first-board-four-strategies", "等权 A 股基准", "等权 A 股基准", "#a78bfa", ""),
    ]
    series = [
        _overview_series(comparisons, comp_id, name, label, color)
        for comp_id, name, label, color, _ in specs
    ]
    dates = [date for date, _ in _find_strategy(
        comparisons, "first-board-low-open-full-vs-third", "首板低开·满仓版"
    )[1]["curve"]]
    # Convert each 200k curve to index=100 and align to the common date grid.
    for item in series:
        source = {date: value for date, value in zip(dates, item["values"])}
        raw = item["values"]
        first = raw[0] if raw else 100.0
        aligned = []
        last = None
        cursor = 0
        source_curve = _find_strategy(
            comparisons, item["compare_id"], item["id"]
        )[1]["curve"]
        by_date = {date: value for date, value in source_curve}
        for date in dates:
            if date in by_date:
                last = by_date[date] / first * 100.0
                aligned.append(last)
            else:
                aligned.append(last)
        item["values"] = aligned
    cards = [
        {
            "name": "首板低开·满仓",
            "badge": "原版",
            "big": "+579.0%",
            "meta1": "年化 +33.9% · 回撤 −35.5%",
            "meta2": "Sharpe 1.01 · 20 万 → 135.8 万",
            "class": "q-stock",
            "color": "#60a5fa",
            "compare_id": "first-board-low-open-full-vs-third",
        },
        {
            "name": "首板低开·单票≤1/3",
            "badge": "降集中度",
            "big": "+174.9%",
            "meta1": "年化 +16.7% · 回撤 −23.3%",
            "meta2": "Sharpe 1.08 · 20 万 → 55.0 万",
            "class": "q-ma",
            "color": "#fbbf24",
            "compare_id": "first-board-low-open-full-vs-third",
        },
        {
            "name": "ETF 生产基准",
            "badge": "低回撤",
            "big": "+90.1%",
            "meta1": "年化 +10.3% · 回撤 −10.2%",
            "meta2": "Sharpe 1.20 · 20 万 → 38.0 万",
            "class": "q-etf",
            "color": "#34d399",
            "compare_id": "etf-return-sleeve",
        },
        {
            "name": "质量 A/B 快照",
            "badge": "有选择偏差",
            "big": "+542.9%",
            "meta1": "年化 +32.8% · 回撤 −30.2%",
            "meta2": "Sharpe 1.38 · 20 万 → 128.6 万",
            "class": "q-quality",
            "color": "#f472b6",
            "compare_id": "smallcap-three-strategies",
        },
    ]
    snapshot = next(s for s in series if s["name"] == "质量 A/B 快照")["metrics"]
    pit = next(s for s in series if s["name"] == "质量 PIT")["metrics"]
    return {
        "dates": dates,
        "series": series,
        "cards": cards,
        "alert": {
            "snapshot": snapshot,
            "pit": pit,
        },
        "pills": [
            "<b>首板满仓</b> +579.0%，回撤 −35.5%",
            "<b>单票 1/3</b> +174.9%，回撤收窄到 −23.3%",
            "<b>ETF 生产基准</b> +90.1%，Sharpe 1.20",
            "<b>质量快照</b> +542.9%，PIT 只剩 +53.1%",
            "2020–2026 窗口，不可外推",
        ],
        "notes": [
            (
                "01 · 首板口径",
                "<b>首板低开</b>是日线机械复现：昨日首板、今日低开 3–4%、60 日相对位置≤50%；"
                "次日 11:28/14:50 卖出。缺集合竞价量、盘中价格和历史 ST 名单；"
                "开盘缺口策略在本地数据上是负期望，作为反例保留。",
            ),
            (
                "02 · 单票上限",
                "<b>单票≤1/3</b> 与满仓版使用同一批 219 笔交易（2020 年后入场），"
                "只改买入金额。累计收益约为满仓版的 30%，最大回撤小 12 个百分点；"
                "代价是平均仓位只有 5.1%，大部分时间现金闲置。",
            ),
            (
                "03 · 质量 / PIT",
                "<b>质量 A/B 快照</b>使用 2026 中证 1000 成分池，存在选择偏差；"
                "补入 237 只退市股、改成 PIT 全 A 后，收益从 +542.9% 降到 +53.1%，"
                "最大回撤反而扩大到 −48.5%。不要用快照回测的 31% 年化做决策。",
            ),
        ],
    }


# ---------------------------------------------------------------------------
# Overview output
# ---------------------------------------------------------------------------

from backtest_overview_template import OVERVIEW_TEMPLATE


def _fmt_pct_html(value: float | None) -> str:
    if value is None:
        return "—"
    number = float(value) * 100
    return f"{'+' if number > 0 else ''}{number:.1f}%"


def _fmt_num_html(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):.2f}"


def _fmt_money_html(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value) / 10000:.1f}万"


def _html_class(value: float | None) -> str:
    if value is None:
        return ""
    return "pos" if float(value) > 0 else "neg" if float(value) < 0 else ""


def _overview_payload(overview: dict[str, Any]) -> dict[str, Any]:
    return {
        "dates": overview["dates"],
        "series": [
            {
                "id": item["name"],
                "name": item["name"],
                "color": item["color"],
                "values": [round(float(v), 2) if v is not None else None for v in item["values"]],
                "metrics": item["metrics"],
                "annual": item["annual"],
            }
            for item in overview["series"]
        ],
    }


def _render_overview(overview: dict[str, Any]) -> str:
    cards_html = []
    for card in overview["cards"]:
        cards_html.append(
            f'<article class="card {card["class"]}">'
            f'<div class="card-label"><span class="swatch" style="background:{card["color"]}"></span>'
            f'<a href="./{card.get("compare_id", "")}/index.html" style="color:inherit">{escape(card["name"])}</a>'
            f'<span class="tag">{escape(card["badge"])}</span></div>'
            f'<div class="card-big">{escape(card["big"])}</div>'
            f'<div class="card-meta">{escape(card["meta1"])}</div>'
            f'<div class="card-meta">{escape(card["meta2"])}</div>'
            "</article>"
        )
    alert = overview["alert"]
    snapshot = alert["snapshot"]
    pit = alert["pit"]
    alert_html = (
        f'<div class="qa-copy"><div class="qa-kicker">质量 A/B baseline · 同一规则，两套池子</div>'
        f'<p>当前中证 1000 快照做出 <b>{_fmt_pct_html(snapshot["total_return"])}</b>，'
        f'换成 PIT 全 A（含退市股）只剩 <b>{_fmt_pct_html(pit["total_return"])}</b>。'
        f'31% 的年化主要来自当前成分池，不是选股 alpha。</p></div>'
        f'<div class="qa-stat"><span>当前中证 1000 快照</span>'
        f'<b>{_fmt_pct_html(snapshot["total_return"])}</b>'
        f'<small>年化 {_fmt_pct_html(snapshot["annualized_return"])} · '
        f'回撤 {_fmt_pct_html(snapshot["max_drawdown"])} · Sharpe {_fmt_num_html(snapshot["sharpe"])}</small></div>'
        f'<div class="qa-stat pit"><span>PIT 全 A · 含退市股</span>'
        f'<b>{_fmt_pct_html(pit["total_return"])}</b>'
        f'<small>年化 {_fmt_pct_html(pit["annualized_return"])} · '
        f'回撤 {_fmt_pct_html(pit["max_drawdown"])} · Sharpe {_fmt_num_html(pit["sharpe"])}</small></div>'
    )
    metric_rows = []
    for item in overview["series"]:
        name = item["name"]
        classes = ""
        tag = ""
        if name == "质量 A/B 快照":
            classes = "quality-row"
            tag = '<span class="tag">快照</span>'
        elif name == "质量 PIT":
            classes = "quality-row pit-row"
            tag = '<span class="tag">PIT</span>'
        elif name == "开盘缺口策略":
            classes = "neg-row"
        elif item["basis"] == "daily":
            tag = '<span class="tag">日频</span>'
        metrics = item["metrics"]
        metric_rows.append(
            f'<tr class="{classes}">'
            f'<td><span class="swatch" style="background:{item["color"]}"></span>'
            f'<a href="./{item["compare_id"]}/index.html" style="color:inherit">{escape(name)}</a>{tag}</td>'
            f'<td class="{_html_class(metrics["total_return"])}">{_fmt_pct_html(metrics["total_return"])}</td>'
            f'<td>{_fmt_pct_html(metrics["annualized_return"])}</td>'
            f'<td class="neg">{_fmt_pct_html(metrics["max_drawdown"])}</td>'
            f'<td>{_fmt_num_html(metrics["sharpe"])}</td>'
            f'<td>{_fmt_pct_html(metrics["win_rate"])}</td>'
            f'<td>{_fmt_num_html(metrics["profit_factor"])}</td>'
            f'<td>{_fmt_num_html(metrics["trades"]) if metrics["trades"] is not None else "—"}</td>'
            f'<td>{_fmt_money_html(metrics["final_equity"])}</td>'
            "</tr>"
        )
    years = ["2020", "2021", "2022", "2023", "2024", "2025", "2026"]
    annual_head = "<th>策略</th>" + "".join(
        f'<th>{year}{" YTD" if year == "2026" else ""}</th>' for year in years
    )
    annual_rows = []
    for item in overview["series"]:
        name = item["name"]
        tag = ""
        if name == "质量 A/B 快照":
            tag = '<span class="tag">快照</span>'
        elif name == "质量 PIT":
            tag = '<span class="tag">PIT</span>'
        cells = []
        for year in years:
            value = item["annual"].get(year)
            cells.append(f'<td class="{_html_class(value)}">{_fmt_pct_html(value)}</td>')
        annual_rows.append(
            f'<tr><td><span class="swatch" style="background:{item["color"]}"></span>'
            f'{escape(name)}{tag}</td>{"".join(cells)}</tr>'
        )
    notes_html = "".join(
        f'<div class="note"><div class="n">{escape(title)}</div>{body}</div>'
        for title, body in overview["notes"]
    )
    pills_html = "".join(f'<span class="pill">{pill}</span>' for pill in overview["pills"])
    data = _overview_payload(overview)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = (
        OVERVIEW_TEMPLATE.replace("__TITLE__", "回测总览 · 2020-2026")
        .replace("__PILLS__", pills_html)
        .replace("__CARDS__", "".join(cards_html))
        .replace("__SNAP_TOTAL__", _fmt_pct_html(snapshot["total_return"]))
        .replace("__PIT_TOTAL__", _fmt_pct_html(pit["total_return"]))
        .replace("__ALERT_COPY__", "31% 的年化主要来自当前成分池，不是选股 alpha。")
        .replace("__SNAP_ANN__", _fmt_pct_html(snapshot["annualized_return"]))
        .replace("__SNAP_MDD__", _fmt_pct_html(snapshot["max_drawdown"]))
        .replace("__SNAP_SHARPE__", _fmt_num_html(snapshot["sharpe"]))
        .replace("__PIT_ANN__", _fmt_pct_html(pit["annualized_return"]))
        .replace("__PIT_MDD__", _fmt_pct_html(pit["max_drawdown"]))
        .replace("__PIT_SHARPE__", _fmt_num_html(pit["sharpe"]))
        .replace("__METRICS_ROWS__", "".join(metric_rows))
        .replace("__ANNUAL_HEAD__", annual_head)
        .replace("__ANNUAL_ROWS__", "".join(annual_rows))
        .replace("__NOTES__", notes_html)
        .replace("__DATA__", _json_for_script(data))
        .replace("__GENERATED__", generated)
    )
    return html


if __name__ == "__main__":
    main()
