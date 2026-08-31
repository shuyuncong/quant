"""Point-in-time fundamental history construction utilities.

The selector consumes one snapshot per historical ``as_of`` date.  In real
data, however, the inputs usually arrive in two different streams:

* financial filings (ROE/debt ratio) become available on an announcement date;
* valuation fields (PE/circulating market cap) are market-day observations.

This module joins those streams without looking ahead and writes the compact
JSON/JSONL contract already understood by :mod:`core.selector.fundamental`.
It deliberately has no network or database dependency; callers provide local
records or records returned by an adapter they control.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


_SYMBOL_KEYS = ("symbol", "ts_code", "code", "ticker", "stock_code", "证券代码")
_PERIOD_KEYS = ("period", "report_period", "end_date", "报告期", "报告期末")
_ANNOUNCEMENT_KEYS = (
    "financial_ann_date",
    "fundamental_ann_date",
    "ann_date",
    "announcement_date",
    "published_at",
    "publish_date",
    "公告日期",
    "公告日",
    # Generic availability is accepted only when no explicit announcement
    # field exists; explicit filing dates above must win when both are present.
    "fundamental_data_as_of",
    "available_as_of",
)
_TRADE_DATE_KEYS = (
    "trade_date",
    "market_as_of",
    "signal_day",
    "date",
    "交易日期",
)
_SOURCE_KEYS = ("source", "data_source", "provider", "来源")
_DATE_QUALITY_KEYS = (
    "ann_date_quality",
    "announcement_date_quality",
    "date_quality",
)
_DATE_ESTIMATED_KEYS = (
    "ann_date_estimated",
    "announcement_date_estimated",
    "date_estimated",
)

_ALIASES = {
    "roe": ("roe", "roe_avg", "net_roe", "净资产收益率", "净资产收益率(摊薄)"),
    "debt_ratio": (
        "debt_ratio",
        "debt_to_assets",
        "debt",
        "asset_liability_ratio",
        "资产负债率",
    ),
    "pe": ("pe", "pe_ttm", "pe_dynamic", "市盈率", "市盈率(TTM)"),
    "market_cap": (
        "market_cap",
        "circ_mv",
        "free_float_mv",
        "total_mv",
        "流通市值",
        "流通市值(亿)",
    ),
}


def _coerce_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _normalise_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    code = text.split(".", 1)[0]
    if code.isdigit() and len(code) <= 6:
        return code.zfill(6)
    return text


def _first(record: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "N/A", "NA", "None", "null", "\u2014"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        result = float(text)
    except (TypeError, ValueError):
        return None
    if result != result:
        return None
    return -result if negative else result


def _market_cap_unit(record: Mapping[str, Any], selected_field: str | None) -> str:
    value = _first(record, ("market_cap_unit", "circ_mv_unit", "市值单位"))
    if value not in (None, ""):
        return str(value).strip().lower()
    # Tushare/AKShare daily-basic ``circ_mv``/``total_mv`` values are in
    # 10k CNY.  A generic ``market_cap`` is kept in the selector's canonical
    # 100m CNY unit unless the caller supplies an explicit unit.
    if selected_field in {"circ_mv", "total_mv", "free_float_mv"}:
        return "10k_cny"
    return "100m_cny"


def _normalise_market_cap(
    value: Any,
    record: Mapping[str, Any],
    selected_field: str | None,
) -> float | None:
    amount = _as_float(value)
    if amount is None:
        return None
    unit = _market_cap_unit(record, selected_field).replace(" ", "")
    if unit in {"cny", "yuan", "rmb", "元", "人民币"}:
        return amount / 100_000_000
    if unit in {"10k_cny", "10k", "万元", "万", "wan"}:
        return amount / 10_000
    if unit in {"1k_cny", "千元", "thousand"}:
        return amount / 100_000
    # The selector/backtest contract uses 100m CNY (亿元) as its canonical unit.
    return amount


def _canonical_value(record: Mapping[str, Any], field: str) -> Any:
    return _first(record, _ALIASES[field])


def _canonical_item(record: Mapping[str, Any], field: str) -> tuple[str | None, Any]:
    for key in _ALIASES[field]:
        if key in record and record[key] not in (None, ""):
            return key, record[key]
    return None, None


def normalize_history_record(
    record: Mapping[str, Any],
    *,
    symbol: Any = None,
    kind: str = "joined",
    source: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Normalize one raw financial/market record.

    ``kind`` may be ``financial``, ``market`` or ``joined``.  The second tuple
    item is a machine-readable issue (rather than an exception) so a builder
    can report bad rows while retaining valid symbols.
    """

    if not isinstance(record, Mapping):
        return None, {"code": "record_not_mapping", "message": "record must be an object"}

    normalized_symbol = _normalise_symbol(symbol or _first(record, _SYMBOL_KEYS))
    if not normalized_symbol:
        return None, {"code": "symbol_missing", "message": "symbol/ts_code is required"}

    kind = str(kind or "joined").lower().strip()
    if kind not in {"financial", "market", "joined"}:
        raise ValueError("history record kind must be financial, market or joined")

    result: dict[str, Any] = {"symbol": normalized_symbol}
    period = _first(record, _PERIOD_KEYS)
    if period is not None:
        result["period"] = str(period).replace("-", "").replace("/", "")

    ann_date = _coerce_date(_first(record, _ANNOUNCEMENT_KEYS))
    trade_date = _coerce_date(_first(record, _TRADE_DATE_KEYS))
    if ann_date is not None:
        result["ann_date"] = ann_date.isoformat()
        quality = _first(record, _DATE_QUALITY_KEYS)
        estimated = _first(record, _DATE_ESTIMATED_KEYS)
        if quality not in (None, ""):
            result["ann_date_quality"] = str(quality).strip().lower()
        if estimated not in (None, ""):
            if isinstance(estimated, str):
                estimated = estimated.strip().lower() in {"1", "true", "yes", "y", "estimated"}
            result["ann_date_estimated"] = bool(estimated)
            if quality in (None, "") and result["ann_date_estimated"]:
                result["ann_date_quality"] = "estimated"
        elif quality not in (None, ""):
            result["ann_date_estimated"] = str(quality).strip().lower() == "estimated"
    if trade_date is not None:
        result["trade_date"] = trade_date.isoformat()

    source_value = source or _first(record, _SOURCE_KEYS)
    if source_value:
        result["source"] = str(source_value)

    if kind in {"financial", "joined"}:
        for field in ("roe", "debt_ratio"):
            value = _as_float(_canonical_value(record, field))
            if value is not None:
                result[field] = value

    if kind in {"market", "joined"}:
        value = _as_float(_canonical_value(record, "pe"))
        if value is not None:
            result["pe"] = value
        market_cap_field, market_cap_value = _canonical_item(record, "market_cap")
        value = _normalise_market_cap(market_cap_value, record, market_cap_field)
        if value is not None:
            result["market_cap"] = value

        # Preserve useful diagnostics without letting arbitrary input fields
        # enter the selector contract.
        for field in ("amount", "turnover_rate", "volume_ratio"):
            value = _as_float(record.get(field))
            if value is not None:
                result[field] = value

    if kind == "financial" and ann_date is None:
        return result, {
            "symbol": normalized_symbol,
            "code": "announcement_date_missing",
            "message": "financial record has no announcement/availability date",
        }
    if kind == "market" and trade_date is None:
        return result, {
            "symbol": normalized_symbol,
            "code": "trade_date_missing",
            "message": "market record has no trade date",
        }
    return result, None


