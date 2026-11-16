#!/usr/bin/env python3
"""Build the 2020-2026 family-champion comparison.

One credible champion per requested family, normalized to 200k on 2020-01-02.
This script only assembles existing, already-published research artifacts; it
does not re-run or tune any strategy.

Champions:
- three stock strategies -> 年线趋势 (highest Sharpe of the three)
- ETF / index          -> E1 + 10% Nasdaq carve
- QQQ                  -> MA200 + 15% vol target (highest 2020-2026 Sharpe)
- first-board low open -> single-name <=1/3
- smallcap             -> PIT TTM EP+ME
- equal-weight A-share benchmark (context, not a family champion)
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "docs" / "backtest-results"
OUT = RESULTS / "2026-11-16-family-best-2020-2026"
START = pd.Timestamp("2020-01-02")
END = pd.Timestamp("2026-10-30")
INITIAL = 200_000.0


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig")


def normalize(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce").dropna()
    s.index = pd.to_datetime(s.index)
    s = s[(s.index >= START) & (s.index <= END)]
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if len(s) < 2:
        return s
    return s / float(s.iloc[0]) * INITIAL


def metrics(series: pd.Series, win_basis: str) -> dict[str, Any]:
    returns = series.pct_change().dropna()
    total = float(series.iloc[-1] / series.iloc[0] - 1.0)
    annualized = float((1.0 + total) ** (252.0 / (len(series) - 1)) - 1.0)
    sharpe = (
        float(returns.mean() / returns.std(ddof=1) * math.sqrt(252.0))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else None
    )
    mdd = float((series / series.cummax() - 1.0).min())
    positive = returns[returns > 0]
    negative = returns[returns < 0]
    pf = (
        float(positive.sum() / abs(negative.sum()))
        if len(positive) and len(negative)
        else None
    )
    return {
        "period": f"{series.index[0].date()}..{series.index[-1].date()}",
        "total_return_pct": round(total * 100.0, 2),
        "annualized_pct": round(annualized * 100.0, 2),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "max_drawdown_pct": round(mdd * 100.0, 2),
        "win_rate_pct": round(float((returns > 0).mean()) * 100.0, 2)
        if win_basis == "daily"
        else None,
        "win_basis": win_basis,
        "profit_factor": round(pf, 3) if pf is not None else None,
        "trades": None,
        "final_equity_200k": round(float(series.iloc[-1]), 2),
        "days": int(len(series)),
    }


def three_stock_best() -> tuple[pd.Series, dict[str, Any], str]:
    html = (ROOT / "docs" / "etf-vs-stocks-2020-2026.html").read_text(encoding="utf-8")
    marker = "const DATA = "
    i = html.index(marker) + len(marker)
    data, _ = json.JSONDecoder().raw_decode(html[i:])
    dates = pd.to_datetime(data["dates"])
    series = next(s for s in data["series"] if s["id"] == "yearline_pullback")
    s = pd.Series(series["values"], index=dates, name="three_yearline")
    meta = series["metrics"]
    stats = {
        "total_return_pct": round(float(meta["total_return_pct"]), 2),
        "annualized_pct": round(float(meta["annualized_return_pct"]), 2),
        "sharpe": round(float(meta["sharpe_ratio"]), 3),
        "max_drawdown_pct": round(-abs(float(meta["max_drawdown_pct"])), 2),
        "win_rate_pct": round(float(meta["win_rate_pct"]), 2),
        "profit_factor": None,
        "trades": int(meta["closed_trades"]),
        "win_basis": "trade",
    }
    return normalize(s), stats, (
        "三策略中 2020-2026 Sharpe 最高：年线趋势 0.265；零轴金叉 0.240、"
        "零轴+底背离 0.224。stop8/TP30、gate-off，2020 起点仅 1,288/5,014 只有预热，"
        "当前上市池、无退市股。"
    )


def qqq_best() -> tuple[pd.Series, dict[str, Any], str]:
    raw = read_csv(RESULTS / "2026-11-08-nasdaq-index" / "curves.csv")
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw = raw.dropna(subset=["date"]).set_index("date").sort_index()
    s = normalize(raw["qqq_ma200_vol15"])
    stats = {"win_basis": "daily"}
    return s, stats, (
        "QQQ MA200 + 15% 年化波动目标；2020-2026 Sharpe 1.029，高于买入持有 0.835，"
        "但年化 14.1% 低于买入持有 19.3%。QQQ 为美元总收益，未做 USD/CNY 换算。"
    )


def etf_best() -> tuple[pd.Series, dict[str, Any], str]:
    raw = read_csv(RESULTS / "2026-11-13-etf-return-sleeve" / "curves.csv")
    raw = raw.rename(columns={raw.columns[0]: "date"})
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw = raw.dropna(subset=["date"]).set_index("date").sort_index()
    s = normalize(raw["E1N3_cash_carve10"])
    return s, {"win_basis": "daily"}, (
        "E1 现金 sleeve + 10% 纳指 carve（513100，人民币）；ETF 家族中通过验收且 Sharpe 最高。"
        "闲置权重放货币 ETF，总敞口不增加，周度调仓。"
    )


def first_board_best() -> tuple[pd.Series, dict[str, Any], str]:
    raw = read_csv(RESULTS / "2026-11-15-first-board-low-open-maxweight33" / "curves.csv")
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw = raw.dropna(subset=["date"])
    s = normalize(raw[raw["strategy"] == "s1_first_board_low_open"].set_index("date")["equity"])
    trades = read_csv(RESULTS / "2026-11-15-first-board-low-open-maxweight33" / "trades.csv")
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce")
    trades = trades[(trades["entry_date"] >= START) & (trades["entry_date"] <= END)]
    r = pd.to_numeric(trades["net_return"], errors="coerce").dropna()
    pnl = pd.to_numeric(trades["net_pnl"], errors="coerce").dropna()
    stats = {
        "win_rate_pct": round(float((r > 0).mean()) * 100.0, 2),
        "profit_factor": round(float(pnl[pnl > 0].sum() / abs(pnl[pnl < 0].sum())), 3),
        "trades": int(len(r)),
        "win_basis": "trade",
    }
    return s, stats, (
        "首板低开单票≤1/3 版；Sharpe 1.080，高于满仓版 1.01，最大回撤 −23.3% 对 −35.5%。"
        "持有期不再平衡，现金闲置，收益低于满仓版。"
    )


def smallcap_best() -> tuple[pd.Series, dict[str, Any], str]:
    raw = read_csv(RESULTS / "2026-11-09-smallcap-value-ttm" / "curves_ep_me.csv")
    raw = raw.rename(columns={raw.columns[0]: "date"})
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw = raw.dropna(subset=["date"]).set_index("date").sort_index()
    s = normalize(raw["equity"])
    summary = json.loads((RESULTS / "2026-11-09-smallcap-value-ttm" / "summary.json").read_text())
    m = summary["variants"]["ep_me"]["metrics"]
    stats = {
        "total_return_pct": round(float(m["total_return_pct"]), 2),
        "annualized_pct": round(float(m["cagr_pct"]), 2),
        "sharpe": round(float(m["sharpe"]), 3),
        "max_drawdown_pct": round(float(m["max_drawdown_pct"]), 2),
        "win_rate_pct": round(float(m["trade_win_rate_pct"]), 2),
        "profit_factor": None,
        "trades": int(m["closed_trades"]),
        "win_basis": "trade",
    }
    return s, stats, (
        "PIT 全 A、30%-50% 流通市值分位、TTM EP 前 50% 再按市值取 50 只、行业≤25%、"
        "月度、10bp；家族可信口径中 Sharpe 0.427、最大回撤 −25.1%。"
        "PIT 质量 baseline 年化更高 6.44%，但回撤 −48.5%，不作为冠军。"
    )


def benchmark() -> pd.Series:
    raw = read_csv(RESULTS / "2026-11-14-open-gap-momentum" / "curves.csv")
    raw = raw.rename(columns={raw.columns[0]: "date"})
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw = raw.dropna(subset=["date"]).set_index("date").sort_index()
    return normalize(raw["ew_all_a"])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    specs = []
    for key, label, family, basis, loader in [
        ("three_yearline", "三股票 · 年线趋势", "三股票策略", "trade", three_stock_best),
        ("etf_e1_carve10", "指数策略 · E1+10%纳指", "指数 / ETF", "daily", etf_best),
        ("qqq_ma200_vol15", "QQQ · MA200+15%波动", "QQQ", "daily", qqq_best),
        ("first_board_third", "首板低开 · 单票≤1/3", "首板低开", "trade", first_board_best),
        ("smallcap_ttm_ep_me", "小市值 · TTM EP+ME", "小市值", "trade", smallcap_best),
    ]:
        s, extra, description = loader()
        m = metrics(s, extra.get("win_basis", basis))
        m.update({k: v for k, v in extra.items() if k != "win_basis"})
        m["win_basis"] = extra.get("win_basis", basis)
        specs.append({
            "id": key,
            "name": label,
            "family": family,
            "description": description,
            "series": s,
            "metrics": m,
        })
    b = benchmark()
    bm = metrics(b, "daily")
    bm.update({"win_basis": "daily"})
    specs.append({
        "id": "benchmark_equal_weight",
        "name": "等权 A 股基准",
        "family": "基准",
        "description": "等权主板+创业板、每日再平衡、无成本；仅作市场参照，不参与冠军评选。",
        "series": b,
        "metrics": bm,
    })

    long_rows = []
    for item in specs:
        for d, v in item["series"].items():
            long_rows.append({"date": d.strftime("%Y-%m-%d"), "strategy": item["id"], "equity": round(float(v), 2)})
    pd.DataFrame(long_rows).to_csv(OUT / "curves.csv", index=False, encoding="utf-8-sig")

    metric_rows = []
    for item in specs:
        m = item["metrics"]
        metric_rows.append({
            "family": item["family"],
            "strategy": item["name"],
            "period": m["period"],
            "total_return_pct": m["total_return_pct"],
            "annualized_pct": m["annualized_pct"],
            "sharpe": m["sharpe"],
            "max_drawdown_pct": m["max_drawdown_pct"],
            "win_rate_pct": m.get("win_rate_pct"),
            "win_basis": m.get("win_basis"),
            "profit_factor": m.get("profit_factor"),
            "trades": m.get("trades"),
            "final_equity_200k": m["final_equity_200k"],
            "days": m["days"],
        })
    pd.DataFrame(metric_rows).to_csv(OUT / "metrics.csv", index=False, encoding="utf-8-sig")

    summary = {
        "generated_at": "2026-11-16",
        "period": {"start": str(START.date()), "end": str(END.date()), "initial": INITIAL},
        "selection_rule": "每个家族按 2020-2026 Sharpe 选冠军；同时检查回撤/可信数据口径。不重新调参。",
        "strategies": [
            {
                "id": item["id"],
                "family": item["family"],
                "name": item["name"],
                "description": item["description"],
                "metrics": item["metrics"],
            }
            for item in specs
        ],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = "\n".join(
        f"| {r['family']} | {r['strategy']} | {r['total_return_pct']:+.2f}% | {r['annualized_pct']:+.2f}% | {r['sharpe']:.3f} | {r['max_drawdown_pct']:.2f}% | {r['win_rate_pct']:.2f}% ({'交易' if r['win_basis']=='trade' else '日频'}) | {r['final_equity_200k']:,.0f} |"
        for r in metric_rows
    )
    memo = f"""# 2020–2026 各家族最优策略对比

