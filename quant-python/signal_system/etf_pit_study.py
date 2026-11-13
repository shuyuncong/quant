"""Point-in-time ETF universe study: does survivorship bias explain the ETF edge?

The production pool is a 2026 snapshot of 44 current-listed sector/theme ETFs.
A backtest over 2020-2026 therefore only sees funds that survived.  This study
rebuilds the candidate pool from exchange month-end snapshots, maps historical
funds to the same 44 themes, and compares three arms:

* ``static_current44``: the production snapshot, unchanged.
* ``pit_current_only``: PIT theme map restricted to funds still listed in 2026.
* ``pit_all``: the same map including funds that later delisted or liquidated.

The difference between the last two isolates survivorship; the difference
between the first two shows how the theme mapping itself changes the pool.

The mapping and the one-ETF-per-theme rule are research heuristics, not the
production selector.  Delisted positions are marked at their last available
price; the script does not model a forced liquidation event.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import etf_extended_study as extended
import etf_optimization_study as optimization
from etf_extended_study import BOND, GOLD
from etf_optimization_study import CASH_ETF, NASDAQ_ETF
from etf_rotation.engine import ADV_MIN, CORE, MARKET, SATELLITE
from fetch_etf_pit_prices import is_domestic_equity
from strategy_statistics import paired_sharpe_diff

THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "电力": ("电力",),
    "中药": ("中药",),
    "建材": ("建材",),
    "旅游": ("旅游",),
    "机器人": ("机器人",),
    "农业": ("农业", "农牧"),
    "养殖": ("养殖", "畜牧"),
    "游戏": ("游戏",),
    "化工": ("化工",),
    "机械": ("机械",),
    "创业板": ("创业板",),
    "消费": ("消费",),
    "成长": ("成长",),
    "创新药": ("创新药",),
    "家电": ("家电",),
    "电子": ("电子",),
    "医药": ("医药",),
    "价值": ("价值",),
    "中证1000": ("中证1000", "1000ETF"),
    "医疗": ("医疗",),
    "房地产": ("房地产", "地产"),
    "半导体": ("半导体",),
    "环保": ("环保",),
    "军工": ("军工", "国防"),
    "计算机": ("计算机",),
    "芯片": ("芯片",),
    "银行": ("银行",),
    "证券": ("证券", "券商"),
    "低波": ("低波",),
    "传媒": ("传媒",),
    "科技": ("科技",),
    "5G": ("5G",),
    "人工智能": ("人工智能", "AI"),
    "食品": ("食品",),
    "钢铁": ("钢铁",),
    "软件": ("软件",),
    "光伏": ("光伏",),
    "通信": ("通信",),
    "汽车": ("汽车",),
    "新能源": ("新能源",),
    "云计算": ("云计算",),
    "稀土": ("稀土",),
    "基建": ("基建",),
    "科创50": ("科创50", "科创板50"),
}
ANCHORS = (MARKET, *CORE, CASH_ETF, NASDAQ_ETF)


def map_theme(name: str) -> str | None:
    """Longest matching keyword wins; ties keep the dict insertion order."""
    best_theme: str | None = None
    best_length = -1
    for theme, keywords in THEME_KEYWORDS.items():
        for keyword in keywords:
            if keyword in str(name) and len(keyword) > best_length:
                best_theme, best_length = theme, len(keyword)
    return best_theme


def _masked_series(series: pd.Series, master: pd.Index) -> pd.Series:
    """Forward-fill internal gaps, but never after the fund's last observation."""
    aligned = series.reindex(master)
    last = aligned.last_valid_index()
    if last is None:
        return aligned
    aligned = aligned.ffill()
    aligned.loc[aligned.index > last] = np.nan
    return aligned


