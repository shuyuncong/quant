"""首板低开：开盘价能不能成交（研究脚本，不接生产）。

回测假定买入价 = 开盘价 + 10bp，没有验证过开盘时有没有足够的量。BaoStock 没有
集合竞价数据，但有不复权 5 分钟线；第一根（09:30–09:35，含集合竞价成交）可以回答
两个问题：订单相对开盘头 5 分钟的成交额有多大；如果拿不到开盘价、改在头 5 分钟
按均价买，结果还剩多少。下面的口径和判定在第一次运行前写定，跑完不改。

口径
    账户规则不动：first_board_four_strategies_study 的 s1，单票 ≤ 1/3，从
    2019-01-01 起跑，先与归档净值逐日核对；只看 2020-01-02 之后的成交。
    参与率     = 该笔买入金额 ÷ 买入日第一根 5 分钟线的成交额。
    受限       = 参与率 > 10%。
    5 分钟均价 = 第一根 5 分钟线的成交额 ÷ 成交量；最高价 = 第一根的最高价。
    换价重算   = 买入价按 (均价或最高价 ÷ 第一根开盘价) 等比例调整，仍加 10bp 滑点，
                 卖出不变，逐笔重算净收益。不重跑账户（股数、后续资金不变）。

判定（全部满足才算"开盘成交这一项关闭"）
    查不到第一根 5 分钟线的成交不超过 5%，否则无法判定；
    按 5 分钟均价买入的单笔净收益均值 > 0；
    受限的成交不超过 10%（按回测账户当时的实际买入金额）。

只报告、不参加判定
    按第一根最高价买入的均值（悲观口径）；
    贡献最大的 10 笔各自的参与率和均价偏离；
    资金放大到 5 倍、10 倍时受限的比例，以及"每笔最多买头 5 分钟成交额的 10%"时
    能买到的金额占比。

已知限制
    第一根 5 分钟线把集合竞价和 09:30–09:35 的连续竞价合在一起，分不出竞价本身的量；
    所以这里验证的是"头 5 分钟内能不能买到"，不是"能不能买在开盘价那一个点上"。
    信号要求低开 3%–4%，开盘不可能在涨跌停价上，一字板开盘在定义上不存在。

用法::

    python first_board_low_open_fill_study.py
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
from first_board_low_open_15m_study import first_5m_bar, repriced_net_return

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = blend.RESULTS / "2026-11-22-first-board-low-open-fill"
BAR_CACHE = BASE_DIR / "cache" / "first_board_low_open_5m"
MAX_PARTICIPATION = 0.10
MAX_UNKNOWN_SHARE = 0.05
MAX_LIMITED_SHARE = 0.10
TOP_TRADES = 10
CAPITAL_MULTIPLES = (1, 5, 10)

FirstBar = Callable[[str, str], dict[str, float] | None]


def baostock_first_bar(symbol: str, day: str) -> dict[str, float] | None:
    return first_5m_bar(symbol, day, BAR_CACHE)


def fill_table(trades: pd.DataFrame, first_bar: FirstBar) -> pd.DataFrame:
    """给每笔成交加上第一根 5 分钟线的参与率，以及按均价 / 最高价买入重算的净收益。"""

    rows = []
    for trade in trades.itertuples():
        bar = first_bar(trade.symbol, trade.entry_date.strftime("%Y-%m-%d"))
        row: dict[str, Any] = {
            "symbol": trade.symbol,
            "entry_date": trade.entry_date.strftime("%Y-%m-%d"),
            "order_amount": trade.gross_entry,
            "net_return": trade.net_return,
            "unknown": bar is None,
        }
        if bar is not None:
            proceeds = trade.gross_exit - trade.exit_fee
            for name, price in (("vwap", bar["amount"] / bar["volume"]), ("high", bar["high"])):
                row[f"{name}_vs_open"] = price / bar["open"] - 1.0
                row[f"net_return_{name}"] = repriced_net_return(trade.gross_entry, proceeds, price / bar["open"])
            row["first_5m_amount"] = bar["amount"]
            row["participation"] = trade.gross_entry / bar["amount"]
            row["flat_first_bar"] = bar["high"] == bar["low"]
        rows.append(row)
    return pd.DataFrame(rows)


def capacity(table: pd.DataFrame, multiple: float) -> dict[str, float]:
    """资金放大 ``multiple`` 倍后：受限成交的比例，和每笔封顶在 10% 参与率时能买到的金额占比。"""

    known = table[~table["unknown"]]
    orders = known["order_amount"] * multiple
    ceiling = known["first_5m_amount"] * MAX_PARTICIPATION
    return {
        "limited_share": round(float((orders > ceiling).mean()), 4),
        "fillable_amount_share": round(float(np.minimum(orders, ceiling).sum() / orders.sum()), 4),
        "median_participation": round(float((orders / known["first_5m_amount"]).median()), 5),
    }


def _pct(value: float, digits: int = 3) -> float:
    return round(float(value) * 100, digits)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(etf.START)

    bundle = four.load_bundle()
    by_index = four.build_signals(bundle)[blend.LOW_OPEN]
    equity, trades = blend.low_open_run(bundle, by_index)
    archive = pd.read_csv(blend.LOW_OPEN_ARCHIVE, parse_dates=["date"]).set_index("date")["equity"]
    parity = blend.check_parity("首板低开", equity, archive)
    window = trades[trades["entry_date"] >= start].reset_index(drop=True)
    print(f"low-open rerun ok: {len(window)} trades since {etf.START}, parity gap {parity:.1e}", flush=True)

    import baostock as bs

    with contextlib.redirect_stdout(io.StringIO()):  # baostock 往 stdout 打登录信息
        login = bs.login()
    if login.error_code != "0":
        raise StudyError(f"BaoStock 登录失败: {login.error_msg}")
    try:
        table = fill_table(window, baostock_first_bar)
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            bs.logout()

    known = table[~table["unknown"]]
    unknown_share = float(table["unknown"].mean())
    scales = {f"x{multiple}": capacity(table, multiple) for multiple in CAPITAL_MULTIPLES}
    best = blend.best_trades(window, equity, start, TOP_TRADES)
    keyed = table.set_index(["symbol", "entry_date"])
    top = []
    for row in best.itertuples():
        item = keyed.loc[(row.symbol, row.entry_date.strftime("%Y-%m-%d"))]
        top.append(
            {
                "symbol": row.symbol,
                "entry_date": row.entry_date.strftime("%Y-%m-%d"),
                "contribution_pct": _pct(row.contribution, 2),
                "net_return_pct": _pct(row.net_return, 2),
                "unknown": bool(item["unknown"]),
                **(
                    {}
                    if item["unknown"]
                    else {
                        "net_return_vwap_pct": _pct(item["net_return_vwap"], 2),
                        "vwap_vs_open_pct": _pct(item["vwap_vs_open"], 2),
                        "participation_pct": _pct(item["participation"], 2),
                        "first_5m_amount": round(float(item["first_5m_amount"])),
                    }
                ),
            }
        )

    fills = {
        "open_plus_10bp_all": {"trades": int(len(table)), "mean_net_return_pct": _pct(table["net_return"].mean())},
        "open_plus_10bp": {"trades": int(len(known)), "mean_net_return_pct": _pct(known["net_return"].mean())},
    }
    for name in ("vwap", "high"):
        column = known[f"net_return_{name}"]
        fills[f"first_5m_{name}"] = {
            "trades": int(len(known)),
            "mean_net_return_pct": _pct(column.mean()),
            "win_rate_pct": _pct((column > 0).mean(), 2),
            "mean_price_vs_open_pct": _pct(known[f"{name}_vs_open"].mean()),
            "median_price_vs_open_pct": _pct(known[f"{name}_vs_open"].median()),
        }
    checks = {
        "coverage": unknown_share <= MAX_UNKNOWN_SHARE,
        "mean_net_return_positive_at_first_5m_vwap": bool(known["net_return_vwap"].mean() > 0),
        "few_capacity_limited_trades": scales["x1"]["limited_share"] <= MAX_LIMITED_SHARE,
    }
    payload = {
        "meta": {
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "start": etf.START,
            "end": etf.END,
            "source": "BaoStock query_history_k_data_plus frequency=5 adjustflag=3, first bar (09:35)",
            "thresholds": {
                "max_participation": MAX_PARTICIPATION,
                "max_unknown_share": MAX_UNKNOWN_SHARE,
                "max_limited_share": MAX_LIMITED_SHARE,
            },
        },
        "parity_max_rel_gap": parity,
        "trades": int(len(table)),
        "unknown": int(table["unknown"].sum()),
        "unknown_share": round(unknown_share, 4),
        "flat_first_bar": int(known["flat_first_bar"].sum()),
        "first_5m_amount": {
            "min": round(float(known["first_5m_amount"].min())),
            "p10": round(float(known["first_5m_amount"].quantile(0.10))),
            "median": round(float(known["first_5m_amount"].median())),
        },
        "participation_pct": {
            "median": _pct(known["participation"].median()),
            "p90": _pct(known["participation"].quantile(0.90)),
            "max": _pct(known["participation"].max()),
        },
        "fills": fills,
        "capacity": scales,
        "best_trades": top,
        "checks": checks,
        "closed": all(checks.values()),
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    table.to_csv(out_dir / "trades.csv", index=False)

    print(f"trades {len(table)}  unknown {payload['unknown']}  flat first bar {payload['flat_first_bar']}", flush=True)
    print(f"first 5m amount {payload['first_5m_amount']}  participation % {payload['participation_pct']}", flush=True)
    for name, row in fills.items():
        print(f"{name:<20} {row}", flush=True)
    for name, row in scales.items():
        print(f"capital {name:<4} {row}", flush=True)
    print(f"checks {checks} closed={payload['closed']}", flush=True)
    print("wrote", out_dir, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