- 生成：2026-11-16
- 区间：2020-01-02 → 2026-10-30（2026 为 YTD）
- 初始：20 万，按各曲线起点等比缩放
- 选择规则：**每个家族按 2020–2026 Sharpe 选一个冠军**，同时检查回撤和数据可信度；不重新调参。
- 状态：研究对比，不是实盘业绩，不接生产。

## 总表

| 家族 | 冠军 | 2020–2026 总收益 | 年化 | Sharpe | 最大回撤 | 胜率（口径） | 期末 20 万 |
|---|---|---:|---:|---:|---:|---:|---:|
{rows}

## 选择理由

1. **三股票策略 → 年线趋势**：三策略中 2020–2026 Sharpe 最高（0.265），高于零轴金叉 0.240、零轴+底背离 0.224。数据为 stop8/TP30、gate-off；2020 起点只有 1,288/5,014 只有 270 根预热，且是当前上市池，存在幸存者偏差。
2. **指数策略 → E1+10% 纳指 carve**：ETF 家族中通过验收且 Sharpe 最高（1.370）；E1 现金 sleeve 把闲置权重放货币 ETF，10% 纳指 carve 用 513100（人民币），总敞口不增加。
3. **QQQ → MA200+15% 波动目标**：2020–2026 Sharpe 1.029，高于买入持有 0.835；最大回撤 −14.2% 对 −35.1%。代价是年化 14.1% 低于买入持有 19.3%，且换手高。QQQ 是**美元总收益，未做 USD/CNY 换算**。
4. **首板低开 → 单票≤1/3**：Sharpe 1.080，高于满仓版 1.01；最大回撤 −23.3% 对 −35.5%。代价是收益低于满仓版。
5. **小市值 → PIT TTM EP+ME**：可信 PIT 口径中 Sharpe 0.427、最大回撤 −25.1%；PIT 质量 baseline 年化更高（6.44%）但回撤 −48.5%，不作为冠军。快照版 31% 年化有选择偏差，明确排除。
6. **等权 A 股基准**：仅作参照，不参与冠军评选。

