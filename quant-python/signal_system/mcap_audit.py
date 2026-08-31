"""log 市值因子审计 (mcap_audit.py v1.0.0).

候选层为市值过滤前（保留流动性过滤）。因子 = log(market_cap) 按信号日截面分位。
输出原始排名 + 行业中性排名 + 板块分组（主板/创业板/科创板/中小板）。
Q1-Q5 不预设方向。
"""
import argparse, json, hashlib, os, math
from collections import defaultdict
import pandas as pd

AUDIT_VERSION = "1.1.0"
FINANCIAL_KEYWORDS = ("银行", "非银金融")


def sha256_head(path, n=16):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:n]


def load(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    def average_ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        start = 0
        while start < len(order):
            end = start + 1
            value = values[order[start]]
            while end < len(order) and values[order[end]] == value:
                end += 1
            rank = (start + end - 1) / 2.0
            for pos in range(start, end):
                ranks[order[pos]] = rank
            start = end
        return ranks

    dx = average_ranks(xs)
    dy = average_ranks(ys)
    mx, my = sum(dx) / n, sum(dy) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(dx, dy))
    vx = sum((a - mx) ** 2 for a in dx) ** 0.5
    vy = sum((b - my) ** 2 for b in dy) ** 0.5
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def board(symbol):
    s = str(symbol)
    if s.startswith("688"):
        return "科创板"
    if s.startswith(("300", "301")):
        return "创业板"
    if s.startswith(("002", "003")):
        return "中小板"
    return "主板"


def log_mcap(row):
    m = row.get("market_cap")
    if m is None or m <= 0:
        return None
    return math.log(m)


def rank_in_groups(rows, group_fn, factor_fn):
    """组内百分位排名 (0..1, 高=好)。group_fn 返回分组键。"""
    groups = defaultdict(list)
    for r in rows:
        fv = factor_fn(r)
        if fv is None:
            continue
        groups[group_fn(r)].append((r, fv))
    result = {}
    for g, pairs in groups.items():
        if len(pairs) < 2:
            for r, _ in pairs:
                result[id(r)] = 0.5
            continue
        vals = [v for _, v in pairs]
        for r, v in pairs:
            # Average-tie percentile: equal values receive the midpoint of their
            # strictly-below and below-or-equal ranks.
            below = sum(1 for other in vals if other < v)
            equal = sum(1 for other in vals if other == v)
            result[id(r)] = (below + max(equal - 1, 0) / 2.0) / (len(vals) - 1)
    return result


def daily_ic(rows, factor_fn, return_fields, rank_map=None):
    by_day = defaultdict(list)
    for r in rows:
        if factor_fn(r) is None:
            continue
        by_day[r.get("signal_day", "unknown")].append(r)
    results = {}
    for rf in return_fields:
        ics = []
        for day, group in by_day.items():
            valid = [r for r in group if r.get(rf) is not None]
            if len(valid) < 3:
                continue
            if rank_map is not None:
                xs = [rank_map.get(id(r), 0.5) for r in valid]
            else:
                xs = [factor_fn(r) for r in valid]
            ys = [r[rf] for r in valid]
            rho = spearman(xs, ys)
            if rho is not None:
                ics.append(rho)
        if ics:
            results[rf] = {
                "days": len(ics),
                "mean_ic": round(sum(ics) / len(ics), 4),
                "median_ic": round(float(pd.Series(ics).median()), 4),
                "pct_positive": round(sum(1 for v in ics if v > 0) / len(ics) * 100, 1),
            }
    return results


def q1q5(rows, factor_fn, rf, rank_map=None, k=5):
    valid = [(r, factor_fn(r)) for r in rows if factor_fn(r) is not None and r.get(rf) is not None]
    if len(valid) < k:
        return None
    if rank_map is not None:
        order = sorted(valid, key=lambda pr: rank_map.get(id(pr[0]), 0.5))
    else:
        order = sorted(valid, key=lambda pr: pr[1])
    n = len(order)
    out = []
    for q in range(k):
        start = int(n * q / k)
        end = int(n * (q + 1) / k)
        group = [r[rf] for r, _ in order[start:end]]
        out.append({"q": q + 1, "n": len(group),
                    "avg_return": round(sum(group) / len(group), 4),
                    "median_return": round(float(pd.Series(group).median()), 4)})
    return out