def load_pit_panels(price_dir: Path, universe: pd.DataFrame, *, current_only: bool,
                    fallback_dir: Path | None = None,
                    sina_dir: Path | None = None,
                    ) -> tuple[dict[str, str], pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load PIT prices and return theme -> codes, close, open, volume panels."""
    status = universe.set_index("code")["status"]
    names = universe.set_index("code")["name"]
    theme_codes: dict[str, list[str]] = {theme: [] for theme in THEME_KEYWORDS}
    for code, name in names.items():
        if not is_domestic_equity(name):
            continue
        theme = SATELLITE.get(str(code).zfill(6)) or map_theme(name)
        if theme is None:
            continue
        if current_only and status.get(code) != "current":
            continue
        theme_codes[theme].append(str(code).zfill(6))

    wanted = {code for codes in theme_codes.values() for code in codes}
    wanted.update(ANCHORS)
    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for code in sorted(wanted):
        path = price_dir / f"{code}_qfq.pkl"
        if not path.exists() and sina_dir is not None:
            path = sina_dir / f"{code}_qfq.pkl"
        if not path.exists() and fallback_dir is not None:
            path = fallback_dir / f"{code}_qfq.pkl"
        if not path.exists():
            missing.append(code)
            continue
        frame = pd.read_pickle(path)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            missing.append(code)
            continue
        frame = frame[frame["close"].gt(0)].copy()
        frame["datetime"] = pd.to_datetime(frame["datetime"])
        frames[code] = frame.set_index("datetime").sort_index()
    if MARKET not in frames:
        raise RuntimeError(f"missing market anchor {MARKET} in {price_dir}")
    master = frames[MARKET].index
    close = pd.DataFrame({code: _masked_series(frame["close"], master) for code, frame in frames.items()})
    open_ = pd.DataFrame({code: _masked_series(frame["open"], master) for code, frame in frames.items()})
    volume = pd.DataFrame({code: _masked_series(frame["volume"], master) for code, frame in frames.items()})
    theme_codes = {theme: [code for code in codes if code in close.columns]
                   for theme, codes in theme_codes.items()}
    theme_codes = {theme: codes for theme, codes in theme_codes.items() if codes}
    if missing:
        print(f"missing price files: {len(missing)} (e.g. {missing[:8]})", flush=True)
    return theme_codes, close, open_, volume


def build_pit_signals(close: pd.DataFrame, volume: pd.DataFrame,
                      theme_codes: dict[str, list[str]], dates: pd.Index,
                      ) -> dict[pd.Timestamp, dict[str, Any]]:
    """Same 40/35 filters as production, but one representative per theme."""
    panels = extended.build_panels(close, volume)
    cache: dict[pd.Timestamp, dict[str, Any]] = {}
    for day in dates:
        csi_close = float(close[MARKET].loc[day])
        csi_ma200 = float(panels["csi_ma200"].loc[day])
        gate = bool(np.isfinite(csi_ma200) and csi_close > csi_ma200)
        high = float(panels["csi_high252"].loc[day])
        csi_ma20 = float(panels["csi_ma20"].loc[day])
        drawdown = csi_close / high - 1.0 if np.isfinite(high) and high > 0 else 0.0
        crash = bool(drawdown <= -0.20 and np.isfinite(csi_ma20) and csi_close > csi_ma20)

        core_mom: dict[str, float] = {}
        for symbol in (GOLD, BOND):
            mom = float(panels["mom"][symbol].loc[day])
            if np.isfinite(mom) and mom > 0:
                core_mom[symbol] = mom

        candidates: list[tuple[float, str, float, float]] = []
        for theme, codes in theme_codes.items():
            representative: tuple[str, float, float, float] | None = None
            for symbol in codes:
                if symbol not in close.columns:
                    continue
                if int(panels["valid"][symbol].loc[day]) < 253:
                    continue
                adv = float(panels["adv20"][symbol].loc[day])
                if not np.isfinite(adv) or adv < ADV_MIN:
                    continue
                price = float(close[symbol].loc[day])
                ma60 = float(panels["ma60"][symbol].loc[day])
                ma200 = float(panels["ma200"][symbol].loc[day])
                ma200_prev = float(panels["ma200_prev"][symbol].loc[day])
                ret60 = float(panels["ret60"][symbol].loc[day])
                if not (
                    np.isfinite(price) and np.isfinite(ma60) and np.isfinite(ma200)
                    and np.isfinite(ma200_prev) and np.isfinite(ret60)
                    and price > ma200 and ma200 > ma200_prev
                    and price > ma60 and ma60 > ma200 and ret60 > 0
                ):
                    continue
                mom = float(panels["mom"][symbol].loc[day])
                vol = float(panels["vol60"][symbol].loc[day])
                if not np.isfinite(mom) or not np.isfinite(vol) or mom <= 0 or vol <= 0:
                    continue
                score = mom / vol
                if representative is None or score > representative[3]:
                    representative = (symbol, mom, vol, score)
            if representative is None:
                continue
            symbol, mom, vol, score = representative
            candidates.append((score, symbol, mom, vol))
        candidates.sort(key=lambda row: (-row[0], row[1]))
        cache[day] = {"gate": gate, "crash": crash, "drawdown": drawdown,
                      "core_mom": core_mom, "candidates": candidates}
    return cache


def _target_for(signals: dict[pd.Timestamp, dict[str, Any]],
                panels: dict[str, pd.DataFrame]) -> optimization.TargetFn:
    return optimization.runtime_target(signals, panels)


def _run(close_mark: pd.DataFrame, open_mark: pd.DataFrame,
         target: optimization.TargetFn, dates: pd.Index, *, label: str,
         slippage: float) -> extended.RunResult:
    return extended.execute(
        close_mark, open_mark, dates=dates, target_fn=target,
        rebalance="W", slippage=slippage, label=label, band=0.0,
    )


def _row(result: extended.RunResult, stress: extended.RunResult) -> dict[str, Any]:
    periods = {
        "full": (None, None),
        "design": (None, optimization.DESIGN_END),
        "holdout": (optimization.HOLDOUT_START, None),
    }
    headline = result.metrics()
    return {
        **{name: optimization.curve_metrics(result.curve, start, end)
           for name, (start, end) in periods.items()},
        "yearly_pct": headline["yearly"],
        "avg_exposure": headline["avg_exposure"],
        "trades": headline["trades"],
        "turnover_x": headline["turnover_x"],
        "stress_20bp": optimization.curve_metrics(stress.curve),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--universe", type=Path, default=Path("cache/etf_pit/universe.csv"))
    parser.add_argument("--prices", type=Path, default=Path("cache/etf_pit/prices"))
    parser.add_argument("--research-dir", type=Path, default=Path("cache/etf_research_long"),
                        help="fallback cache for current-listed anchors")
    parser.add_argument("--sina-dir", type=Path, default=Path("cache/etf_pit/prices_sina"),
                        help="fallback cache for delisted ETFs")
    parser.add_argument("--out", type=Path, default=Path("cache/etf_pit/study"))
    parser.add_argument("--start", default=optimization.START)
    parser.add_argument("--end", default=optimization.END)
    parser.add_argument("--n-boot", type=int, default=5000)
    args = parser.parse_args(argv)

    universe = pd.read_csv(args.universe, dtype={"code": str})
    universe["code"] = universe["code"].str.zfill(6)
    args.out.mkdir(parents=True, exist_ok=True)

    all_themes, all_close, all_open, all_volume = load_pit_panels(
        args.prices, universe, current_only=False,
        fallback_dir=args.research_dir, sina_dir=args.sina_dir)
    cur_themes, cur_close, cur_open, cur_volume = load_pit_panels(
        args.prices, universe, current_only=True,
        fallback_dir=args.research_dir, sina_dir=args.sina_dir)
    dates = all_close.loc[pd.Timestamp(args.start):pd.Timestamp(args.end)].index
    print(f"themes all={len(all_themes)} current={len(cur_themes)} days={len(dates)}", flush=True)

    all_panels = extended.build_panels(all_close, all_volume)
    cur_panels = extended.build_panels(cur_close, cur_volume)
    missing_static = sorted(set(SATELLITE) - set(all_close.columns))
    if missing_static:
        raise RuntimeError(f"static production pool missing from PIT prices: {missing_static}")
    arms: dict[str, tuple[optimization.TargetFn, extended.RunResult]] = {}
    arm_panels: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {
        "static_current44": (all_close, all_open),
        "pit_current_only": (cur_close, cur_open),
        "pit_all": (all_close, all_open),
    }
    signals_by_arm: dict[str, dict[pd.Timestamp, dict[str, Any]]] = {}

    static_signals = extended.build_signals(all_close, SATELLITE, all_panels, dates)
    static_target = _target_for(static_signals, all_panels)
    static_result = _run(
        all_close.ffill(), all_open.ffill(), static_target, dates,
        label="static_current44", slippage=optimization.SLIPPAGE,
    )
    arms["static_current44"] = (static_target, static_result)
    for label, themes, close, open_, volume, panels in (
        ("pit_current_only", cur_themes, cur_close, cur_open, cur_volume, cur_panels),
        ("pit_all", all_themes, all_close, all_open, all_volume, all_panels),
    ):
        signals = build_pit_signals(close, volume, themes, dates)
        signals_by_arm[label] = signals
        target = _target_for(signals, panels)
        arms[label] = (target, _run(
            close.ffill(), open_.ffill(), target, dates,
            label=label, slippage=optimization.SLIPPAGE,
        ))

    rows: dict[str, dict[str, Any]] = {}
    for label, (target, result) in arms.items():
        close_mark, open_mark = arm_panels[label]
        stress = _run(
            close_mark.ffill(), open_mark.ffill(), target, dates,
            label=f"{label}_20bp", slippage=optimization.STRESS_SLIPPAGE,
        )
        rows[label] = _row(result, stress)
        full = rows[label]["full"]
        print(f"{label:<18} ret {full['total_return_pct']:>7.2f}%  cagr {full['cagr_pct']:>5.2f}%  "
              f"sharpe {full['sharpe']:>5.3f}  dd {full['max_drawdown_pct']:>7.2f}%  "
              f"turn {rows[label]['turnover_x']:>6.2f}x  20bp {rows[label]['stress_20bp']['cagr_pct']:>5.2f}%",
              flush=True)

    base_returns = arms["static_current44"][1].curve.pct_change().dropna()
    for label in ("pit_current_only", "pit_all"):
        returns = arms[label][1].curve.pct_change().dropna()
        rows[label]["vs_static"] = {
            period: paired_sharpe_diff(
                returns.loc[start:end], base_returns.loc[start:end], n_boot=args.n_boot)
            for period, (start, end) in {
                "full": (None, None), "design": (None, optimization.DESIGN_END),
                "holdout": (optimization.HOLDOUT_START, None),
            }.items()
        }
    pit_returns = arms["pit_all"][1].curve.pct_change().dropna()
    current_returns = arms["pit_current_only"][1].curve.pct_change().dropna()
    rows["pit_all"]["vs_pit_current_only"] = {
        period: paired_sharpe_diff(
            pit_returns.loc[start:end], current_returns.loc[start:end], n_boot=args.n_boot)
        for period, (start, end) in {
            "full": (None, None), "design": (None, optimization.DESIGN_END),
            "holdout": (optimization.HOLDOUT_START, None),
        }.items()
    }

    stale_codes = set(universe.loc[universe["status"] != "current", "code"])
    stale_in_map = {code for codes in all_themes.values() for code in codes if code in stale_codes}
    stale_with_price = {code for code in stale_in_map if code in all_close.columns}
    stale_candidates: set[str] = set()
    stale_top: set[str] = set()
    for signal in signals_by_arm.get("pit_all", {}).values():
        for row in signal["candidates"]:
            if row[1] in stale_codes:
                stale_candidates.add(row[1])
        if signal["candidates"] and signal["candidates"][0][1] in stale_codes:
            stale_top.add(signal["candidates"][0][1])

    payload = {
        "meta": {
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "universe": str(args.universe),
            "prices": str(args.prices),
            "start": str(dates[0].date()),
            "end": str(dates[-1].date()),
            "design_end": optimization.DESIGN_END,
            "holdout_start": optimization.HOLDOUT_START,
            "n_boot": args.n_boot,
            "theme_keywords": {theme: list(keywords) for theme, keywords in THEME_KEYWORDS.items()},
            "adv_min": ADV_MIN,
            "pit_codes_all": sum(len(codes) for codes in all_themes.values()),
            "pit_codes_current": sum(len(codes) for codes in cur_themes.values()),
            "stale_codes_in_theme_map": len(stale_in_map),
            "stale_codes_with_price": len(stale_with_price),
            "stale_codes_ever_candidate": len(stale_candidates),
            "stale_codes_ever_top": len(stale_top),
            "limitations": [
                "annual exchange snapshots only; first/last seen is approximate",
                "theme mapping is keyword-based and not the production selector",
                "one representative per theme is chosen by 12-1/vol score",
                "delisted positions are marked at their last price; no forced liquidation",
                "only delisted peers of the 44 production themes were fetched; current peers beyond the production pool are absent",
            ],
        },
        "arms": rows,
    }
    (args.out / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    pd.DataFrame({label: result.curve for label, (_, result) in arms.items()}).to_csv(
        args.out / "curves.csv")
    metrics = []
    for label, row in rows.items():
        metrics.append({
            "arm": label,
            "cagr_pct": row["full"]["cagr_pct"],
            "sharpe": row["full"]["sharpe"],
            "max_drawdown_pct": row["full"]["max_drawdown_pct"],
            "design_cagr_pct": row["design"]["cagr_pct"],
            "design_sharpe": row["design"]["sharpe"],
            "holdout_cagr_pct": row["holdout"]["cagr_pct"],
            "holdout_sharpe": row["holdout"]["sharpe"],
            "stress_20bp_cagr_pct": row["stress_20bp"]["cagr_pct"],
            "turnover_x": row["turnover_x"],
        })
    pd.DataFrame(metrics).to_csv(args.out / "metrics.csv", index=False)
    print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
