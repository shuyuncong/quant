"""P5a feature audit — no weight optimization.

Checks whether the continuous features add real differentiation or just
float noise. Operates on the candidate layer (find_signals output), not
the portfolio, except for top4 turnover which mirrors portfolio daily
selection on the true entry day.

Report fields:
  feature_formula        exact formulas used
  contribution_ranges    P1 base / confirm / gap / zero contribution ranges
  feature_percentiles    P1/P10/P50/P90/P99 of dif_dea_gap and zero_dist
  by_regime              feature + tie stats grouped by regime
  by_signal_type         feature + tie stats grouped by signal type
  tie_by_group           same-day tie rate for MACD vs buy_* candidates
  precision              tie rate & top4 overlap at raw/4/3/2 decimal scores
  perturbation           top4 candidate overlap after +-1% feature perturbation
  spearman               P5a vs P1 rank correlation within same-day groups
  top4_turnover          daily top4 candidate set turnover across entry days
"""
import sys, json, os, math
from collections import defaultdict
from pathlib import Path
import pandas as pd
import datetime as dt

sys.path.insert(0, "D:/development/github/quant/quant-python/signal_system")
os.chdir("D:/development/github/quant/quant-python/signal_system")

import yaml
from backtest_winrate import (
    build_market_gate, find_signals, load_backtest_history, _candidate_score,
)

config = yaml.safe_load(open("config/config.yaml", encoding="utf-8"))
idx = pd.read_pickle("cache/index_000001_sh.pkl")
gate = build_market_gate(idx, config)
regime_lookup = {
    k: v["regime"] for k, v in gate.items() if v["regime"] in ("bull", "range", "bear")
}
rank = {name: i for i, name in enumerate(
    ("macd_golden_cross_pullback_confirmed_above",
     "macd_golden_cross_pullback_confirmed_near",
     "buy_1", "buy_2", "buy_3"))}

START = "2024-09-01"
END = "2026-08-24"


def _next_bar(dates, day):
    """First trading day strictly after `day`."""
    for d in dates:
        if d > day:
            return d
    return None


# ---------------------------------------------------------------- collect
files = sorted(Path("cache/daily_history").glob("*_none.pkl"))
candidates = []   # every signal candidate in window
failed = 0
ok = 0
for f in files:
    sym = f.name.split("_")[0]
    try:
        closed, _src = load_backtest_history(
            sym, adjustment="qfq", config=config, history_bars=800,
            end=dt.date(2026, 8, 24), fetch_missing=False)
    except Exception:
        failed += 1
        continue
    if closed is None or closed.empty:
        continue
    ok += 1
    closed = closed[pd.to_datetime(closed["datetime"]).dt.date <= dt.date(2026, 8, 24)].reset_index(drop=True)
    dates = [_d for _d in pd.to_datetime(closed["datetime"]).dt.date]
    events = find_signals(closed, config)
    for b in events.get("buy", []):
        d = pd.Timestamp(str(b["day"])).date()
        if not (pd.Timestamp(START).date() <= d <= pd.Timestamp(END).date()):
            continue
        entry = _next_bar(dates, d)
        b["symbol"] = sym
        b["_entry_day"] = entry.isoformat() if entry else None
        b["_regime"] = regime_lookup.get(str(b["day"]))
        candidates.append(b)

print(f"symbols ok={ok} failed={failed} candidates={len(candidates)}", flush=True)

# ------------------------------------------------------------- feature calc
def p1_score(c):
    return _candidate_score(c, "P1", rank)

def p5a_score(c):
    return _candidate_score(c, "P5a", rank)

def gap_score(c):
    f = c.get("_p5a_features") or {}
    return min(float(f.get("dif_dea_gap") or 0.0) / 0.05, 1.0)

def zero_score(c):
    f = c.get("_p5a_features") or {}
    return 1.0 - min(float(f.get("zero_dist") or 0.0) / 0.10, 1.0)

macd_cands = [c for c in candidates if str(c["signal_type"]).startswith("macd")]
buy_cands = [c for c in candidates if not str(c["signal_type"]).startswith("macd")]

# ------------------------------------------------------------ contribution
p1_scores = [p1_score(c) for c in candidates]
p5a_scores = [p5a_score(c) for c in candidates]
gaps = [float((c.get("_p5a_features") or {}).get("dif_dea_gap") or 0.0) for c in macd_cands]
zdists = [float((c.get("_p5a_features") or {}).get("zero_dist") or 0.0) for c in macd_cands]
gap_scores = [gap_score(c) * 30.0 for c in macd_cands]
zero_scores = [zero_score(c) * 20.0 for c in macd_cands]

def pctile(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1)))))
    return round(float(s[k]), 8)