def main():
    ap = argparse.ArgumentParser(description="log market-cap factor auditor v1")
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--industry", default=None, help="fund300_industry.json (optional for industry-neutral)")
    ap.add_argument("--output", required=True)
    ap.add_argument("--label", default="unknown")
    args = ap.parse_args()

    rows = load(args.candidate)
    industry_map = None
    if args.industry:
        with open(args.industry, encoding="utf-8") as handle:
            industry_map = {s["symbol"]: s for s in json.load(handle)}
    data_hash = sha256_head(args.candidate)
    return_fields = ["future_5d", "future_20d", "future_40d", "trade_pnl_pct"]

    n_total = len(rows)
    n_mcap = sum(1 for r in rows if log_mcap(r) is not None)

    result = {
        "version": AUDIT_VERSION,
        "label": args.label,
        "data_hash": data_hash,
        "factor": "log(market_cap)",
        "n_total": n_total,
        "n_avail": n_mcap,
        "coverage_ratio": round(n_mcap / n_total, 4) if n_total else None,
        "sample_mcap_range": {"min": min((r.get("market_cap") for r in rows if r.get("market_cap")), default=None),
                              "max": max((r.get("market_cap") for r in rows if r.get("market_cap")), default=None)},
    }

    # 原始排名（log mcap 按日截面）
    raw_rank = rank_in_groups(rows, lambda r: r.get("signal_day"), log_mcap)
    result["rank_ic_raw"] = daily_ic(rows, log_mcap, return_fields, rank_map=raw_rank)
    result["q1q5_raw"] = {}
    result["q5_q1_raw"] = {}
    for rf in return_fields:
        qb = q1q5(rows, log_mcap, rf, rank_map=raw_rank)
        if qb:
            result["q1q5_raw"][rf] = qb
            result["q5_q1_raw"][rf] = round(qb[-1]["avg_return"] - qb[0]["avg_return"], 4)

    # 行业中性排名。行业标签是静态映射时，报告明确记录其非 PIT 性质。
    if industry_map:
        ind_rank = rank_in_groups(
            rows,
            lambda r: (
                r.get("signal_day"),
                (industry_map.get(r["symbol"]) or {}).get("sw1_industry") or "unknown",
            ),
            log_mcap,
        )
        result["rank_ic_industry_neutral"] = daily_ic(rows, log_mcap, return_fields, rank_map=ind_rank)
        result["q1q5_industry"] = {}
        result["q5_q1_industry"] = {}
        for rf in return_fields:
            qb = q1q5(rows, log_mcap, rf, rank_map=ind_rank)
            if qb:
                result["q1q5_industry"][rf] = qb
                result["q5_q1_industry"][rf] = round(qb[-1]["avg_return"] - qb[0]["avg_return"], 4)

        result["industry_neutralization"] = {
            "grouping": "signal_day x sw1_industry",
            "source": "static_sw1_2026",
            "pit_historical_membership": False,
        }

    # 板块中性排名：同一信号日、同一板块内做市值分位。
    board_rank = rank_in_groups(
        rows,
        lambda r: (r.get("signal_day"), board(r.get("symbol", ""))),
        log_mcap,
    )
    result["rank_ic_board_neutral"] = daily_ic(
        rows, log_mcap, return_fields, rank_map=board_rank
    )
    result["q1q5_board"] = {}
    result["q5_q1_board"] = {}
    for rf in return_fields:
        qb = q1q5(rows, log_mcap, rf, rank_map=board_rank)
        if qb:
            result["q1q5_board"][rf] = qb
            result["q5_q1_board"][rf] = round(qb[-1]["avg_return"] - qb[0]["avg_return"], 4)
    result["board_neutralization"] = {
        "grouping": "signal_day x board",
        "source": "symbol_prefix",
        "historical_membership": "derived_from_symbol",
    }

    # 板块分组（主板/创业板/科创板/中小板）
    result["by_board"] = {}
    board_groups = defaultdict(list)
    for r in rows:
        if log_mcap(r) is None:
            continue
        board_groups[board(r["symbol"])].append(r)
    for b, group in board_groups.items():
        f40 = [r["future_40d"] for r in group if r.get("future_40d") is not None]
        trade_pnl = [r["trade_pnl_pct"] for r in group if r.get("trade_pnl_pct") is not None]
        result["by_board"][b] = {
            "n": len(group),
            "avg_f40": round(sum(f40) / len(f40), 3) if f40 else None,
            "avg_trade_pnl": round(sum(trade_pnl) / len(trade_pnl), 3) if trade_pnl else None,
        }

    # 换手率（候选集合跨日变化近似）
    days_sorted = sorted(set(r["signal_day"] for r in rows if log_mcap(r) is not None))
    turnovers = []
    prev = None
    for day in days_sorted:
        day_syms = {r["symbol"] for r in rows if r.get("signal_day") == day and log_mcap(r) is not None}
        if prev is not None:
            inter = len(day_syms & prev)
            turnovers.append(1 - inter / max(len(prev), 1))
        prev = day_syms
    result["candidate_turnover"] = {
        "days": len(days_sorted),
        "avg_daily_new_ratio": round(sum(turnovers) / len(turnovers), 3) if turnovers else None,
    }

    # regime / signal_type 分组收益
    result["by_regime"] = {}
    result["by_signal_type"] = {}
    for rf in ["future_40d", "trade_pnl_pct"]:
        rg = defaultdict(list)
        st = defaultdict(list)
        for r in rows:
            if log_mcap(r) is None or r.get(rf) is None:
                continue
            rg[r.get("regime", "unknown")].append(r[rf])
            st[r.get("signal_type", "unknown")].append(r[rf])
        result["by_regime"][rf] = {k: {"n": len(v), "avg": round(sum(v)/len(v), 3)} for k, v in rg.items() if len(v) >= 3}
        result["by_signal_type"][rf] = {k: {"n": len(v), "avg": round(sum(v)/len(v), 3)} for k, v in st.items() if len(v) >= 3}

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"saved {args.output}", flush=True)
    print(f"log_mcap coverage={result['coverage_ratio']} range=[{result['sample_mcap_range']['min']:.0f},{result['sample_mcap_range']['max']:.0f}]亿", flush=True)
    for rf, ic in (result.get("rank_ic_raw") or {}).items():
        print(f"  RAW IC({rf}): med={ic['median_ic']} pos={ic['pct_positive']}%", flush=True)
    if industry_map and result.get("rank_ic_industry_neutral"):
        for rf, ic in result["rank_ic_industry_neutral"].items():
            print(f"  IND IC({rf}): med={ic['median_ic']} pos={ic['pct_positive']}%", flush=True)
    for rf, d in (result.get("q5_q1_raw") or {}).items():
        print(f"  RAW Q5-Q1({rf}): {d}pp", flush=True)


if __name__ == "__main__":
    main()