def _record_sort_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("ann_date") or record.get("trade_date") or ""),
        str(record.get("period") or ""),
        str(record.get("source") or ""),
    )


def _dedupe_records(
    records: Iterable[dict[str, Any]],
    date_key: str,
    *,
    include_period: bool = True,
) -> list[dict[str, Any]]:
    """Keep the last deterministic record for each symbol/date/period key."""

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in sorted(records, key=_record_sort_key):
        key = (
            str(record["symbol"]),
            str(record.get(date_key) or ""),
            str(record.get("period") or "") if include_period else "",
        )
        grouped[key] = dict(record)
    return list(grouped.values())


def build_point_in_time_history(
    financial_records: Iterable[Mapping[str, Any]],
    market_records: Iterable[Mapping[str, Any]],
    *,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    include_uncovered_market_days: bool = True,
) -> "HistoryBuildResult":
    """Join filing records to market-day observations without look-ahead.

    For every market row, the selected filing is the latest filing whose
    announcement date is not later than that market day.  The resulting row's
    ``available_as_of`` is the market day, while ``ann_date`` remains the
    financial filing date for auditability.  Consequently the existing
    historical selector can choose a row by signal day and still verify both
    timestamps.
    """

    start = _coerce_date(start_date)
    end = _coerce_date(end_date)
    if start_date is not None and start is None:
        raise ValueError(f"invalid start_date: {start_date!r}")
    if end_date is not None and end is None:
        raise ValueError(f"invalid end_date: {end_date!r}")
    if start and end and start > end:
        raise ValueError("start_date must not be later than end_date")
    issues: list[dict[str, Any]] = []
    financial: dict[str, list[dict[str, Any]]] = {}
    market: dict[str, list[dict[str, Any]]] = {}

    for index, raw in enumerate(financial_records):
        normalized, issue = normalize_history_record(raw, kind="financial")
        if issue:
            issue = {"source": "financial", "row": index, **issue}
            issues.append(issue)
        if normalized and normalized.get("ann_date"):
            financial.setdefault(normalized["symbol"], []).append(normalized)

    for index, raw in enumerate(market_records):
        normalized, issue = normalize_history_record(raw, kind="market")
        if issue:
            issue = {"source": "market", "row": index, **issue}
            issues.append(issue)
        trade_day = normalized.get("trade_date") if normalized else None
        if not normalized or not trade_day:
            continue
        trade = _coerce_date(trade_day)
        if (start and trade and trade < start) or (end and trade and trade > end):
            continue
        market.setdefault(normalized["symbol"], []).append(normalized)

    for values in financial.values():
        counts = Counter((row.get("ann_date"), row.get("period")) for row in values)
        for (announced, period), count in counts.items():
            if count > 1:
                issues.append(
                    {
                        "source": "financial",
                        "code": "duplicate_filing_key",
                        "symbol": values[0]["symbol"],
                        "ann_date": announced,
                        "period": period,
                        "count": count,
                        "message": "multiple filings share the same symbol/announcement/period key; deterministic last row retained",
                    }
                )
        values[:] = _dedupe_records(values, "ann_date")
        values.sort(key=lambda item: (item.get("ann_date", ""), item.get("period", "")))
    for values in market.values():
        counts = Counter(row.get("trade_date") for row in values)
        for trade_day, count in counts.items():
            if count > 1:
                issues.append(
                    {
                        "source": "market",
                        "code": "duplicate_market_date",
                        "symbol": values[0]["symbol"],
                        "trade_date": trade_day,
                        "count": count,
                        "message": "multiple market rows share the same symbol/trade date; deterministic last row retained",
                    }
                )
        values[:] = _dedupe_records(values, "trade_date", include_period=False)
        values.sort(key=lambda item: item.get("trade_date", ""))

    output: dict[str, list[dict[str, Any]]] = {}
    for symbol, market_rows in market.items():
        filings = financial.get(symbol, [])
        for market_row in market_rows:
            trade_day = _coerce_date(market_row["trade_date"])
            filing = None
            for candidate in filings:
                announced = _coerce_date(candidate.get("ann_date"))
                if announced and trade_day and announced <= trade_day:
                    filing = candidate
                elif announced and trade_day and announced > trade_day:
                    break

            if filing is None and not include_uncovered_market_days:
                issues.append(
                    {
                        "source": "join",
                        "symbol": symbol,
                        "code": "no_filing_available",
                        "trade_date": market_row["trade_date"],
                        "message": "no filing announced on or before market day",
                    }
                )
                continue

            joined: dict[str, Any] = {"symbol": symbol}
            if filing:
                joined.update({key: value for key, value in filing.items() if key != "symbol"})
                joined["financial_ann_date"] = filing.get("ann_date")
                joined["financial_source"] = filing.get("source")
                joined["financial_ann_date_quality"] = filing.get("ann_date_quality")
                joined["financial_ann_date_estimated"] = bool(
                    filing.get("ann_date_estimated", False)
                )
            else:
                joined["financial_ann_date"] = None
                joined["financial_source"] = None
                joined["financial_ann_date_quality"] = None
                joined["financial_ann_date_estimated"] = False
            for key, value in market_row.items():
                if key != "symbol":
                    joined[key] = value
            joined["market_as_of"] = market_row["trade_date"]
            joined["available_as_of"] = market_row["trade_date"]
            if filing and filing.get("source") and market_row.get("source"):
                joined["source"] = f"{filing['source']}+{market_row['source']}"
            elif market_row.get("source"):
                joined["source"] = market_row["source"]
            elif filing and filing.get("source"):
                joined["source"] = filing["source"]
            output.setdefault(symbol, []).append(joined)

    for values in output.values():
        values.sort(key=lambda item: (item.get("available_as_of", ""), item.get("period", "")))
    return HistoryBuildResult(history=output, issues=tuple(issues), report=history_coverage_report(output))