feature_percentiles = {
    "dif_dea_gap": {p: pctile(gaps, p) for p in (1, 10, 50, 90, 99)},
    "zero_dist": {p: pctile(zdists, p) for p in (1, 10, 50, 90, 99)},
    "gap_score_x30": {p: pctile(gap_scores, p) for p in (1, 10, 50, 90, 99)},
    "zero_score_x20": {p: pctile(zero_scores, p) for p in (1, 10, 50, 90, 99)},
    "P1_score": {p: pctile(p1_scores, p) for p in (1, 10, 50, 90, 99)},
    "P5a_score": {p: pctile(p5a_scores, p) for p in (1, 10, 50, 90, 99)},
}
contribution_ranges = {
    "P1_base_zone": {"above": 300, "near": 200, "buy1": 100, "buy2": 90, "buy3": 80},
    "confirm_count": {"min": min((c.get("confirmation_count") or 0) for c in candidates),
                      "max": max((c.get("confirmation_count") or 0) for c in candidates),
                      "x10_per_count": 10.0},
    "gap_score_x30": {"min": min(gap_scores), "max": max(gap_scores)},
    "zero_score_x20": {"min": min(zero_scores), "max": max(zero_scores)},
    "P1_observed_range": [round(min(p1_scores), 2), round(max(p1_scores), 2)],
    "P5a_observed_range": [round(min(p5a_scores), 2), round(max(p5a_scores), 2)],
}

# --------------------------------------------------------------- by regime
def stat_block(cs):
    gs = [gap_score(c) * 30 for c in cs]
    zs = [zero_score(c) * 20 for c in cs]
    return {
        "n": len(cs),
        "gap_x30_median": round(pd.Series(gs).median(), 4) if gs else None,
        "zero_x20_median": round(pd.Series(zs).median(), 4) if zs else None,
    }

by_regime = {}
for reg in ("bull", "range", "bear", "unknown"):
    cs = [c for c in candidates if (c.get("_regime") or "unknown") == reg]
    by_regime[reg] = stat_block(cs)

by_signal_type = {}
for st in sorted({c["signal_type"] for c in candidates}):
    cs = [c for c in candidates if c["signal_type"] == st]
    by_signal_type[st] = stat_block(cs)

# ------------------------------------------------------- tie by group type
# same-day contest groups: (entry_day, regime) -> candidates
groups = defaultdict(list)
for c in candidates:
    if c.get("_entry_day"):
        groups[c["_entry_day"]].append(c)

def tie_pct(cs, score_fn):
    by_sym_day = defaultdict(list)
    for c in cs:
        by_sym_day[(c["symbol"], str(c["day"]))].append(c)
    merged = []
    for items in by_sym_day.values():
        m = dict(items[0])
        m["_score"] = score_fn(items[0])
        merged.append(m)
    # contest by entry day
    by_entry = defaultdict(list)
    for m in merged:
        by_entry[m["_entry_day"]].append(m)
    contested = 0
    tied = 0
    for day, ms in by_entry.items():
        if len(ms) < 2:
            continue
        contested += 1
        vals = [m["_score"] for m in ms]
        if len(set(vals)) < len(vals):
            tied += 1
    return (tied, contested)

macd_tie, macd_cont = tie_pct(macd_cands, p5a_score)
buy_tie, buy_cont = tie_pct(buy_cands, p5a_score)
all_tie, all_cont = tie_pct(candidates, p5a_score)

# ------------------------------------------------------------- precision
def round_scores(cs, digits):
    by_sym_day = defaultdict(list)
    for c in cs:
        by_sym_day[(c["symbol"], str(c["day"]))].append(c)
    merged = []
    for items in by_sym_day.values():
        m = dict(items[0])
        m["_score"] = round(p5a_score(items[0]), digits)
        merged.append(m)
    by_entry = defaultdict(list)
    for m in merged:
        by_entry[m["_entry_day"]].append(m)
    return by_entry

def top4_by_day(by_entry, n=4):
    tops = {}
    for day, ms in by_entry.items():
        order = sorted(ms, key=lambda m: (-m["_score"], str(m["symbol"])))
        tops[day] = frozenset(m["symbol"] for m in order[:n])
    return tops

raw_by_entry = round_scores(candidates, 6)
raw_top4 = top4_by_day(raw_by_entry)
precision = {}
for digits in (6, 4, 3, 2):
    be = round_scores(candidates, digits)
    tie_days = sum(1 for d, ms in be.items() if len(ms) >= 2 and len({m["_score"] for m in ms}) < len(ms))
    cont = sum(1 for d, ms in be.items() if len(ms) >= 2)
    tops = top4_by_day(be)
    common = sum(1 for d in raw_top4 if d in tops and raw_top4[d] == tops[d])
    overlap_days = sum(1 for d in raw_top4 if d in tops)
    precision[digits] = {
        "tie_days": tie_days,
        "contested_days": cont,
        "tie_pct": round(tie_days / cont * 100, 1) if cont else None,
        "top4_exact_match": round(common / overlap_days * 100, 1) if overlap_days else None,
    }

