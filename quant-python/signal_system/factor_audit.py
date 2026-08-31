"""可复用的候选层因子因子审计器 (factor_audit.py v1).

用法：
  python factor_audit.py \
    --candidate /tmp/candidates/candidates_val.jsonl \
    --factor roe \
    --period-normalize \
    --output /tmp/audit/roe_report.json

记录：
  - 数据文件 SHA256（前 16 字符）
  - 候选数量、窗口标记
  - 因子公式 / 标准化方法 / 代码版本
  - 覆盖率、分布、Rank IC、Q1-Q5、分组统计
  - 精确 vs 估算公告日分离
"""
import argparse, json, hashlib, sys, os
from collections import defaultdict
from pathlib import Path
import pandas as pd

AUDIT_VERSION = "1.0.0"


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


def percentile(vals, p):
    s = sorted(vals)
    if not s:
        return None
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1)))))
    return round(float(s[k]), 6)


def factor_available(rows, field):
    """Return list of (row, value) for rows with non-None factor value."""
    pairs = []
    for r in rows:
        v = r.get(field)
        if v is not None and v == v:  # not None and not NaN
            pairs.append((r, float(v)))
    return pairs


def period_bucket(report_date: str | None) -> str:
    """Classify report period into Q1 / H1 / Q3 / Annual / unknown.

    Accepts 'YYYYMMDD' (PIT canonical) or 'YYYY-MM-DD' (ISO).
    """
    if not report_date:
        return "unknown"
    s = str(report_date).strip().replace("-", "").replace("/", "")
    if len(s) < 8 or not s[:8].isdigit():
        return "unknown"
    m = int(s[4:6])
    if m == 3:
        return "Q1"
    if m == 6:
        return "H1"
    if m == 9:
        return "Q3"
    if m == 12:
        return "Annual"
    return "unknown"


def rank_transform(values, *, period_labels=None):
    """Compute within-group percentile ranks (0..1).
    When period_labels is provided, rank is within each period group.
    Otherwise rank across ALL values.
    Higher is better → fraction strictly below.
    """
    n = len(values)
    if not values:
        return []
    # bucket by period
    if period_labels:
        buckets = defaultdict(list)
        for val, lbl in zip(values, period_labels):
            buckets[lbl].append(val)
        result = []
        for val, lbl in zip(values, period_labels):
            bucket = buckets[lbl]
            below = sum(1 for other in bucket if other < val)
            result.append(below / (len(bucket) - 1) if len(bucket) > 1 else 0.5)
        return result
    # single group
    return [sum(1 for other in values if other < val) / (n - 1) if n > 1 else 0.5 for val in values]


def rank_ic_by_day(rows, field, period_normalize, return_fields):
    """Daily Spearman(factor value, return). Day-equal-weighted."""
    # group by signal_day
    by_day = defaultdict(list)
    for r in rows:
        fv = r.get(field)
        if fv is None or fv != fv:
            continue
        by_day[r.get("signal_day", "unknown")].append(r)
    results = {}
    for ret_field in return_fields:
        ics = []
        for day, group in by_day.items():
            valid = [r for r in group if r.get(ret_field) is not None]
            if len(valid) < 3:
                continue
            if period_normalize:
                periods = [period_bucket(r.get("period")) for r in valid]
                xs = rank_transform([r[field] for r in valid], period_labels=periods)
            else:
                xs = [r[field] for r in valid]
            ys = [r[ret_field] for r in valid]
            rho = spearman(xs, ys)
            if rho is not None:
                ics.append(rho)
        if ics:
            results[ret_field] = {
                "days": len(ics),
                "mean_ic": round(sum(ics) / len(ics), 4),
                "median_ic": round(float(pd.Series(ics).median()), 4),
                "pct_positive": round(sum(1 for v in ics if v > 0) / len(ics) * 100, 1),
            }
    return results


def q1q5_buckets(rows, field, period_normalize, ret_field, k=5):
    """Q1-Q5 buckets by factor value, returns avg return per bucket."""
    valid = [(r.get(field), r.get(ret_field)) for r in rows
             if r.get(field) is not None and r.get(ret_field) is not None]
    if len(valid) < k:
        return None
    vals, rets = zip(*valid)
    if period_normalize:
        periods = [period_bucket(r.get("period")) for r in rows if r.get(field) is not None and r.get(ret_field) is not None]
        sorted_vals = rank_transform(list(vals), period_labels=list(periods))
    else:
        sorted_vals = list(vals)
    # Sort by the (possibly period-normalized) factor rank for bucketing
    indices = sorted(range(len(sorted_vals)), key=lambda i: sorted_vals[i])
    n = len(indices)
    out = []
    for q in range(k):
        start = int(n * q / k)
        end = int(n * (q + 1) / k)
        group = [rets[i] for i in indices[start:end]]
        out.append({
            "q": q + 1,
            "n": len(group),
            "avg_return": round(sum(group) / len(group), 4),
            "median_return": round(float(pd.Series(group).median()), 4),
        })
    return out


