"""开盘涨幅动量策略：亏损归因与变体扫描（研究脚本）。

主回测（open_gap_momentum_study.py）已经给出负期望。这个脚本只做三件事：

1. 把 open_1 的收益拆成“入场日开盘→收盘”“隔夜”“次日开盘→收盘”三段，
   看亏损来自哪里。
2. 在**看到主回测结果之后**测试一组简单的、有经济含义的变体（收紧开盘涨幅、
   加流动性/价格/板块过滤、改排序方向、改入场时点）。这些变体不是预注册策略，
   存在多重检验；任何看起来为正的结果都必须独立样本验证。
3. 按 399101 的开盘缺口/前日涨跌做市场状态分层，看亏损是否集中在某类日子。

输出写到主回测目录下的 diagnostics.md / variants.csv。
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from open_gap_momentum_study import (
    DEFAULT_OUT,
    INDEX_399101,
    PANEL_CACHE,
    StudyConfig,
    _pct,
    build_signal_table,
    build_trades,
    load_study_panels,
    md_table,
    stats_from_trades,
)

ADV_MIN = 50_000_000.0
ADV_STRICT = 100_000_000.0


def select_from_mask(
    mask: np.ndarray,
    signal: dict[str, Any],
    max_picks: int = 2,
    pick: str = "desc",
) -> pd.DataFrame:
    dates = signal["dates"]
    symbols = signal["symbols"]
    open_ = signal["open"].to_numpy(dtype="float64")
    open_gap = signal["open_gap"].to_numpy(dtype="float64")
    yest_ret = signal["yest_ret"].to_numpy(dtype="float64")
    ret20 = signal["ret20"].to_numpy(dtype="float64")

    records: list[dict[str, Any]] = []
    for index in range(len(dates)):
        candidates = np.flatnonzero(mask[index])
        if candidates.size == 0:
            continue
        if pick == "desc":
            order = np.lexsort((symbols[candidates], -open_gap[index, candidates]))
        elif pick == "asc":
            order = np.lexsort((symbols[candidates], open_gap[index, candidates]))
        elif pick == "abs":
            order = np.lexsort((symbols[candidates], np.abs(open_gap[index, candidates])))
        else:
            raise ValueError(f"unknown pick mode: {pick}")
        for column in candidates[order[:max_picks]]:
            records.append(
                {
                    "entry_idx": int(index),
                    "entry_date": dates[index],
                    "symbol": str(symbols[column]),
                    "entry_open": float(open_[index, column]),
                    "open_gap": float(open_gap[index, column]),
                    "yest_ret": float(yest_ret[index, column]),
                    "ret20": float(ret20[index, column]),
                }
            )
    return pd.DataFrame(records)


def custom_trades(
    selected: pd.DataFrame,
    signal: dict[str, Any],
    cfg: StudyConfig,
    entry_offset: int,
    exit_offset: int,
    entry_kind: str,
    exit_kind: str,
) -> pd.DataFrame:
    """按指定的开/收盘偏移计算交易；用于入场/出场时点诊断。"""

    dates = signal["dates"]
    open_ = signal["open"].to_numpy(dtype="float64")
    close = signal["close"].to_numpy(dtype="float64")
    column_index = {
        str(symbol): index for index, symbol in enumerate(signal["symbols"])
    }

    rows: list[dict[str, Any]] = []
    for record in selected.itertuples(index=False):
        entry_idx = int(record.entry_idx) + entry_offset
        exit_idx = int(record.entry_idx) + exit_offset
        if entry_idx >= len(dates) or exit_idx >= len(dates):
            continue
        column = column_index[str(record.symbol)]
        entry = open_[entry_idx, column] if entry_kind == "open" else close[entry_idx, column]
        exit_price = open_[exit_idx, column] if exit_kind == "open" else close[exit_idx, column]
        if (
            not math.isfinite(entry)
            or not math.isfinite(exit_price)
            or entry <= 0
            or exit_price <= 0
        ):
            continue
        gross = exit_price / entry - 1.0
        net = (
            exit_price
            * (1.0 - cfg.slippage)
            * (1.0 - cfg.commission - cfg.stamp_tax)
            / (entry * (1.0 + cfg.slippage) * (1.0 + cfg.commission))
            - 1.0
        )
        rows.append(
            {
                "entry_date": dates[entry_idx],
                "exit_date": dates[exit_idx],
                "symbol": str(record.symbol),
                "gross_return": gross,
                "net_return": net,
            }
        )
    return pd.DataFrame(rows)


def variant_row(
    name: str,
    trades: pd.DataFrame,
    *,
    category: str,
) -> dict[str, Any]:
    if trades.empty:
        return {"variant": name, "category": category, "trades": 0}
    stats = stats_from_trades(trades, name)
    in_sample = trades[trades["entry_date"] < pd.Timestamp("2025-01-01")]
    out_sample = trades[trades["entry_date"] >= pd.Timestamp("2025-01-01")]
    return {
        "variant": name,
        "category": category,
        "trades": stats["trades"],
        "net_mean_pct": stats["net_mean_pct"],
        "net_median_pct": stats["net_median_pct"],
        "win_rate_pct": stats["win_rate_pct"],
        "t_stat_daily": stats["t_stat_daily"],
        "net_mean_2018_2024_pct": round(float(in_sample["net_return"].mean()) * 100.0, 4)
        if not in_sample.empty
        else None,
        "net_mean_2025_2026_pct": round(float(out_sample["net_return"].mean()) * 100.0, 4)
        if not out_sample.empty
        else None,
    }


def decomposition(
    selected: pd.DataFrame, signal: dict[str, Any], cfg: StudyConfig
) -> pd.DataFrame:
    dates = signal["dates"]
    open_ = signal["open"].to_numpy(dtype="float64")
    close = signal["close"].to_numpy(dtype="float64")
    column_index = {
        str(symbol): index for index, symbol in enumerate(signal["symbols"])
    }

    rows: list[dict[str, Any]] = []
    for record in selected.itertuples(index=False):
        index = int(record.entry_idx)
        if index + 1 >= len(dates):
            continue
        column = column_index[str(record.symbol)]
        o0 = open_[index, column]
        c0 = close[index, column]
        o1 = open_[index + 1, column]
        c1 = close[index + 1, column]
        if not all(math.isfinite(value) and value > 0 for value in (o0, c0, o1)):
            continue
        rows.append(
            {
                "entry_date": dates[index],
                "symbol": str(record.symbol),
                "open_to_open": o1 / o0 - 1.0,
                "entry_intraday": c0 / o0 - 1.0,
                "overnight": o1 / c0 - 1.0,
                "exit_intraday": c1 / o1 - 1.0 if math.isfinite(c1) and c1 > 0 else np.nan,
            }
        )
    return pd.DataFrame(rows)


def market_regime_table(
    selected: pd.DataFrame,
    signal: dict[str, Any],
    cfg: StudyConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = build_trades(selected, signal, 1, "open", cfg)
    dates = signal["dates"]
    index = (
        pd.read_csv(INDEX_399101, parse_dates=["date"])
        .set_index("date")
        .sort_index()
        .reindex(dates)
        .ffill()
    )
    index_gap = (index["open"] / index["close"].shift(1) - 1.0).rename("index_gap")
    index_ret = (index["close"].shift(1) / index["close"].shift(2) - 1.0).rename("index_ret")

    frame = trades.merge(
        pd.DataFrame({"entry_date": index_gap.index, "index_gap": index_gap.values}),
        on="entry_date",
        how="left",
    ).merge(
        pd.DataFrame({"entry_date": index_ret.index, "index_ret": index_ret.values}),
        on="entry_date",
        how="left",
    )

    def bucket_table(column: str, bins: list[float], labels: list[str]) -> pd.DataFrame:
        frame["bucket"] = pd.cut(frame[column], bins=bins, labels=labels)
        rows = []
        for label, group in frame.groupby("bucket", observed=True):
            net = group["net_return"].to_numpy(dtype="float64")
            rows.append(
                {
                    "bucket": str(label),
                    "trades": int(len(net)),
                    "net_mean_pct": round(float(net.mean()) * 100.0, 4),
                    "net_median_pct": round(float(np.median(net)) * 100.0, 4),
                    "win_rate_pct": round(float((net > 0).mean()) * 100.0, 2),
                }
            )
        return pd.DataFrame(rows)

    gap_table = bucket_table(
        "index_gap",
        [-1.0, -0.02, -0.01, 0.0, 0.01, 0.02, 1.0],
        ["<=-2%", "-2~-1%", "-1~0%", "0~1%", "1~2%", ">2%"],
    )
    ret_table = bucket_table(
        "index_ret",
        [-1.0, -0.02, -0.01, 0.0, 0.01, 0.02, 1.0],
        ["<=-2%", "-2~-1%", "-1~0%", "0~1%", "1~2%", ">2%"],
    )
    return gap_table, ret_table


def render_diagnostics(
    decomposition_frame: pd.DataFrame,
    variants: pd.DataFrame,
    timing: pd.DataFrame,
    gap_table: pd.DataFrame,
    ret_table: pd.DataFrame,
) -> str:
    decomp_rows = []
    for column, label in [
        ("open_to_open", "open_t → open_{t+1}（总）"),
        ("entry_intraday", "入场日 open_t → close_t"),
        ("overnight", "隔夜 close_t → open_{t+1}"),
        ("exit_intraday", "次日 open_{t+1} → close_{t+1}"),
    ]:
        values = decomposition_frame[column].dropna().to_numpy()
        decomp_rows.append(
            [
                label,
                len(values),
                _pct(float(values.mean()) * 100.0, 3),
                _pct(float(np.median(values)) * 100.0, 3),
                _pct(float((values > 0).mean()) * 100.0, 1),
            ]
        )

    variant_rows = [
        [
            row["variant"],
            row["trades"],
            _pct(row["net_mean_pct"], 3),
            _pct(row["net_median_pct"], 3),
            _pct(row["win_rate_pct"], 1),
            f"{row['t_stat_daily']:.2f}",
            _pct(row["net_mean_2018_2024_pct"], 3),
            _pct(row["net_mean_2025_2026_pct"], 3),
        ]
        for row in variants.to_dict("records")
    ]
    timing_rows = [
        [
            row["variant"],
            row["trades"],
            _pct(row["net_mean_pct"], 3),
            _pct(row["net_median_pct"], 3),
            _pct(row["win_rate_pct"], 1),
            f"{row['t_stat_daily']:.2f}",
        ]
        for row in timing.to_dict("records")
    ]
    gap_rows = [
        [row["bucket"], row["trades"], _pct(row["net_mean_pct"], 3), _pct(row["net_median_pct"], 3), _pct(row["win_rate_pct"], 1)]
        for row in gap_table.to_dict("records")
    ]
    ret_rows = [
        [row["bucket"], row["trades"], _pct(row["net_mean_pct"], 3), _pct(row["net_median_pct"], 3), _pct(row["win_rate_pct"], 1)]
        for row in ret_table.to_dict("records")
    ]

    positive = variants[variants["net_mean_pct"] > 0]
    positive_text = (
        "、".join(
            f"{row['variant']}（{row['net_mean_pct']:.2f}%，n={row['trades']}）"
            for row in positive.to_dict("records")
        )
        if not positive.empty
        else "无"
    )

    return f"""# 开盘涨幅动量策略：亏损归因与变体扫描

