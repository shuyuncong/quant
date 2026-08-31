"""行业中性负债率因子审计 (debt_ratio_audit.py v1.0.0).

金融行业（银行/非银金融）单列，非金融股票按行业内分位排名。
复用 factor_audit 的统计逻辑，但标准化改为：
  - 金融股票：单独一组（行业内分位）
  - 非金融股票：按申万一级行业内分位
"""
import argparse, json, hashlib, os, sys
from collections import defaultdict
import pandas as pd

AUDIT_VERSION = "1.0.0"
FINANCIAL_KEYWORDS = ("银行", "非银金融")


def sha256_head(path, n=16):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:n]


def load(path):
    return [json.loads(l) for l in open(path, encoding="utf-8")]


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    rx = {v: i for i, v in enumerate(sorted(xs))}
    ry = {v: i for i, v in enumerate(sorted(ys))}
    dx = [rx[v] for v in xs]
    dy = [ry[v] for v in ys]
    mx, my = sum(dx) / n, sum(dy) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(dx, dy))
    vx = sum((a - mx) ** 2 for a in dx) ** 0.5
    vy = sum((b - my) ** 2 for b in dy) ** 0.5
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def is_financial(sw1):
    return any(kw in str(sw1) for kw in FINANCIAL_KEYWORDS)


def industry_rank(rows, industry_map):
    """Compute debt_ratio within-group percentile ranks.
    Group = symbol's SW1 industry (financial stocks grouped by industry too,
    so banks rank within 银行, non-bank within 非银金融)."""
    groups = defaultdict(list)
    for r in rows:
        if r.get("debt_ratio") is None:
            continue
        ind = (industry_map.get(r["symbol"]) or {}).get("sw1_industry") or "unknown"
        groups[ind].append((r, float(r["debt_ratio"])))
    result = {}
    for ind, pairs in groups.items():
        if len(pairs) < 2:
            for r, _ in pairs:
                result[id(r)] = 0.5
            continue
        values = [v for _, v in pairs]
        for r, v in pairs:
            below = sum(1 for other in values if other < v)
            result[id(r)] = below / (len(values) - 1)
    return result


def rank_ic_by_day(rows, industry_map, return_fields):
    by_day = defaultdict(list)
    for r in rows:
        if r.get("debt_ratio") is None:
            continue
        by_day[r.get("signal_day", "unknown")].append(r)
    ranks = industry_rank(rows, industry_map)
    results = {}
    for rf in return_fields:
        ics = []
        for day, group in by_day.items():
            valid = [r for r in group if r.get(rf) is not None]
            if len(valid) < 3:
                continue
            xs = [ranks.get(id(r), 0.5) for r in valid]
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


def q1q5(rows, industry_map, rf, k=5):
    valid = [(r.get("debt_ratio"), r.get(rf)) for r in rows
             if r.get("debt_ratio") is not None and r.get(rf) is not None]
    if len(valid) < k:
        return None
    ranks = industry_rank(rows, industry_map)
    # 按行业内分位排序分桶
    order = sorted(range(len(valid)), key=lambda i: ranks.get(id(rows[i]), 0.5))
    n = len(order)
    out = []
    for q in range(k):
        start = int(n * q / k)
        end = int(n * (q + 1) / k)
        group = [valid[i][1] for i in order[start:end]]
        out.append({"q": q + 1, "n": len(group),
                    "avg_return": round(sum(group) / len(group), 4),
                    "median_return": round(float(pd.Series(group).median()), 4)})
    return out


def main():
    ap = argparse.ArgumentParser(description="Industry-neutral debt ratio auditor v1")
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--industry", required=True, help="fund300_industry.json")
    ap.add_argument("--output", default="/tmp/audit/debt_report.json")
    ap.add_argument("--label", default="unknown")
    args = ap.parse_args()

    rows = load(args.candidate)
    industry_map = {s["symbol"]: s for s in json.load(open(args.industry))}
    data_hash = sha256_head(args.candidate)
    return_fields = ["future_5d", "future_20d", "future_40d", "trade_pnl_pct"]

    n_total = len(rows)
    n_debt = sum(1 for r in rows if r.get("debt_ratio") is not None)
    n_fin = sum(1 for r in rows if is_financial((industry_map.get(r["symbol"]) or {}).get("sw1_industry")))

    result = {
        "version": AUDIT_VERSION,
        "label": args.label,
        "data_hash": data_hash,
        "factor": "debt_ratio",
        "normalization": "industry-neutral (SW1 within-group percentile)",
        "n_total": n_total,
        "n_avail": n_debt,
        "coverage_ratio": round(n_debt / n_total, 4) if n_total else None,
        "n_financial": n_fin,
        "n_non_financial": n_total - n_fin,
        "rank_ic": rank_ic_by_day(rows, industry_map, return_fields),
    }
    result["quantile_buckets"] = {}
    result["q5_minus_q1"] = {}
    for rf in return_fields:
        qb = q1q5(rows, industry_map, rf)
        if qb:
            result["quantile_buckets"][rf] = qb
            result["q5_minus_q1"][rf] = round(qb[-1]["avg_return"] - qb[0]["avg_return"], 4)

    # 金融 vs 非金融分离
    fin_rows = [r for r in rows if is_financial((industry_map.get(r["symbol"]) or {}).get("sw1_industry"))]
    nonfin_rows = [r for r in rows if not is_financial((industry_map.get(r["symbol"]) or {}).get("sw1_industry"))]
    result["financial_split"] = {
        "financial": {"n": len(fin_rows),
                      "debt_median": round(float(pd.Series([r["debt_ratio"] for r in fin_rows if r.get("debt_ratio") is not None]).median()), 2),
                      "trade_pnl_median": round(float(pd.Series([r["trade_pnl_pct"] for r in fin_rows if r.get("trade_pnl_pct") is not None]).median()), 3)},
        "non_financial": {"n": len(nonfin_rows),
                          "debt_median": round(float(pd.Series([r["debt_ratio"] for r in nonfin_rows if r.get("debt_ratio") is not None]).median()), 2),
                          "trade_pnl_median": round(float(pd.Series([r["trade_pnl_pct"] for r in nonfin_rows if r.get("trade_pnl_pct") is not None]).median()), 3)},
    }

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"saved {args.output}", flush=True)
    print(f"debt_ratio coverage={result['coverage_ratio']} fin={n_fin} nonfin={n_total-n_fin}", flush=True)
    for rf, ic in (result.get("rank_ic") or {}).items():
        print(f"  IC({rf}): med={ic['median_ic']} pos={ic['pct_positive']}%")
    for rf, qb in (result.get("quantile_buckets") or {}).items():
        if qb:
            print(f"  {rf}: Q1={qb[0]['avg_return']} Q5={qb[-1]['avg_return']} diff={result['q5_minus_q1'].get(rf,'?')}")


if __name__ == "__main__":
    main()