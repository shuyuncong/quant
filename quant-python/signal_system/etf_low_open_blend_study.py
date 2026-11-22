"""ETF（E1 + 10% 纳指）+ 首板低开：固定权重混合的预注册检验（研究脚本，不接生产）。

要回答的问题：在现有 ETF 组合里固定拿出一部分资金跑首板低开，风险调整后收益
是否比只跑 ETF 更好。下面的口径在第一次运行前写定，跑完不改。

两条腿（规则都不动，直接调用各自归档回测的代码重跑，并与归档净值逐日核对）
    ETF       etf_optimization_study 的 E1N3_cash_carve10，周度调仓、次日开盘成交
    首板低开   first_board_four_strategies_study 的 s1，单票 ≤ 1/3，
              从 2019-01-01 起跑（与归档一致），取 2020-01-02 之后的日收益

混合方式
    两个子账户各自运作；每月最后一个交易日收盘后把两边调回固定权重，
    划转金额按 10bp 计成本（压力情景 25bp）。首板低开子账户的闲置现金不计息。

组别
    baseline   100% ETF
    B20        80% ETF + 20% 首板低开（唯一参加验收的组）
    B10 / B30  权重敏感性，只看方向，不参加验收
    B20_drop5 / B20_drop10
               去掉 2020 年以来对首板低开账户贡献最大的 5 / 10 笔信号后重跑
    low_open   100% 首板低开，仅作参照

验收（B20 相对 baseline，全部满足才算通过；门槛沿用 etf_optimization_study）
    全期 Sharpe 差 > 0，配对 stationary bootstrap 的 P(差>0) ≥ 0.90；
    设计期（2020-2023）和留出期（2024-2026）的 Sharpe 差都 ≥ 0；
    最大回撤不比 baseline 深超过 1pp；
    压力情景（ETF 滑点 20bp、首板低开滑点 20bp/边、划转 25bp）下年化不低于
    同样压力下的 baseline；
    B20_drop5 的全期 Sharpe 差仍 ≥ 0（已知首板低开的收益集中在少数几笔）。

已知的不干净之处
    两条腿的 2024-2026 都被之前的研究看过；20% 这个权重定下来之前，看过一张
    不计成本、每日再平衡的 0/20/30/50/100% 粗表。所以留出期只是时间上的留出，
    结论只能用来决定"值不值得做前向验证"，不能当成样本外证据。

用法::

    python etf_low_open_blend_study.py
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd

import etf_optimization_study as etf
import first_board_four_strategies_study as four
from etf_extended_study import (
    SLIPPAGE as ETF_SLIPPAGE,
    StudyError,
    build_panels,
    build_signals,
    execute,
    load_panels,
    rebalance_days,
)
from strategy_statistics import paired_sharpe_diff

BASE_DIR = Path(__file__).resolve().parent
RESULTS = BASE_DIR.parent.parent / "docs" / "backtest-results"
DEFAULT_OUT = RESULTS / "2026-11-22-etf-low-open-blend"
ETF_ARCHIVE = RESULTS / "2026-11-13-etf-return-sleeve" / "curves.csv"
LOW_OPEN_ARCHIVE = RESULTS / "2026-11-15-first-board-low-open-maxweight33" / "curves.csv"
ETF_ARM = "E1N3_cash_carve10"
LOW_OPEN = "s1_first_board_low_open"
LOW_OPEN_START = "2019-01-01"  # 归档回测的起点；净值有路径依赖，必须从同一天跑
LOW_OPEN_MAX_WEIGHT = 0.3333333333
PRIMARY = 0.20
SENSITIVITY = (0.10, 0.30)
TRANSFER_COST = 0.0010
STRESS_TRANSFER_COST = 0.0025
STRESS_LOW_OPEN_SLIPPAGE = 0.0020
ROBUST_DROP = 5
DROPS = (5, 10)
PARITY_RTOL = 1e-6
PERIODS: dict[str, tuple[str | None, str | None]] = {
    "full": (None, None),
    "design": (None, etf.DESIGN_END),
    "holdout": (etf.HOLDOUT_START, None),
}


# ---------------------------------------------------------------------------
# 两条腿
# ---------------------------------------------------------------------------


def etf_curves(data_dir: Path) -> dict[str, pd.Series]:
    """ETF 腿在正常滑点和 20bp 压力滑点下的日度净值。"""

    satellite, close, open_, volume = load_panels(data_dir, extra=(etf.CASH_ETF, etf.NASDAQ_ETF))
    dates = close.loc[pd.Timestamp(etf.START) : pd.Timestamp(etf.END)].index
    panels = build_panels(close, volume)
    arm = etf.build_arms(build_signals(close, satellite, panels, dates), panels)[ETF_ARM]
    return {
        name: execute(
            close, open_, dates=dates, target_fn=arm["target"], rebalance="W",
            slippage=slippage, label=f"{ETF_ARM}_{name}", band=arm["band"],
        ).curve
        for name, slippage in (("base", ETF_SLIPPAGE), ("stress", etf.STRESS_SLIPPAGE))
    }


@contextlib.contextmanager
def low_open_slippage(value: float) -> Iterator[None]:
    """首板低开回测的滑点是模块常量；压力情景临时改掉。"""

    previous = four.SLIPPAGE
    four.SLIPPAGE = value
    try:
        yield
    finally:
        four.SLIPPAGE = previous


def low_open_run(
    bundle: four.Bundle,
    signals: dict[int, np.ndarray],
    drop: frozenset[tuple[int, int]] = frozenset(),
) -> tuple[pd.Series, pd.DataFrame]:
    """跑首板低开账户，返回日度净值和逐笔成交；``drop`` 是要剔除的 (交易日序号, 列)。"""

    if drop:
        signals = {
            index: np.array([c for c in columns if (index, int(c)) not in drop], dtype=columns.dtype)
            for index, columns in signals.items()
        }
    portfolio, equity, _, _ = four.simulate(
        bundle, {LOW_OPEN: signals}, LOW_OPEN, LOW_OPEN_START, etf.END, 0.0, LOW_OPEN_MAX_WEIGHT
    )
    return equity, pd.DataFrame([asdict(trade) for trade in portfolio.trades])


def best_trades(trades: pd.DataFrame, equity: pd.Series, start: pd.Timestamp, count: int) -> pd.DataFrame:
    """``start`` 之后对账户贡献最大的几笔：单笔净盈亏 ÷ 买入前一日的账户净值。"""

    window = trades[trades["entry_date"] >= start]
    before = equity.shift(1).reindex(window["entry_date"]).to_numpy()
    contribution = pd.Series(window["net_pnl"].to_numpy() / before, index=window.index)
    chosen = window.loc[contribution.sort_values(ascending=False).index[:count]].copy()
    chosen["contribution"] = contribution.loc[chosen.index]
    return chosen


def check_parity(name: str, curve: pd.Series, archive: pd.Series) -> float:
    if not curve.index.equals(archive.index):
        raise StudyError(f"{name}: 重跑的交易日与归档不一致")
    gap = float((curve / archive - 1.0).abs().max())
    if gap > PARITY_RTOL:
        raise StudyError(f"{name}: 重跑净值与归档最大相差 {gap:.2e}，数据或代码已变")
    return gap


# ---------------------------------------------------------------------------
# 混合
# ---------------------------------------------------------------------------


def blend(
    etf_curve: pd.Series,
    low_open_curve: pd.Series,
    weight: float,
    cost: float,
    initial: float = four.INITIAL_CAPITAL,
) -> tuple[pd.Series, float]:
    """两个子账户按各自日收益运作，月末收盘后调回 ``weight``。

    返回混合净值和累计划转量（每次划转金额 ÷ 当时总资产 之和）。
    """

    if not etf_curve.index.equals(low_open_curve.index):
        raise StudyError("两条腿的交易日不一致")
    dates = etf_curve.index
    marks = rebalance_days(dates, "M")
    etf_growth = (etf_curve / etf_curve.shift(1)).to_numpy()
    low_open_growth = (low_open_curve / low_open_curve.shift(1)).to_numpy()
    etf_value, low_open_value = (1.0 - weight) * initial, weight * initial
    values = [initial]
    moved = 0.0
    for index in range(1, len(dates)):
        etf_value *= etf_growth[index]
        low_open_value *= low_open_growth[index]
        if dates[index] in marks:
            total = etf_value + low_open_value
            move = abs(weight * total - low_open_value)
            moved += move / total
            total -= move * cost
            etf_value, low_open_value = (1.0 - weight) * total, weight * total
        values.append(etf_value + low_open_value)
    return pd.Series(values, index=dates), moved


def yearly_pct(curve: pd.Series) -> dict[str, float]:
    last = curve.groupby(curve.index.year).last()
    previous = last.shift(1).fillna(curve.iloc[0])
    return {str(year): round(float(value) * 100, 2) for year, value in (last / previous - 1.0).items()}


def acceptance(arm: dict[str, Any], baseline: dict[str, Any], dropped: dict[str, Any]) -> dict[str, bool]:
    diff = arm["vs_baseline"]
    return {
        "sharpe_diff_positive": diff["full"]["sharpe_diff"] > 0,
        "prob_diff_gt_zero": diff["full"]["prob_diff_gt_zero"] >= etf.PROB_THRESHOLD,
        "design_not_worse": diff["design"]["sharpe_diff"] >= 0,
        "holdout_not_worse": diff["holdout"]["sharpe_diff"] >= 0,
        "drawdown_within_tolerance": (
            arm["full"]["max_drawdown_pct"]
            >= baseline["full"]["max_drawdown_pct"] - etf.DRAWDOWN_TOLERANCE_PP
        ),
        "stress_cagr_not_worse": arm["stress"]["cagr_pct"] >= baseline["stress"]["cagr_pct"],
        f"robust_without_best{ROBUST_DROP}": dropped["vs_baseline"]["full"]["sharpe_diff"] >= 0,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--etf-data-dir", default=str(BASE_DIR / "cache" / "etf_research_long"))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--n-boot", type=int, default=5000)
    args = parser.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ETF 腿
    etf_leg = etf_curves(Path(args.etf_data_dir))
    archive = pd.read_csv(ETF_ARCHIVE, index_col=0, parse_dates=True)[ETF_ARM]
    parity = {"etf_max_rel_gap": check_parity("ETF", etf_leg["base"], archive)}
    dates = etf_leg["base"].index
    print(f"etf leg ok: {len(dates)} days, parity gap {parity['etf_max_rel_gap']:.1e}", flush=True)

    # 首板低开腿
    bundle = four.load_bundle()
    signals = four.build_signals(bundle)[LOW_OPEN]
    equity, trades = low_open_run(bundle, signals)
    archive = pd.read_csv(LOW_OPEN_ARCHIVE, parse_dates=["date"]).set_index("date")["equity"]
    parity["low_open_max_rel_gap"] = check_parity("首板低开", equity, archive)
    print(f"low-open leg ok: {len(trades)} trades, parity gap {parity['low_open_max_rel_gap']:.1e}", flush=True)
    with low_open_slippage(STRESS_LOW_OPEN_SLIPPAGE):
        stress_equity, _ = low_open_run(bundle, signals)

    column_of = {str(symbol): column for column, symbol in enumerate(bundle.symbols)}
    low_open_legs = {"base": equity, "stress": stress_equity}
    removed: dict[str, list[dict[str, Any]]] = {}
    for count in DROPS:
        best = best_trades(trades, equity, dates[0], count)
        drop = frozenset(
            (int(bundle.dates.get_loc(day)), column_of[symbol])
            for day, symbol in zip(best["entry_date"], best["symbol"])
        )
        low_open_legs[f"drop{count}"], _ = low_open_run(bundle, signals, drop)
        removed[f"drop{count}"] = [
            {
                "symbol": row.symbol,
                "entry_date": row.entry_date.strftime("%Y-%m-%d"),
                "net_return_pct": round(row.net_return * 100, 2),
                "contribution_pct": round(row.contribution * 100, 2),
            }
            for row in best.itertuples()
        ]
    low_open_legs = {name: curve.reindex(dates) for name, curve in low_open_legs.items()}
    if any(curve.isna().any() for curve in low_open_legs.values()):
        raise StudyError("首板低开净值缺少 ETF 腿的部分交易日")

    # 组别：(组, 首板低开权重, 用哪条首板低开净值)
    specs: dict[str, tuple[str, float, str]] = {
        "baseline": ("baseline", 0.0, "base"),
        **{f"B{round(w * 100)}": ("sensitivity", w, "base") for w in SENSITIVITY},
        f"B{round(PRIMARY * 100)}": ("primary", PRIMARY, "base"),
        **{f"B{round(PRIMARY * 100)}_drop{n}": ("robustness", PRIMARY, f"drop{n}") for n in DROPS},
        "low_open": ("reference", 1.0, "base"),
    }
    curves: dict[str, pd.Series] = {}
    rows: dict[str, dict[str, Any]] = {}
    for name, (group, weight, leg) in specs.items():
        curve, moved = blend(etf_leg["base"], low_open_legs[leg], weight, TRANSFER_COST)
        curves[name] = curve
        rows[name] = {
            "group": group,
            "low_open_weight": weight,
            **{period: etf.curve_metrics(curve, start, end) for period, (start, end) in PERIODS.items()},
            "yearly_pct": yearly_pct(curve),
            "transfers_x": round(moved, 3),
        }
        if leg == "base":
            stress, _ = blend(etf_leg["stress"], low_open_legs["stress"], weight, STRESS_TRANSFER_COST)
            rows[name]["stress"] = etf.curve_metrics(stress)

    base_returns = curves["baseline"].pct_change().dropna()
    for name, row in rows.items():
        if name == "baseline":
            continue
        returns = curves[name].pct_change().dropna()
        row["vs_baseline"] = {
            period: paired_sharpe_diff(returns.loc[start:end], base_returns.loc[start:end], n_boot=args.n_boot)
            for period, (start, end) in PERIODS.items()
        }
    primary = f"B{round(PRIMARY * 100)}"
    checks = acceptance(rows[primary], rows["baseline"], rows[f"{primary}_drop{ROBUST_DROP}"])
    rows[primary]["checks"] = checks
    rows[primary]["accepted"] = all(checks.values())

    leg_returns = pd.DataFrame({
        "etf": etf_leg["base"].pct_change(),
        "low_open": low_open_legs["base"].pct_change(),
    }).dropna()
    monthly = (1.0 + leg_returns).groupby(leg_returns.index.to_period("M")).prod() - 1.0
    payload = {
        "meta": {
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "start": str(dates[0].date()),
            "end": str(dates[-1].date()),
            "design_end": etf.DESIGN_END,
            "holdout_start": etf.HOLDOUT_START,
            "initial": four.INITIAL_CAPITAL,
            "etf_arm": ETF_ARM,
            "low_open_max_weight": LOW_OPEN_MAX_WEIGHT,
            "primary_weight": PRIMARY,
            "rebalance": "month-end close",
            "transfer_cost": TRANSFER_COST,
            "stress": {
                "etf_slippage": etf.STRESS_SLIPPAGE,
                "low_open_slippage": STRESS_LOW_OPEN_SLIPPAGE,
                "transfer_cost": STRESS_TRANSFER_COST,
            },
            "n_boot": args.n_boot,
            "acceptance": {
                "prob_diff_gt_zero": etf.PROB_THRESHOLD,
                "drawdown_tolerance_pp": etf.DRAWDOWN_TOLERANCE_PP,
                "robust_drop": ROBUST_DROP,
            },
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "parity": parity,
        "leg_correlation": {
            "daily": round(float(leg_returns["etf"].corr(leg_returns["low_open"])), 3),
            "monthly": round(float(monthly["etf"].corr(monthly["low_open"])), 3),
        },
        "removed_trades": removed,
        "arms": rows,
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    pd.DataFrame(curves).rename_axis("date").to_csv(out_dir / "curves.csv")
    table = []
    for name, row in rows.items():
        diff = row.get("vs_baseline", {})
        table.append({
            "arm": name,
            "group": row["group"],
            "low_open_weight": row["low_open_weight"],
            **{
                f"{period}_{key}": row[period].get(key)
                for period in PERIODS
                for key in ("total_return_pct", "cagr_pct", "sharpe", "max_drawdown_pct")
            },
            "stress_cagr_pct": row.get("stress", {}).get("cagr_pct"),
            **{f"d_sharpe_{period}": diff.get(period, {}).get("sharpe_diff") for period in PERIODS},
            "ci95_low": diff.get("full", {}).get("ci95_low"),
            "ci95_high": diff.get("full", {}).get("ci95_high"),
            "prob_diff_gt_zero": diff.get("full", {}).get("prob_diff_gt_zero"),
            "accepted": row.get("accepted"),
        })
    pd.DataFrame(table).to_csv(out_dir / "metrics.csv", index=False)

    for name, row in rows.items():
        full, diff = row["full"], row.get("vs_baseline", {})
        line = (
            f"{name:<12} cagr {full['cagr_pct']:>6.2f}%  sharpe {full['sharpe']:>5.3f}  "
            f"dd {full['max_drawdown_pct']:>7.2f}%"
        )
        if "stress" in row:
            line += f"  stress cagr {row['stress']['cagr_pct']:>6.2f}%"
        if diff:
            line += (
                f"  dSharpe {diff['full']['sharpe_diff']:+.3f} "
                f"[{diff['full']['ci95_low']:+.3f},{diff['full']['ci95_high']:+.3f}] "
                f"P>0 {diff['full']['prob_diff_gt_zero']:.3f}  "
                f"design {diff['design']['sharpe_diff']:+.3f}  holdout {diff['holdout']['sharpe_diff']:+.3f}"
            )
        print(line, flush=True)
    print(f"{primary} checks {checks} accepted={rows[primary]['accepted']}", flush=True)
    print("wrote", out_dir, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