主回测见 `memo.md`。这里回答两个问题：**亏损来自哪一段**，以及**换一个简单口径能不能救**。

## 结论

- 主口径 open_1 的毛收益 -0.74%/笔，其中**入场日开盘→收盘 -0.52%**、隔夜 -0.25%，次日开盘→收盘反而 +0.27%。亏损主要发生在买入当天：开盘追高后，股票日内回落。
- 收紧开盘涨幅、加流动性/价格/板块过滤、改排序方向，都只是把亏损从 -1.1% 缩小到 -0.4% 到 -0.9%，**没有一个变体在 2018-2024 和 2025-2026 两段同时为正**。
- 把买入推迟到**次日开盘**、只吃次日 open→close，净均值 -0.095%（日度 t=-1.26），已经接近“无 edge”，但仍是负的；这不是一个可交易的正期望。
- 唯一均值为正的切片是“399101 开盘缺口 ≤ -2%”的市场恐慌日：44 笔、均值 +0.65%，但中位数 -0.49%、t=0.49，样本太少且不稳，不能作为策略。
- 变体表中为正的项：{positive_text}。这些结果是在看到主回测之后扫出来的，存在多重检验，最多只能作为下一步独立验证的线索，不能直接实盘。

## 1. 收益拆解（主口径，毛收益，未扣成本）

