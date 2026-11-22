"""首板低开：用 BaoStock 逐日 isST 标记复测（研究脚本，不接生产）。

之前的首板低开回测都写着"没有历史 ST 名单"。BaoStock 不复权日线自带逐日 isST
字段（ST 和 *ST 都记 1），可以直接回答：回测里有多少信号落在 ST 股票上，去掉它们
之后结果还剩多少。下面的口径和判定在第一次运行前写定，跑完不改。

口径
    账户规则不动：first_board_four_strategies_study 的 s1，单票 ≤ 1/3，从
    2019-01-01 起跑，先与归档净值逐日核对；指标取 2020-01-02 之后。
    ST 的认定沿用聚宽源码：看首板当天（买入日的前一个交易日）是否 ST。

组别
    main              归档口径，不过滤
    no_st             去掉首板当天是 ST 的信号（参加判定）
    no_st_any         首板当天或买入当天任一天是 ST 就去掉（敏感性）
    no_st_or_unknown  再去掉 BaoStock 查不到 isST 的信号（敏感性）

判定（全部满足才算"ST 这一项关闭"）
    BaoStock 查不到 isST 的信号不超过全部信号的 5%，否则无法判定；
    no_st 的单笔净收益均值（2020 年以来的成交）> 0；
    main 里对账户贡献最大的 10 笔成交中，ST 不超过 5 笔。

已知限制
    回测用 10% / 20% 的涨停幅度认定首板，主板 ST 的 5% 涨停本来就不会被认成首板，
    所以预期 ST 信号很少；这里是把"预期"换成实数。创业板 ST 是 20% 涨跌幅，会被认到。

用法::

    python first_board_low_open_st_study.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

import etf_low_open_blend_study as blend
import etf_optimization_study as etf
import first_board_four_strategies_study as four
from etf_extended_study import StudyError
from strategy_statistics import paired_sharpe_diff

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = blend.RESULTS / "2026-11-22-first-board-low-open-st"
FLAG_CACHE = BASE_DIR / "cache" / "first_board_st" / "flags.json"
MAX_UNKNOWN_SHARE = 0.05
TOP_TRADES = 10
MAX_ST_IN_TOP = 5

Query = Callable[[str, str, str], dict[str, int]]


# ---------------------------------------------------------------------------
# isST
# ---------------------------------------------------------------------------


def baostock_query(symbol: str, start: str, end: str) -> dict[str, int]:
    """``symbol`` 在 [start, end] 每个有记录的交易日的 isST（0/1）。"""

    import baostock as bs

    code = f"{'sh' if symbol[0] == '6' else 'sz'}.{symbol}"
    result = bs.query_history_k_data_plus(
        code, "date,isST", start_date=start, end_date=end, frequency="d", adjustflag="3"
    )
    if result.error_code != "0":
        raise StudyError(f"BaoStock isST 查询失败 {code} {start}..{end}: {result.error_msg}")
    flags: dict[str, int] = {}
    while result.next():
        day, is_st = result.get_row_data()
        if is_st in ("0", "1"):
            flags[day] = int(is_st)
    return flags


def load_flags(
    signals: pd.DataFrame, query: Query, cache_path: Path
) -> dict[str, int | None]:
    """每个信号的首板日、买入日的 isST；查过但没有记录的记为 None。结果落盘缓存。"""

    cache: dict[str, int | None] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    fetched = False
    for row in signals.itertuples():
        keys = [f"{row.symbol}|{row.board_date}", f"{row.symbol}|{row.buy_date}"]
        if all(key in cache for key in keys):
            continue
        found = query(row.symbol, row.board_date, row.buy_date)
        cache[keys[0]] = found.get(row.board_date)
        cache[keys[1]] = found.get(row.buy_date)
        fetched = True
    if fetched:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    return cache


def flag_signals(signals: pd.DataFrame, flags: dict[str, int | None]) -> pd.DataFrame:
    """给信号表加上 st（首板日）、st_any（任一天）、unknown（首板日查不到）三列。"""

    board = [flags.get(f"{row.symbol}|{row.board_date}") for row in signals.itertuples()]
    buy = [flags.get(f"{row.symbol}|{row.buy_date}") for row in signals.itertuples()]
    result = signals.copy()
    result["st"] = [value == 1 for value in board]
    result["st_any"] = [a == 1 or b == 1 for a, b in zip(board, buy)]
    result["unknown"] = [value is None for value in board]
    return result


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------


def signal_table(bundle: four.Bundle, signals: dict[int, np.ndarray]) -> pd.DataFrame:
    first = max(3, int(bundle.dates.searchsorted(pd.Timestamp(blend.LOW_OPEN_START))))
    last = int(bundle.dates.searchsorted(pd.Timestamp(etf.END), side="right") - 1)
    rows = [
        {
            "symbol": str(bundle.symbols[column]),
            "board_date": bundle.dates[index - 1].strftime("%Y-%m-%d"),
            "buy_date": bundle.dates[index].strftime("%Y-%m-%d"),
            "index": index,
            "column": int(column),
        }
        for index, columns in sorted(signals.items())
        if first <= index < last  # 最后一天不开新仓，与 simulate 一致
        for column in columns
    ]
    return pd.DataFrame(rows)


def run_stats(equity: pd.Series, trades: pd.DataFrame, start: pd.Timestamp) -> dict[str, Any]:
    window = trades[trades["entry_date"] >= start]
    gains = float(window.loc[window["net_pnl"] > 0, "net_pnl"].sum())
    losses = float(-window.loc[window["net_pnl"] <= 0, "net_pnl"].sum())
    return {
        **etf.curve_metrics(equity.loc[start:]),
        "trades": int(len(window)),
        "win_rate_pct": round(float((window["net_return"] > 0).mean()) * 100, 2),
        "mean_net_return_pct": round(float(window["net_return"].mean()) * 100, 3),
        "profit_factor": round(gains / losses, 3) if losses > 0 else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--etf-data-dir", default=str(BASE_DIR / "cache" / "etf_research_long"))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--n-boot", type=int, default=5000)
    args = parser.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(etf.START)

    bundle = four.load_bundle()
    by_index = four.build_signals(bundle)[blend.LOW_OPEN]
    equity, trades = blend.low_open_run(bundle, by_index)
    archive = pd.read_csv(blend.LOW_OPEN_ARCHIVE, parse_dates=["date"]).set_index("date")["equity"]
    parity = blend.check_parity("首板低开", equity, archive)
    print(f"low-open rerun ok: {len(trades)} trades, parity gap {parity:.1e}", flush=True)

    table = signal_table(bundle, by_index)
    import baostock as bs

    with contextlib.redirect_stdout(io.StringIO()):  # baostock 往 stdout 打登录信息
        login = bs.login()
    if login.error_code != "0":
        raise StudyError(f"BaoStock 登录失败: {login.error_msg}")
    try:
        flags = load_flags(table, baostock_query, FLAG_CACHE)
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            bs.logout()
    table = flag_signals(table, flags)
    bought = set(zip(trades["symbol"], trades["entry_date"].dt.strftime("%Y-%m-%d")))
    table["bought"] = [(row.symbol, row.buy_date) in bought for row in table.itertuples()]
    recent = table[table["buy_date"] >= etf.START]
    print(
        f"signals {len(table)} (2020+: {len(recent)})  st {int(table['st'].sum())}  "
        f"st_any {int(table['st_any'].sum())}  unknown {int(table['unknown'].sum())}",
        flush=True,
    )

    drops = {
        "main": pd.Series(False, index=table.index),
        "no_st": table["st"],
        "no_st_any": table["st_any"],
        "no_st_or_unknown": table["st_any"] | table["unknown"],
    }
    curves: dict[str, pd.Series] = {"main": equity}
    rows: dict[str, dict[str, Any]] = {}
    for name, mask in drops.items():
        removed = table[mask]
        if name != "main":
            drop = frozenset(zip(removed["index"], removed["column"]))
            curves[name], run_trades = blend.low_open_run(bundle, by_index, drop) if drop else (equity, trades)
        else:
            run_trades = trades
        rows[name] = {
            "signals_removed": int(mask.sum()),
            "bought_signals_removed": int(removed["bought"].sum()),
            **run_stats(curves[name], run_trades, start),
        }

    best = blend.best_trades(trades, equity, start, TOP_TRADES)
    st_keys = set(zip(table.loc[table["st"], "symbol"], table.loc[table["st"], "buy_date"]))
    top = [
        {
            "symbol": row.symbol,
            "entry_date": row.entry_date.strftime("%Y-%m-%d"),
            "net_return_pct": round(row.net_return * 100, 2),
            "contribution_pct": round(row.contribution * 100, 2),
            "st": (row.symbol, row.entry_date.strftime("%Y-%m-%d")) in st_keys,
        }
        for row in best.itertuples()
    ]
    unknown_share = float(table["unknown"].mean())
    checks = {
        "coverage": unknown_share <= MAX_UNKNOWN_SHARE,
        "mean_net_return_positive_without_st": rows["no_st"]["mean_net_return_pct"] > 0,
        "st_not_majority_of_best_trades": sum(item["st"] for item in top) <= MAX_ST_IN_TOP,
    }

    # 混合组合：只有去掉 ST 改变了成交时才需要重算。
    blended: dict[str, Any] | None = None
    if rows["no_st"]["bought_signals_removed"]:
        etf_leg = blend.etf_curves(Path(args.etf_data_dir))["base"]
        baseline = etf_leg / etf_leg.iloc[0] * four.INITIAL_CAPITAL
        base_returns = baseline.pct_change().dropna()
        blended = {"baseline": etf.curve_metrics(baseline)}
        for name in ("main", "no_st"):
            curve, _ = blend.blend(etf_leg, curves[name].reindex(etf_leg.index), blend.PRIMARY, blend.TRANSFER_COST)
            blended[f"B20_{name}"] = {
                **etf.curve_metrics(curve),
                "vs_baseline": paired_sharpe_diff(curve.pct_change().dropna(), base_returns, n_boot=args.n_boot),
            }

    payload = {
        "meta": {
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "start": etf.START,
            "end": etf.END,
            "st_source": "BaoStock query_history_k_data_plus isST (adjustflag=3)",
            "st_definition": "isST == 1 on the first-board day (the session before the buy)",
            "thresholds": {
                "max_unknown_share": MAX_UNKNOWN_SHARE,
                "top_trades": TOP_TRADES,
                "max_st_in_top": MAX_ST_IN_TOP,
            },
        },
        "parity_max_rel_gap": parity,
        "signals": {
            "all": int(len(table)),
            "since_2020": int(len(recent)),
            "st": int(table["st"].sum()),
            "st_any": int(table["st_any"].sum()),
            "unknown": int(table["unknown"].sum()),
            "unknown_share": round(unknown_share, 4),
            "st_bought": int((table["st"] & table["bought"]).sum()),
        },
        "runs": rows,
        "best_trades": top,
        "checks": checks,
        "closed": all(checks.values()),
        "blend": blended,
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    table.drop(columns=["index", "column"]).to_csv(out_dir / "signals.csv", index=False)
    pd.DataFrame([{"run": name, **row} for name, row in rows.items()]).to_csv(out_dir / "metrics.csv", index=False)

    for name, row in rows.items():
        print(
            f"{name:<17} removed {row['signals_removed']:>3} (bought {row['bought_signals_removed']:>3})  "
            f"trades {row['trades']:>3}  mean {row['mean_net_return_pct']:+.3f}%  win {row['win_rate_pct']:.1f}%  "
            f"total {row['total_return_pct']:+.2f}%  sharpe {row['sharpe']:.3f}  dd {row['max_drawdown_pct']:.2f}%",
            flush=True,
        )
    print(f"checks {checks} closed={payload['closed']}", flush=True)
    print("wrote", out_dir, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