# ---------------------------------------------------------- perturbation
def perturbed_top4(pf):
    """top4 sets after perturbing gap/zero by factor (1+pf)."""
    by_entry = defaultdict(list)
    for c in candidates:
        if not c.get("_entry_day"):
            continue
        f = c.get("_p5a_features")
        c2 = dict(c)
        if f:
            f2 = dict(f)
            f2["dif_dea_gap"] = f2["dif_dea_gap"] * (1 + pf)
            f2["zero_dist"] = f2["zero_dist"] * (1 + pf)
            c2["_p5a_features"] = f2
        c2["_score"] = _candidate_score(c2, "P5a", rank)
        by_entry[c2["_entry_day"]].append(c2)
    tops = {}
    for day, ms in by_entry.items():
        order = sorted(ms, key=lambda m: (-m["_score"], str(m["symbol"])))
        tops[day] = frozenset(m["symbol"] for m in order[:4])
    return tops

perturbation = {}
for pf in (-0.01, 0.01):
    tops = perturbed_top4(pf)
    common = sum(1 for d in raw_top4 if d in tops and raw_top4[d] == tops[d])
    overlap = sum(1 for d in raw_top4 if d in tops)
    perturbation[f"{pf:+.2f}"] = {
        "top4_exact_match_pct": round(common / overlap * 100, 1) if overlap else None,
    }

# -------------------------------------------------------------- spearman
spearman_groups = []
for day, ms in raw_by_entry.items():
    if len(ms) < 2:
        continue
    by_sym_day = defaultdict(list)
    for c in candidates:
        if c.get("_entry_day") == day:
            by_sym_day[(c["symbol"], str(c["day"]))].append(c)
    merged = []
    for items in by_sym_day.values():
        merged.append(items[0])
    if len(merged) < 2:
        continue
    r1 = {id(m): p1_score(m) for m in merged}
    r5 = {id(m): p5a_score(m) for m in merged}
    order1 = [id(m) for m in sorted(merged, key=lambda m: (-r1[id(m)], str(m["symbol"])))]
    order5 = [id(m) for m in sorted(merged, key=lambda m: (-r5[id(m)], str(m["symbol"])))]
    # spearman rank corr
    n = len(merged)
    pos1 = {oid: i for i, oid in enumerate(order1)}
    pos5 = {oid: i for i, oid in enumerate(order5)}
    d2 = sum((pos1[oid] - pos5[oid]) ** 2 for oid in order1)
    rho = 1 - 6 * d2 / (n * (n * n - 1))
    spearman_groups.append(rho)

# -------------------------------------------------------------- turnover
top4_sets = sorted((d, s) for d, s in raw_top4.items() if d is not None)
prev = None
overlaps = []
for day, sset in top4_sets:
    if prev is not None:
        inter = len(sset & prev)
        overlaps.append(inter / len(prev) if prev else None)
    prev = sset
turnover = {
    "days": len(top4_sets),
    "avg_daily_overlap_with_prev": round(sum(o for o in overlaps if o is not None) / len([o for o in overlaps if o is not None]), 3) if overlaps else None,
    "avg_daily_turnover_pct": round((1 - sum(o for o in overlaps if o is not None) / len([o for o in overlaps if o is not None])) * 100, 1) if overlaps else None,
    "distinct_symbols_over_all_days": len(set().union(*[s for _, s in top4_sets])) if top4_sets else 0,
}

report = {
    "window": [START, END],
    "symbols_ok": ok,
    "symbols_failed": failed,
    "candidates_total": len(candidates),
    "macd_candidates": len(macd_cands),
    "buy_candidates": len(buy_cands),
    "feature_formula": {
        "dif_dea_gap": "abs(DIF - DEA) / close  (confirmation day close)",
        "zero_dist": "abs(DIF) / close",
        "gap_score": "min(gap / 0.05, 1.0) * 30",
        "zero_score": "(1 - min(zero_dist / 0.10, 1.0)) * 20",
        "P1_score": "base_zone + confirmation_count * 10",
        "P5a_score": "P1_score + gap_score + zero_score",
    },
    "contribution_ranges": contribution_ranges,
    "feature_percentiles": feature_percentiles,
    "by_regime": by_regime,
    "by_signal_type": by_signal_type,
    "tie_by_group": {
        "all": {"tie_days": all_tie, "contested": all_cont,
                "tie_pct": round(all_tie / all_cont * 100, 1) if all_cont else None},
        "macd": {"tie_days": macd_tie, "contested": macd_cont,
                 "tie_pct": round(macd_tie / macd_cont * 100, 1) if macd_cont else None},
        "buy_1_2_3": {"tie_days": buy_tie, "contested": buy_cont,
                      "tie_pct": round(buy_tie / buy_cont * 100, 1) if buy_cont else None},
    },
    "precision": precision,
    "perturbation": perturbation,
    "spearman": {
        "groups": len(spearman_groups),
        "mean_rho": round(sum(spearman_groups) / len(spearman_groups), 4) if spearman_groups else None,
        "median_rho": round(float(pd.Series(spearman_groups).median()), 4) if spearman_groups else None,
        "pct_rho_lt_0": round(sum(1 for r in spearman_groups if r < 0) / len(spearman_groups) * 100, 1) if spearman_groups else None,
    },
    "top4_turnover": turnover,
}
os.makedirs("bt_exec", exist_ok=True)
json.dump(report, open("bt_exec/p5a_audit.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps({k: v for k, v in report.items() if k != "by_signal_type"}, ensure_ascii=False, indent=1)[:4000])