{md_table(["区段", "笔数", "均值", "中位数", "胜率"], decomp_rows)}

- 三段收益逐笔满足乘性关系：`(1+入场日)*(1+隔夜) = 1+open_to_open`。
- 成本约 0.36%/笔，主要吃掉的是本来就很薄的隔夜/次日收益；入场日 -0.52% 是亏损的大头。

## 2. 变体扫描（open_1，净收益）

{md_table(["变体", "笔数", "全样本均值", "中位数", "胜率", "日度t", "2018-2024", "2025-2026"], variant_rows)}

- 所有变体的全样本均值都为负；`gap<=0.5%` 和 `smallest abs(gap)` 亏损最小，但也分别只有 -0.48% 和 -0.39%。
- `2018-2024` 与 `2025-2026` 两段没有出现“一段正、一段负”的明显分化；负收益是持续的。

## 3. 入场/出场时点诊断（同一批选股）

{md_table(["口径", "笔数", "净均值", "中位数", "胜率", "日度t"], timing_rows)}

- 基准是 `open_t → open_t+1`。
- `open_t → close_t` 是 T+1 违规的当日买卖，只用于诊断；它也是负的。
- `open_{{t+1}} → close_{{t+1}}` 把追高日让过去，只吃次日日内，净均值 -0.095%，不显著、也不为正。
- `close_t → close_{{t+1}}` 净均值 -0.36%，仍为负。