## 读表警告

- **胜率口径不同**：股票/小市值是**交易胜率**（已平仓），ETF/QQQ/基准是**日频胜率**（正收益交易日占比）。不要把 35.6% 的交易胜率和 55.4% 的日频胜率直接比较。
- **币种不同**：三股票、ETF、首板、小市值、基准为人民币；QQQ 为美元总收益。Sharpe 对恒定汇率不敏感，但人民币投资者的实际收益会受 USD/CNY 影响。
- **样本与偏差不同**：三股票只有 1,288 只有预热、当前上市池；小市值是 PIT 全 A + 244 只退市股；首板/ETF 各有自己的股票池/ETF 池。跨家族比较是“研究口径并排”，不是同一可交易组合。
- **2020–2026 是美股强牛 + A 股结构市**；QQQ 的高收益有窗口因素，不能线性外推。
- 冠军按 Sharpe 选，不代表收益最高；满仓首板、QQQ 买入持有、PIT 质量 baseline 的收益都更高，但回撤或可信度更差。

## 文件

- `curves.csv`：各策略归一化到 20 万的日度净值（长表）
- `metrics.csv`：总表机器可读版
- `summary.json`：完整指标、选择理由、数据口径
- 本文件：`memo.md`

## 数据来源

- 三股票：`docs/etf-vs-stocks-2020-2026.html`（2020-01-02 → 2026-10-30；stop8/TP30、gate-off）
- 指数：`docs/backtest-results/2026-11-13-etf-return-sleeve/curves.csv`（`E1N3_cash_carve10`）
- QQQ：`docs/backtest-results/2026-11-08-nasdaq-index/curves.csv`（`qqq_ma200_vol15`）
- 首板低开：`docs/backtest-results/2026-11-15-first-board-low-open-maxweight33/curves.csv`（`s1_first_board_low_open`）
- 小市值：`docs/backtest-results/2026-11-09-smallcap-value-ttm/curves_ep_me.csv`
- 基准：`docs/backtest-results/2026-11-14-open-gap-momentum/curves.csv` 的 `ew_all_a`
"""
    (OUT / "memo.md").write_text(memo, encoding="utf-8")
    print(f"wrote {OUT}")
    print(pd.DataFrame(metric_rows).to_string(index=False))


if __name__ == "__main__":
    main()