def audit(rows, field, period_normalize, label, data_hash, return_fields):
    factor_pairs = factor_available(rows, field)
    n_total = len(rows)
    n_avail = len(factor_pairs)
    
    # determine period labels for each row
    period_labels = [period_bucket(r.get("period")) for r in rows] if period_normalize else None
    
    result = {
        "version": AUDIT_VERSION,
        "label": label,
        "data_hash": data_hash,
        "factor": field,
        "period_normalize": period_normalize,
        "n_total": n_total,
        "n_avail": n_avail,
        "coverage_ratio": round(n_avail / n_total, 4) if n_total else None,
    }
    
    # Distribution (raw values)
    raw_vals = [v for _, v in factor_pairs]
    if raw_vals:
        result["distribution"] = {
            "n": len(raw_vals),
            "p1": percentile(raw_vals, 1),
            "p10": percentile(raw_vals, 10),
            "p50": percentile(raw_vals, 50),
            "p90": percentile(raw_vals, 90),
            "p99": percentile(raw_vals, 99),
            "min": round(min(raw_vals), 4),
            "max": round(max(raw_vals), 4),
        }
    
    # Ann date quality
    exact = sum(1 for r in rows if not r.get("ann_date_estimated"))
    estimated = sum(1 for r in rows if r.get("ann_date_estimated"))
    result["ann_date_quality"] = {
        "exact": exact, "estimated": estimated, "estimated_ratio": round(estimated / n_total, 3) if n_total else None,
    }
    
    # Rank IC
    result["rank_ic"] = rank_ic_by_day(rows, field, period_normalize, return_fields)
    
    # Q1-Q5 for each return field
    result["quantile_buckets"] = {}
    for rf in return_fields:
        qb = q1q5_buckets(rows, field, period_normalize, rf)
        if qb:
            result["quantile_buckets"][rf] = qb
    
    # Q5 - Q1
    result["q5_minus_q1"] = {}
    for rf, qb in result["quantile_buckets"].items():
        if qb and len(qb) >= 2:
            q1 = qb[0]["avg_return"]
            q5 = qb[-1]["avg_return"]
            result["q5_minus_q1"][rf] = round(q5 - q1, 4)
    
    # By regime
    result["by_regime"] = {}
    for rf in return_fields:
        grp = defaultdict(list)
        for r in rows:
            if r.get(field) is None or r.get(rf) is None:
                continue
            grp[r.get("regime", "unknown")].append(r[rf])
        result["by_regime"][rf] = {
            reg: {"n": len(v), "avg_return": round(sum(v) / len(v), 4)}
            for reg, v in grp.items() if len(v) >= 3
        }
    
    return result


def main():
    ap = argparse.ArgumentParser(description="Candidate-layer factor auditor v1")
    ap.add_argument("--candidate", required=True, help="candidate jsonl path")
    ap.add_argument("--factor", default="roe", help="factor field name")
    ap.add_argument("--period-normalize", action="store_true", help="rank within report-period groups")
    ap.add_argument("--output", default="/tmp/audit/factor_report.json")
    ap.add_argument("--label", default="unknown")
    args = ap.parse_args()
    
    rows = load(args.candidate)
    data_hash = sha256_head(args.candidate)
    
    return_fields = ["future_5d", "future_20d", "future_40d", "trade_pnl_pct"]
    
    result = audit(rows, args.factor, args.period_normalize, args.label, data_hash, return_fields)
    
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"saved {args.output}", flush=True)
    print(f"factor={args.factor} period_normalize={args.period_normalize} coverage={result['coverage_ratio']}", flush=True)
    for rf, ic in (result.get("rank_ic") or {}).items():
        print(f"  IC({rf}): med={ic['median_ic']} pos={ic['pct_positive']}%")
    for rf, qb in (result.get("quantile_buckets") or {}).items():
        if qb:
            print(f"  {rf}: Q1={qb[0]['avg_return']} Q5={qb[-1]['avg_return']} diff={result['q5_minus_q1'].get(rf,'?')}")
    return result


if __name__ == "__main__":
    main()