def history_coverage_report(
    history: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    signal_days: Mapping[str, Iterable[date | str]] | None = None,
) -> dict[str, Any]:
    """Return deterministic coverage/completeness metrics for a history map."""

    required = ("roe", "debt_ratio", "pe", "market_cap")
    records_total = 0
    complete = 0
    estimated_dates = 0
    financial_snapshot_keys: set[tuple[str, str, str]] = set()
    estimated_financial_snapshot_keys: set[tuple[str, str, str]] = set()
    missing: dict[str, int] = {field: 0 for field in required}
    available_dates: list[str] = []
    symbols: dict[str, dict[str, Any]] = {}

    for symbol, rows in sorted(history.items()):
        row_list = [row for row in rows if isinstance(row, Mapping)]
        symbol_missing = {field: 0 for field in required}
        symbol_complete = 0
        symbol_dates: list[str] = []
        for row in row_list:
            records_total += 1
            available = row.get("available_as_of") or row.get("trade_date") or row.get("ann_date")
            if available:
                available = str(available)[:10]
                available_dates.append(available)
                symbol_dates.append(available)
            if row.get("ann_date_estimated") is True or str(row.get("ann_date_quality", "")).lower() == "estimated":
                estimated_dates += 1
            financial_ann_date = row.get("financial_ann_date") or row.get("ann_date")
            if financial_ann_date:
                snapshot_key = (
                    str(symbol),
                    str(row.get("period") or ""),
                    str(financial_ann_date),
                )
                financial_snapshot_keys.add(snapshot_key)
                if row.get("financial_ann_date_estimated") is True or row.get("ann_date_estimated") is True or str(row.get("financial_ann_date_quality") or row.get("ann_date_quality") or "").lower() == "estimated":
                    estimated_financial_snapshot_keys.add(snapshot_key)
            row_complete = True
            for field in required:
                value = row.get(field)
                if value is None or value == "":
                    missing[field] += 1
                    symbol_missing[field] += 1
                    row_complete = False
            if row_complete:
                complete += 1
                symbol_complete += 1
        symbols[symbol] = {
            "records": len(row_list),
            "complete_records": symbol_complete,
            "missing_fields": symbol_missing,
            "first_available_as_of": min(symbol_dates) if symbol_dates else None,
            "last_available_as_of": max(symbol_dates) if symbol_dates else None,
        }

    report: dict[str, Any] = {
        "symbols": len(symbols),
        "records": records_total,
        "complete_records": complete,
        "complete_ratio": (complete / records_total) if records_total else 0.0,
        "estimated_financial_dates": estimated_dates,
        "estimated_financial_date_ratio": estimated_dates / records_total if records_total else 0.0,
        "financial_snapshots": len(financial_snapshot_keys),
        "estimated_financial_snapshots": len(estimated_financial_snapshot_keys),
        "estimated_financial_snapshot_ratio": (
            len(estimated_financial_snapshot_keys) / len(financial_snapshot_keys)
            if financial_snapshot_keys
            else 0.0
        ),
        "missing_fields": missing,
        "first_available_as_of": min(available_dates) if available_dates else None,
        "last_available_as_of": max(available_dates) if available_dates else None,
        "per_symbol": symbols,
    }

    if signal_days is not None:
        requested = 0
        covered = 0
        causal = 0
        future_financial = 0
        complete_signal_days = 0
        from .fundamental import select_historical_snapshot

        for symbol, days in signal_days.items():
            rows = list(history.get(_normalise_symbol(symbol), ()))
            for day in days:
                requested += 1
                selected = select_historical_snapshot(rows, day)
                if selected is None:
                    continue
                covered += 1
                signal_day = _coerce_date(day)
                financial_day = _coerce_date(
                    selected.get("financial_ann_date")
                    or selected.get("fundamental_ann_date")
                    or selected.get("ann_date")
                )
                if signal_day and financial_day and financial_day > signal_day:
                    future_financial += 1
                    continue
                causal += 1
                if all(selected.get(field) not in (None, "") for field in required):
                    complete_signal_days += 1
        report["signal_days_requested"] = requested
        report["signal_days_covered"] = covered
        report["signal_day_coverage_ratio"] = covered / requested if requested else 0.0
        report["signal_days_causal"] = causal
        report["signal_days_future_financial"] = future_financial
        report["signal_day_causal_ratio"] = causal / requested if requested else 0.0
        report["signal_days_complete"] = complete_signal_days
        report["signal_day_complete_ratio"] = (
            complete_signal_days / requested if requested else 0.0
        )
    return report