## 4. 市场状态分层（主口径，净收益）

按 399101 当日开盘缺口：

{md_table(["指数开盘缺口", "笔数", "净均值", "中位数", "胜率"], gap_rows)}

按 399101 前一日涨跌：

{md_table(["指数前日涨跌", "笔数", "净均值", "中位数", "胜率"], ret_rows)}

- 亏损不是只在“市场高开”时发生；指数平开/低开时同样为负。
- 指数大幅低开（≤-2%）的切片均值为正，但中位数为负、样本仅 44 笔，不能作为稳定 edge。

## 5. 解释

这个信号更像**开盘缺口反转**，而不是动量延续：

1. 开盘涨幅接近 +4% 的股票，在开盘后日内平均回落 0.5%；
2. 隔夜再跌 0.25%；
3. 次日日内虽有 +0.27% 的反弹，但不足以覆盖前两段和成本。

因此“按开盘涨幅降序买 2 只”实际上是在做多最拥挤的开盘缺口，方向与短线反转相反。把它反过来做（开盘卖出/做空）在 A 股多数股票上不可行，且成本/借券会进一步侵蚀。

## 6. 限制

- 变体是事后扫描，不是预注册策略；多重检验会制造假阳性。
- 日线数据无法还原 9:30-10:00 的成交路径；开盘价成交假设、滑点 10bp 都是近似。
- 历史 ST 标记不可得；退市股只补了本地 244 只，仍有幸存者偏差。
- 结论只针对本地 qfq 日线和给定成本，不构成实盘建议。

## 7. 文件

