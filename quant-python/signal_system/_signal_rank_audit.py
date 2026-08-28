"""Signal-layer ranking-quality audit for P1 vs P5a family.

Compares how well each scoring mode orders same-day candidates by their
independently simulated pnl. Uses the exact candidate pipeline as the CLI
(signal policy partition -> position gate -> stock pool -> simulate_signal_mode).

Only contested days (>=2 same entry_day candidates) are analyzed. buy_1/2/3
candidates are separated (they carry no P5a features).

Metrics per mode:
  1. Top-K selection: PF / win rate / avg / median pnl / MFE / MAE / ex-top1 /
     ex-top3 for Top1, Top4, rest, all
  2. Rank correlation: daily Spearman(score, pnl), day-equal-weighted IC
  3. Quantile monotonicity: high/mid/low score terciles PF
  4. Pairwise accuracy: fraction of same-day pairs where higher score => higher pnl
  5. Stability: by quarter, by regime, by signal type, raw vs winsorized(95%)
"""
import sys, json, os
import statistics
from collections import defaultdict
from pathlib import Path
import datetime as dt

import pandas as pd

sys.path.insert(0, "D:/development/github/quant/quant-python/signal_system")
os.chdir("D:/development/github/quant/quant-python/signal_system")

import yaml
from backtest_winrate import (
    build_market_gate, find_signals, load_backtest_history,
    load_stock_pool_history, apply_stock_position_gate, simulate_signal_mode,
    _execution_values, _candidate_score, _apply_p5a_cross_sectional_ranks,
)
from strategy.signal_policy import (
    resolve_signal_execution_policy, partition_entry_signals,
)
from strategy.stock_pool import filter_buy_events
from utils.helpers import load_config

CONFIG = load_config(Path("config/config.yaml"))
config = yaml.safe_load(open("config/config.yaml", encoding="utf-8"))
execution = _execution_values(config.get("execution", {}) if isinstance(config.get("execution"), dict) else {})

RANK = {name: i for i, name in enumerate(
    ("macd_golden_cross_pullback_confirmed_above",
     "macd_golden_cross_pullback_confirmed_near",
     "buy_1", "buy_2", "buy_3"))}

WINDOWS = {
    "train": (dt.date(2024, 9, 1), dt.date(2025, 6, 30)),
    "h2_2025": (dt.date(2025, 7, 1), dt.date(2025, 12, 31)),
    "h1_2026": (dt.date(2026, 1, 1), dt.date(2026, 6, 30)),
}
MODES = ["P1", "P5a", "P5a-C", "P5a-G", "P5a-Z", "P5a-CG", "P5a-CZ", "P5a-CGZ"]
SCORE_MODES = ["P1"] + [m for m in MODES if m != "P1"]

def score_of(candidate, mode):
    return _candidate_score(candidate, mode, RANK)


def collect_candidates(start, end):
    """Reproduce the CLI candidate pipeline for one window."""
    idx = pd.read_pickle("cache/index_000001_sh.pkl")
    gate = build_market_gate(idx, config)
    regime_lookup = {
        k: v["regime"] for k, v in gate.items() if v["regime"] in ("bull", "range", "bear")
    }
    policy = resolve_signal_execution_policy(config)
    stock_pool_settings = config.get("stock_pool", {}) or {}
    market_gate_enabled = bool((config.get("entry_filters") or {}).get("market_gate_enabled", False))

    files = sorted(Path("cache/daily_history").glob("*_none.pkl"))
    all_candidates = []
    failed = 0
    ok = 0
    for path in files:
        symbol = path.name.split("_")[0]
        try:
            closed, _src = load_backtest_history(
                symbol, adjustment="qfq", config=config, history_bars=800,
                end=end, fetch_missing=False)
        except Exception:
            failed += 1
            continue
        if closed is None or closed.empty:
            continue
        ok += 1
        closed = closed[pd.to_datetime(closed["datetime"]).dt.date <= end].reset_index(drop=True)
        if len(closed) < 60:
            continue
        events = find_signals(closed, config)
        events = {
            "buy": [e for e in events.get("buy", []) if start <= dt.date.fromisoformat(str(e["day"])) <= end],
            "sell": list(events.get("sell", [])),
        }
        en, obs, dis = partition_entry_signals(events["buy"], policy, regime_lookup=regime_lookup)
        events["buy"] = en
        events, _pos_skipped = apply_stock_position_gate(closed, events, config)
        if stock_pool_settings.get("enabled") and events.get("buy"):
            try:
                sph = load_stock_pool_history(symbol, config=config, history_bars=800, end=end)
            except Exception:
                sph = pd.DataFrame()
            events, _sk, _det = filter_buy_events(sph, events, config)
        result = simulate_signal_mode(
            symbol, closed, events, start, end, execution,
            market_gate=gate, market_gate_enabled=market_gate_enabled,
            allow_incomplete=False,
        )
        for trade in result["trades"]:
            trade["symbol"] = symbol
        all_candidates.extend(result["trades"])
    return all_candidates, ok, failed


