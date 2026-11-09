"""Prepare the inputs used by the smallcap / quality research scripts.

``smallcap_strategy_study.py``, ``smallcap_optimized_study.py``,
``smallcap_robustness_study.py``, ``quality_filter_study.py`` and
``pit_quality_baseline.py`` default to ``cache/smallcap_study`` (next to this
file) instead of the old ``D:/tmp/...`` Windows scratch paths.  This script
creates the three files those defaults expect:

``cache/smallcap_study/index_399101.csv``
    399101 (中小综指) daily bars, 2005 -> today, from Tencent's public
    kline endpoint (2000 bars per request, so it pages by year).  Columns:
    date, open, high, low, close, volume.
``cache/smallcap_study/sina_all.jsonl``
    Sina industry classification, one JSON object per line:
    ``{"name": "<industry>", "codes": ["000001", ...]}``.  The 84-sector list
    comes from ``newFLJK.php?param=industry``; constituents are paged from
    ``Market_Center.getHQNodeData`` (80 per page, as akshare does).
``cache/smallcap_study/panels_2018_2024.pkl``
    Price/volume panels rebuilt from ``cache/baostock_history_20261026`` with
    the study module's own ``load_panels`` (2018-07-01 -> 2024-12-31).

Usage (either directory works; paths are anchored at this file):

    python prepare_smallcap_inputs.py
    python prepare_smallcap_inputs.py --force            # refetch/rebuild all
    python prepare_smallcap_inputs.py --skip-panels      # JSON/CSV only
    python prepare_smallcap_inputs.py --skip-index --skip-industry

Existing files are left alone unless ``--force`` is passed.  No akshare /
baostock install is required: every fetch is a plain HTTP GET.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = BASE / "cache" / "smallcap_study"
DEFAULT_DATA_DIR = BASE / "cache" / "baostock_history_20261026"

INDEX_START = "2005-01-01"
INDEX_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    "?param=sz399101,day,{start},{end},2000,qfq"
)
INDUSTRY_LIST_URL = "https://money.finance.sina.com.cn/q/view/newFLJK.php?param=industry"
INDUSTRY_COUNT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeStockCount?node={node}"
)
INDUSTRY_NODE_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=1"
    "&node={node}&symbol=&_s_r_a=page"
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _get(url: str, *, encoding: str = "utf-8", retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode(encoding, errors="replace")
        except Exception as exc:  # noqa: BLE001 - transient network failures
            last = exc
            if attempt + 1 < retries:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"GET failed after {retries} attempts: {url}") from last


def fetch_index(out_dir: Path, start: str, end: str, force: bool) -> Path:
    path = out_dir / "index_399101.csv"
    if path.exists() and not force:
        print(f"[index] keep {path}", flush=True)
        return path

    rows: list[list[str]] = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        url = INDEX_URL.format(
            start=f"{year}-01-01", end=f"{year}-12-31"
        )
        payload = json.loads(_get(url))
        data = payload.get("data")
        node = data.get("sz399101") if isinstance(data, dict) else None
        bars = (node or {}).get("day") or (node or {}).get("qfqday") or []
        rows.extend(bars)
        print(f"[index] {year}: {len(bars)} bars", flush=True)

    if not rows:
        raise RuntimeError("index endpoint returned no bars")

    frame = pd.DataFrame(
        rows,
        columns=["date", "open", "close", "high", "low", "volume"],
    )
    frame["date"] = pd.to_datetime(frame["date"])
    for column in ["open", "close", "high", "low", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[["date", "open", "high", "low", "close", "volume"]]
    frame = frame.dropna(subset=["date", "close"])
    frame = frame.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    frame.to_csv(path, index=False, float_format="%.4f")
    print(
        f"[index] wrote {path} rows={len(frame)} "
        f"{frame['date'].min().date()} -> {frame['date'].max().date()}",
        flush=True,
    )
    return path


def fetch_industries(out_dir: Path, force: bool) -> Path:
    path = out_dir / "sina_all.jsonl"
    if path.exists() and not force:
        print(f"[industry] keep {path}", flush=True)
        return path

    raw = _get(INDUSTRY_LIST_URL, encoding="gbk")
    begin, finish = raw.find("{"), raw.rfind("}")
    if begin < 0 or finish < begin:
        raise RuntimeError("unexpected Sina industry-list payload")
    nodes = json.loads(raw[begin : finish + 1])

    entries: list[dict[str, object]] = []
    for index, (node, value) in enumerate(sorted(nodes.items()), 1):
        name = str(value).split(",")[1]
        total = int(json.loads(_get(INDUSTRY_COUNT_URL.format(node=node))))
        codes: set[str] = set()
        for page in range(1, math.ceil(total / 80) + 1):
            rows = json.loads(
                _get(INDUSTRY_NODE_URL.format(page=page, node=node))
            )
            if not rows:
                break
            for row in rows:
                symbol = str(row.get("code") or row.get("symbol") or "")
                if not symbol:
                    continue
                codes.add(
                    symbol.removeprefix("sh")
                    .removeprefix("sz")
                    .removeprefix("bj")
                    .zfill(6)
                )
            time.sleep(0.1)
        entries.append({"name": name, "codes": sorted(codes)})
        if index % 10 == 0 or index == len(nodes):
            print(
                f"[industry] {index}/{len(nodes)} {name} "
                f"declared={total} fetched={len(codes)}",
                flush=True,
            )

    entries.sort(key=lambda row: str(row["name"]))
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    total_codes = sum(len(entry["codes"]) for entry in entries)
    print(
        f"[industry] wrote {path} industries={len(entries)} codes={total_codes}",
        flush=True,
    )
    return path


def build_panels(out_dir: Path, data_dir: Path, force: bool) -> Path:
    path = out_dir / "panels_2018_2024.pkl"
    if path.exists() and not force:
        print(f"[panels] keep {path}", flush=True)
        return path

    from smallcap_strategy_study import Config, load_panels

    cfg = Config(start="2020-01-02", end="2024-12-31")
    panels = load_panels(data_dir, path, cfg)
    shape = panels["close"].shape
    print(f"[panels] wrote {path} shape={shape[0]}x{shape[1]}", flush=True)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--index-start", default=INDEX_START)
    parser.add_argument("--index-end", default=date.today().isoformat())
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-index", action="store_true")
    parser.add_argument("--skip-industry", action="store_true")
    parser.add_argument("--skip-panels", action="store_true")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_index:
        fetch_index(args.out_dir, args.index_start, args.index_end, args.force)
    if not args.skip_industry:
        fetch_industries(args.out_dir, args.force)
    if not args.skip_panels:
        build_panels(args.out_dir, args.data_dir, args.force)


if __name__ == "__main__":
    main()