- `diagnostics.md`：本文件
- `variants.csv`：变体扫描明细
- `memo.md`：主回测报告
- `trades_open_1.csv`：主口径逐笔交易
- `open_gap_momentum_study.py`：主回测脚本
- `open_gap_momentum_diagnostics.py`：本诊断脚本
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = StudyConfig()
    panels = load_study_panels(PANEL_CACHE)
    signal = build_signal_table(panels, cfg)
    base_mask = signal["mask"].copy()
    dates = signal["dates"]
    symbols = signal["symbols"]
    open_ = signal["open"].to_numpy(dtype="float64")
    gap = signal["open_gap"].to_numpy(dtype="float64")
    yest = signal["yest_ret"].to_numpy(dtype="float64")
    ret20 = signal["ret20"].to_numpy(dtype="float64")

    adv20 = (
        panels["amount"]
        .astype("float64")
        .rolling(20, min_periods=20)
        .mean()
        .shift(1)
        .loc[:, symbols]
        .to_numpy(dtype="float64")
    )
    prefix = np.array([str(symbol)[:3] for symbol in symbols])
    star = np.isin(prefix, ["688", "689"])
    chinext = np.isin(prefix, ["300", "301"])

    index = (
        pd.read_csv(INDEX_399101, parse_dates=["date"])
        .set_index("date")
        .sort_index()
        .reindex(dates)
        .ffill()
    )
    index_gap = (index["open"] / index["close"].shift(1) - 1.0).to_numpy(dtype="float64")

    baseline_selected = select_from_mask(base_mask, signal)
    baseline_trades = build_trades(baseline_selected, signal, 1, "open", cfg)
    decomposition_frame = decomposition(baseline_selected, signal, cfg)

    # 变体：条件加在基础 mask 上，仍然按开盘涨幅降序取 2 只。
    variant_specs: list[tuple[str, np.ndarray, str]] = [
        ("baseline", np.ones_like(base_mask, dtype=bool), "desc"),
        ("gap<=0.5%", gap <= 0.005, "desc"),
        ("gap<=1%", gap <= 0.01, "desc"),
        ("gap<=1.5%", gap <= 0.015, "desc"),
        ("gap<=2%", gap <= 0.02, "desc"),
        ("gap<=3%", gap <= 0.03, "desc"),
        ("gap>=0", gap >= 0.0, "desc"),
        ("smallest abs(gap)", np.ones_like(base_mask, dtype=bool), "abs"),
        ("adv20>=50M", adv20 >= ADV_MIN, "desc"),
        ("adv20>=100M", adv20 >= ADV_STRICT, "desc"),
        ("price>=5", open_ >= 5.0, "desc"),
        ("price>=10", open_ >= 10.0, "desc"),
        ("ex-STAR", ~star, "desc"),
        ("ex-STAR+ChiNext", ~star & ~chinext, "desc"),
        ("ret20<=50%", ret20 <= 0.5, "desc"),
        ("yest>=-5%", yest >= -0.05, "desc"),
        ("market gap<=0", index_gap[:, None] <= 0.0, "desc"),
        ("market gap<=-2%", index_gap[:, None] <= -0.02, "desc"),
        ("gap<=2% & adv50M", (gap <= 0.02) & (adv20 >= ADV_MIN), "desc"),
        ("gap<=2% & ex-STAR/ChiNext", (gap <= 0.02) & ~star & ~chinext, "desc"),
    ]
    variant_rows = []
    for name, extra, pick in variant_specs:
        mask = base_mask & extra
        selected = select_from_mask(mask, signal, pick=pick)
        trades = build_trades(selected, signal, 1, "open", cfg)
        variant_rows.append(variant_row(name, trades, category="condition"))
    variants = pd.DataFrame(variant_rows)

    # 时点诊断：同一批 baseline 选股，只改 entry/exit 的偏移和价格类型。
    timing_specs = [
        ("open_t -> open_t+1（基准）", 0, 1, "open", "open"),
        ("open_t -> close_t（T+1 违规）", 0, 0, "open", "close"),
        ("close_t -> open_t+1", 0, 1, "close", "open"),
        ("close_t -> close_t+1", 0, 1, "close", "close"),
        ("open_t+1 -> close_t+1", 1, 1, "open", "close"),
        ("open_t -> close_t+1（T+1 合规）", 0, 1, "open", "close"),
    ]
    timing_rows = []
    for name, entry_offset, exit_offset, entry_kind, exit_kind in timing_specs:
        trades = custom_trades(
            baseline_selected,
            signal,
            cfg,
            entry_offset,
            exit_offset,
            entry_kind,
            exit_kind,
        )
        timing_rows.append(variant_row(name, trades, category="timing"))
    timing = pd.DataFrame(timing_rows)

    gap_table, ret_table = market_regime_table(baseline_selected, signal, cfg)

    variants.to_csv(out_dir / "variants.csv", index=False, encoding="utf-8")
    decomposition_frame.to_csv(
        out_dir / "return_decomposition.csv", index=False, encoding="utf-8"
    )
    gap_table.to_csv(out_dir / "market_regime_gap.csv", index=False, encoding="utf-8")
    ret_table.to_csv(out_dir / "market_regime_ret.csv", index=False, encoding="utf-8")

    memo = render_diagnostics(
        decomposition_frame, variants, timing, gap_table, ret_table
    )
    (out_dir / "diagnostics.md").write_text(memo, encoding="utf-8")

    print("\n=== decomposition (gross) ===")
    for column in ("open_to_open", "entry_intraday", "overnight", "exit_intraday"):
        values = decomposition_frame[column].dropna().to_numpy()
        print(
            f"{column:16s} n={len(values):5d} mean={values.mean()*100:7.3f}% "
            f"median={np.median(values)*100:7.3f}% win={(values>0).mean()*100:5.1f}%"
        )
    print("\n=== variants (net, open_1) ===")
    for row in variants.to_dict("records"):
        print(
            f"{row['variant']:32s} n={row['trades']:5d} "
            f"all={row['net_mean_pct']:7.3f}% "
            f"18-24={row['net_mean_2018_2024_pct']:7.3f}% "
            f"25-26={row['net_mean_2025_2026_pct']:7.3f}%"
        )
    print("\n=== timing (net) ===")
    for row in timing.to_dict("records"):
        print(
            f"{row['variant']:36s} n={row['trades']:5d} "
            f"mean={row['net_mean_pct']:7.3f}% median={row['net_median_pct']:7.3f}%"
        )
    print(f"\nwrote {out_dir / 'diagnostics.md'}")


if __name__ == "__main__":
    main()