def mfe_mae(trade):
    marks = trade.get("_mark_prices") or {}
    if not marks:
        return None, None
    entry = float(trade["entry_price"])
    if entry <= 0:
        return None, None
    ratios = [float(v) / entry - 1.0 for v in marks.values()]
    if not ratios:
        return None, None
    return max(ratios) * 100.0, min(ratios) * 100.0


def pnl_of(trade):
    return float(trade.get("pnl_pct") or 0.0)


def block_stats(trades):
    if not trades:
        return None
    pnls = [pnl_of(t) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    mfes = [m for m, _ in (mfe_mae(t) for t in trades) if m is not None]
    maes = [m for _, m in (mfe_mae(t) for t in trades) if m is not None]
    return {
        "n": len(trades),
        "pf": round(gross_profit / gross_loss, 3) if gross_loss else (None if gross_profit == 0 else 999.0),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "avg_pnl": round(sum(pnls) / len(pnls), 3),
        "median_pnl": round(float(pd.Series(pnls).median()), 3),
        "mfe_median": round(float(pd.Series(mfes).median()), 3) if mfes else None,
        "mae_median": round(float(pd.Series(maes).median()), 3) if maes else None,
        "sum_pnl": round(sum(pnls), 2),
    }


def winsorize(values, q=0.025):
    if not values:
        return values
    s = sorted(values)
    lo = s[int(q * (len(s) - 1))]
    hi = s[int((1 - q) * (len(s) - 1))]
    return [min(max(v, lo), hi) for v in values]


def spearman(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    rx = {v: i for i, v in enumerate(sorted(xs))}
    ry = {v: i for i, v in enumerate(sorted(ys))}
    dx = [rx[v] for v in xs]
    dy = [ry[v] for v in ys]
    mx = sum(dx) / n
    my = sum(dy) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(dx, dy))
    vx = sum((a - mx) ** 2 for a in dx) ** 0.5
    vy = sum((b - my) ** 2 for b in dy) ** 0.5
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def audit_window(label, start, end):
    cands, ok, failed = collect_candidates(start, end)
    # group by entry_day (contested days only)
    by_day = defaultdict(list)
    for c in cands:
        if c.get("entry_day"):
            by_day[str(c["entry_day"])].append(c)
    contested = {d: cs for d, cs in by_day.items() if len(cs) >= 2}

    # split macd vs buy
    macd_days = {}
    buy_days = {}
    for d, cs in contested.items():
        m = [c for c in cs if str(c["signal_type"]).startswith("macd")]
        b = [c for c in cs if not str(c["signal_type"]).startswith("macd")]
        if len(m) >= 2:
            macd_days[d] = m
        if len(b) >= 2:
            buy_days[d] = b

    results = {}
    # Precompute cross-sectional percentile ranks per entry-day group for the
    # P5a variants (mirrors _merge_portfolio_candidates behavior).
    rank_candidates = list({id(c): c for c in [cc for cs in macd_days.values() for cc in cs]}.values())
    _apply_p5a_cross_sectional_ranks(rank_candidates)
    for mode in MODES:
        r = {"top_k": {}, "ic": None, "terciles": None, "pairwise": None, "by_quarter": {}, "by_regime": {}, "by_type": {}, "winsorized": None}
        # --- Top-K on macd contested days
        top1_all = []
        top4_all = []
        rest_all = []
        all_macd = []
        ic_vals = []
        terc_hi = []
        terc_mid = []
        terc_lo = []
        pairs_correct = 0
        pairs_total = 0
        by_quarter = defaultdict(list)
        by_regime = defaultdict(list)
        for day, cs in macd_days.items():
            scored = sorted(cs, key=lambda c: (-score_of(c, mode), str(c["symbol"])))
            all_macd.extend(scored)
            top1_all.append(scored[0])
            top4_all.extend(scored[:4])
            rest_all.extend(scored[4:])
            quarter = day[:7]
            by_quarter[quarter].extend(scored)
            regime = (scored[0].get("market_context") or {}).get("regime") or "unknown"
            by_regime[regime].extend(scored)
            # IC
            pnls = [pnl_of(c) for c in cs]
            scores = [score_of(c, mode) for c in cs]
            rho = spearman(scores, pnls)
            if rho is not None:
                ic_vals.append(rho)
            # terciles
            order = sorted(cs, key=lambda c: score_of(c, mode))
            n = len(order)
            third = max(n // 3, 1)
            terc_lo.extend(order[:third])
            terc_mid.extend(order[third:2 * third] if n >= 2 * third else [])
            terc_hi.extend(order[2 * third:])
            # pairwise
            for i in range(len(cs)):
                for j in range(i + 1, len(cs)):
                    a, b = cs[i], cs[j]
                    sa, sb = score_of(a, mode), score_of(b, mode)
                    pa, pb = pnl_of(a), pnl_of(b)
                    if sa == sb:
                        continue
                    if (sa > sb) == (pa > pb):
                        pairs_correct += 1
                    pairs_total += 1
        r["top_k"] = {
            "top1": block_stats(top1_all),
            "top4": block_stats(top4_all),
            "rest": block_stats(rest_all),
            "all": block_stats(all_macd),
        }
        # ex top1 / top3
        pnls_all = [pnl_of(c) for c in all_macd]
        ordered_pnls = sorted(pnls_all, reverse=True)
        r["top_k"]["all_ex_top1"] = block_stats([c for c in all_macd if pnl_of(c) < ordered_pnls[0]])
        r["top_k"]["all_ex_top3"] = block_stats([c for c in all_macd if pnl_of(c) < ordered_pnls[2]])
        if ic_vals:
            r["ic"] = {
                "days": len(ic_vals),
                "mean": round(sum(ic_vals) / len(ic_vals), 4),
                "median": round(float(pd.Series(ic_vals).median()), 4),
                "pct_positive": round(sum(1 for v in ic_vals if v > 0) / len(ic_vals) * 100, 1),
            }
        if terc_hi and terc_mid and terc_lo:
            r["terciles"] = {
                "high": block_stats(terc_hi),
                "mid": block_stats(terc_mid),
                "low": block_stats(terc_lo),
            }
        if pairs_total:
            r["pairwise"] = {
                "pairs": pairs_total,
                "accuracy_pct": round(pairs_correct / pairs_total * 100, 2),
            }
        r["by_quarter"] = {q: block_stats(v) for q, v in sorted(by_quarter.items()) if v}
        r["by_regime"] = {rg: block_stats(v) for rg, v in by_regime.items() if v}
        # winsorized IC + pairwise
        win_ics = []
        win_pairs_correct = 0
        win_pairs_total = 0
        for day, cs in macd_days.items():
            pnls = winsorize([pnl_of(c) for c in cs])
            scores = [score_of(c, mode) for c in cs]
            rho = spearman(scores, pnls)
            if rho is not None:
                win_ics.append(rho)
            for i in range(len(cs)):
                for j in range(i + 1, len(cs)):
                    if scores[i] == scores[j]:
                        continue
                    if (scores[i] > scores[j]) == (pnls[i] > pnls[j]):
                        win_pairs_correct += 1
                    win_pairs_total += 1
        if win_ics:
            r["winsorized"] = {
                "ic_median": round(float(pd.Series(win_ics).median()), 4),
                "ic_pct_positive": round(sum(1 for v in win_ics if v > 0) / len(win_ics) * 100, 1),
                "pairwise_pct": round(win_pairs_correct / win_pairs_total * 100, 2) if win_pairs_total else None,
            }
        results[mode] = r

    # buy_* contest separately (P1 only relevance; they have no P5a features)
    buy_stats = None
    if buy_days:
        all_buy = [c for cs in buy_days.values() for c in cs]
        buy_stats = block_stats(all_buy)

    return {
        "window": label,
        "symbols_ok": ok,
        "symbols_failed": failed,
        "macd_contested_days": len(macd_days),
        "buy_contested_days": len(buy_days),
        "macd_candidates_in_contested": sum(len(v) for v in macd_days.values()),
        "buy_stats": buy_stats,
        "modes": results,
    }


def main():
    out = {}
    for label, (start, end) in WINDOWS.items():
        print(f"=== {label} ===", flush=True)
        out[label] = audit_window(label, start, end)
        json.dump(out, open("bt_exec/p5a_signal_audit.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("saved bt_exec/p5a_signal_audit.json")


if __name__ == "__main__":
    main()