def load_history_records(path: str | Path) -> list[dict[str, Any]]:
    """Load raw records from JSON, JSONL/NDJSON or CSV without external I/O."""

    source = Path(path).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"history input does not exist: {source}")
    suffix = source.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        records: list[dict[str, Any]] = []
        with source.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
                if isinstance(value, Mapping):
                    records.append(dict(value))
        return records
    if suffix == ".csv":
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    with source.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        raise ValueError("history JSON must contain an object or array")
    records = payload.get("records")
    if isinstance(records, list):
        return [dict(item) for item in records if isinstance(item, Mapping)]
    # Accept the mapping contract used by load_fundamental_history:
    # {"000001.SZ": [{...}, {...}]}.
    flattened: list[dict[str, Any]] = []
    for symbol, values in payload.items():
        if symbol in {"metadata", "report"}:
            continue
        if isinstance(values, Mapping):
            values = [values]
        if isinstance(values, list):
            for value in values:
                if isinstance(value, Mapping):
                    item = dict(value)
                    item.setdefault("symbol", symbol)
                    flattened.append(item)
    return flattened


def write_fundamental_history(
    history: Mapping[str, Iterable[Mapping[str, Any]]],
    path: str | Path,
    *,
    format: str | None = None,
    indent: int = 2,
) -> Path:
    """Write a history map atomically as JSON mapping or JSONL records."""

    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    selected_format = (format or destination.suffix.lstrip(".") or "json").lower()
    if selected_format in {"ndjson"}:
        selected_format = "jsonl"
    if selected_format not in {"json", "jsonl"}:
        raise ValueError("history output format must be json or jsonl")

    temp_path = destination.with_name(destination.name + ".tmp")
    try:
        if selected_format == "jsonl":
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                for symbol in sorted(history):
                    rows = sorted(
                        (dict(row) for row in history[symbol] if isinstance(row, Mapping)),
                        key=lambda row: (str(row.get("available_as_of") or row.get("trade_date") or ""), str(row.get("period") or "")),
                    )
                    for row in rows:
                        row["symbol"] = _normalise_symbol(symbol)
                        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        else:
            payload = {
                _normalise_symbol(symbol): [dict(row) for row in history[symbol] if isinstance(row, Mapping)]
                for symbol in sorted(history)
            }
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=indent, sort_keys=True)
                handle.write("\n")
        os.replace(temp_path, destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return destination


def _write_json_atomic(path: str | Path, payload: Mapping[str, Any]) -> Path:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(destination.name + ".tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return destination


@dataclass(frozen=True)
class HistoryBuildResult:
    history: dict[str, list[dict[str, Any]]]
    issues: tuple[dict[str, Any], ...]
    report: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"history": self.history, "issues": list(self.issues), "report": self.report}


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build causal point-in-time fundamental history")
    parser.add_argument("--financial-data", required=True, help="financial filings JSON/JSONL/CSV")
    parser.add_argument("--market-data", required=True, help="market-day PE/market-cap JSON/JSONL/CSV")
    parser.add_argument("--output", required=True, help="output history JSON/JSONL")
    parser.add_argument("--report", default=None, help="optional coverage report JSON path")
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument(
        "--drop-uncovered",
        action="store_true",
        help="omit market rows with no announced filing (default keeps them for coverage auditing)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_cli_parser().parse_args(argv)
    output_path = Path(args.output).expanduser()
    report_path = Path(args.report).expanduser() if args.report else None
    if report_path is not None and report_path.resolve() == output_path.resolve():
        raise ValueError("--report and --output must be different files")
    result = build_point_in_time_history(
        load_history_records(args.financial_data),
        load_history_records(args.market_data),
        start_date=args.start_date,
        end_date=args.end_date,
        include_uncovered_market_days=not args.drop_uncovered,
    )
    output = write_fundamental_history(result.history, output_path)
    report = {**result.report, "issues": list(result.issues), "output": str(output)}
    if report_path is not None:
        _write_json_atomic(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through CLI smoke tests
    raise SystemExit(main())
