"""Causal fundamental-data contract shared by live selection and backtests.

The live selector may use the provider's latest published snapshot.  Historical
evaluation is deliberately stricter: a snapshot must carry an announcement or
availability date that is not later than the signal date.  A report period alone
is not treated as an availability date because that would introduce look-ahead
bias.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


_FUNDAMENTAL_FIELDS = ("roe", "debt_ratio", "pe", "market_cap")
_DATE_KEYS = (
    "fundamental_data_as_of",
    "available_as_of",
    "published_at",
    "ann_date",
    "announcement_date",
)
_FINANCIAL_DATE_KEYS = (
    "financial_ann_date",
    "fundamental_ann_date",
    "ann_date",
    "announcement_date",
    "published_at",
)


@dataclass(frozen=True)
class FundamentalEvaluation:
    """Outcome of evaluating one fundamental snapshot."""

    passed: bool
    status: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "status": self.status,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "metrics": dict(self.metrics),
        }


def resolve_fundamental_config(
    config: Mapping[str, Any] | None,
    *,
    context: str = "live",
) -> dict[str, Any]:
    """Resolve thresholds and data policy for ``live`` or ``historical`` use.

    Live defaults intentionally preserve the existing ``StockSelector``
    behavior.  Historical backtests are disabled by default and use
    ``unavailable`` when explicitly enabled without coverage.
    """

    root = config or {}
    strategy = root.get("strategy", {}) or {}
    fundamental = strategy.get("fundamental", {}) or {}
    selector = root.get("selector", {}) or {}
    context = str(context or "live").lower().strip()
    if context not in {"live", "historical"}:
        raise ValueError("fundamental context must be 'live' or 'historical'")

    if context == "historical":
        backtest = root.get("backtest", {}) or {}
        historical = backtest.get("fundamental", {}) or {}
        enabled = bool(historical.get("enabled", False))
        missing_data_policy = str(
            historical.get("missing_data_policy", "unavailable")
        ).lower()
    else:
        enabled = bool(
            selector.get(
                "fundamental_enabled",
                fundamental.get("enabled", True),
            )
        )
        missing_data_policy = str(
            selector.get(
                "fundamental_missing_data_policy",
                fundamental.get("missing_data_policy", "reject"),
            )
        ).lower()

    if missing_data_policy not in {"reject", "allow", "unavailable", "allow_unavailable"}:
        raise ValueError(
            "fundamental missing_data_policy must be reject, allow, "
            "unavailable or allow_unavailable"
        )

    return {
        "enabled": enabled,
        "context": context,
        "missing_data_policy": missing_data_policy,
        "roe_min": float(selector.get("roe_min", fundamental.get("min_roe", 10))),
        "debt_ratio_max": float(
            selector.get("debt_ratio_max", fundamental.get("max_debt_ratio", 50))
        ),
        "pe_max": float(
            selector.get("pe_acceptable_max", fundamental.get("max_pe", 30))
        ),
        "market_cap_min": float(
            selector.get("market_cap_min", fundamental.get("min_market_cap", 50))
        ),
        "market_cap_max": float(
            selector.get("market_cap_max", fundamental.get("max_market_cap", 500))
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
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _snapshot_date(snapshot: Mapping[str, Any]) -> date | None:
    for key in _DATE_KEYS:
        value = _coerce_date(snapshot.get(key))
        if value is not None:
            return value
    return None


def _financial_snapshot_date(snapshot: Mapping[str, Any]) -> date | None:
    """Return the filing announcement date when a joined snapshot has one."""

    for key in _FINANCIAL_DATE_KEYS:
        value = _coerce_date(snapshot.get(key))
        if value is not None:
            return value
    return None


def _snapshot_source(snapshot: Mapping[str, Any]) -> str:
    return str(
        snapshot.get("source")
        or snapshot.get("fundamental_data_source")
        or "unknown"
    )


def _snapshot_values(snapshot: Mapping[str, Any]) -> dict[str, float | None]:
    debt = snapshot.get("debt_ratio", snapshot.get("debt_to_assets"))
    market_cap = snapshot.get("market_cap", snapshot.get("circ_mv"))
    return {
        "roe": _as_float(snapshot.get("roe")),
        "debt_ratio": _as_float(debt),
        "pe": _as_float(snapshot.get("pe")),
        "market_cap": _as_float(market_cap),
    }


def _stock_snapshot(stock: Mapping[str, Any]) -> dict[str, Any]:
    nested = stock.get("fundamental_snapshot") or stock.get("fundamentals")
    if isinstance(nested, Mapping):
        snapshot = dict(nested)
        # Preserve explicit metadata supplied alongside a nested snapshot.
        for key in (
            *_DATE_KEYS,
            *_FINANCIAL_DATE_KEYS,
            "source",
            "fundamental_data_source",
        ):
            if key not in snapshot and key in stock:
                snapshot[key] = stock[key]
        if "period" not in snapshot and "fundamental_report_period" in stock:
            snapshot["period"] = stock["fundamental_report_period"]
        return snapshot
    snapshot = dict(stock)
    if "period" not in snapshot and "fundamental_report_period" in stock:
        snapshot["period"] = stock["fundamental_report_period"]
    return snapshot


def _unavailable_evaluation(
    settings: Mapping[str, Any],
    *,
    as_of: date | None,
    reason: str,
    source: str,
    data_as_of: date | None,
) -> FundamentalEvaluation:
    policy = str(settings["missing_data_policy"])
    reject = policy == "reject"
    status = "rejected" if reject else "unavailable"
    reasons = ("fundamental_data_unavailable",) if reject else ()
    warnings = (reason,) if not reject else ()
    metrics = {
        "context": settings["context"],
        "as_of": as_of.isoformat() if as_of else None,
        "data_as_of": data_as_of.isoformat() if data_as_of else None,
        "source": source,
        "data_status": "unavailable",
        "thresholds": {
            key: settings[key]
            for key in (
                "roe_min",
                "debt_ratio_max",
                "pe_max",
                "market_cap_min",
                "market_cap_max",
            )
        },
    }
    return FundamentalEvaluation(not reject, status, reasons, warnings, metrics)


def evaluate_fundamental(
    stock: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    *,
    as_of: date | str | None = None,
    context: str = "live",
) -> FundamentalEvaluation:
    """Evaluate a stock record without fetching or mutating external state."""

    settings = resolve_fundamental_config(config, context=context)
    as_of_date = _coerce_date(as_of)
    if not settings["enabled"]:
        return FundamentalEvaluation(
            True,
            "disabled",
            (),
            (),
            {
                "context": settings["context"],
                "as_of": as_of_date.isoformat() if as_of_date else None,
                "data_status": "disabled",
            },
        )

    snapshot = _stock_snapshot(stock)
    data_as_of = _snapshot_date(snapshot)
    financial_data_as_of = _financial_snapshot_date(snapshot)
    source = _snapshot_source(snapshot)
    if settings["context"] == "historical":
        if as_of_date is None:
            return _unavailable_evaluation(
                settings,
                as_of=None,
                reason="fundamental_as_of_missing",
                source=source,
                data_as_of=data_as_of,
            )
        if data_as_of is None:
            return _unavailable_evaluation(
                settings,
                as_of=as_of_date,
                reason="fundamental_snapshot_has_no_available_date",
                source=source,
                data_as_of=None,
            )
        if data_as_of > as_of_date:
            return _unavailable_evaluation(
                settings,
                as_of=as_of_date,
                reason="fundamental_snapshot_after_signal_day",
                source=source,
                data_as_of=data_as_of,
            )
        if financial_data_as_of is not None and financial_data_as_of > as_of_date:
            return _unavailable_evaluation(
                settings,
                as_of=as_of_date,
                reason="fundamental_financial_snapshot_after_signal_day",
                source=source,
                data_as_of=financial_data_as_of,
            )

    values = _snapshot_values(snapshot)
    missing = [field for field in _FUNDAMENTAL_FIELDS if values[field] is None]
    if missing:
        return _unavailable_evaluation(
            settings,
            as_of=as_of_date,
            reason="fundamental_fields_missing:" + ",".join(missing),
            source=source,
            data_as_of=data_as_of,
        )

    reasons: list[str] = []
    if values["roe"] < settings["roe_min"]:
        reasons.append("fundamental_roe_below_min")
    if values["debt_ratio"] > settings["debt_ratio_max"]:
        reasons.append("fundamental_debt_ratio_above_max")
    if values["pe"] <= 0 or values["pe"] > settings["pe_max"]:
        reasons.append("fundamental_pe_out_of_range")
    if not settings["market_cap_min"] <= values["market_cap"] <= settings["market_cap_max"]:
        reasons.append("fundamental_market_cap_out_of_range")

    metrics = {
        **values,
        "market_cap_unit": "100m_cny",
        "context": settings["context"],
        "as_of": as_of_date.isoformat() if as_of_date else None,
        "data_as_of": data_as_of.isoformat() if data_as_of else None,
        "financial_data_as_of": (
            financial_data_as_of.isoformat() if financial_data_as_of else None
        ),
        "source": source,
        "period": snapshot.get("period"),
        "data_status": "available",
        "thresholds": {
            key: settings[key]
            for key in (
                "roe_min",
                "debt_ratio_max",
                "pe_max",
                "market_cap_min",
                "market_cap_max",
            )
        },
    }
    return FundamentalEvaluation(
        not reasons,
        "rejected" if reasons else "passed",
        tuple(reasons),
        (),
        metrics,
    )


def select_historical_snapshot(
    snapshots: Iterable[Mapping[str, Any]] | None,
    as_of: date | str,
) -> dict[str, Any] | None:
    """Select the latest snapshot announced on or before ``as_of``."""

    as_of_date = _coerce_date(as_of)
    if as_of_date is None:
        return None
    eligible: list[tuple[date, dict[str, Any]]] = []
    for item in snapshots or ():
        if not isinstance(item, Mapping):
            continue
        snapshot = dict(item)
        available = _snapshot_date(snapshot)
        if available is not None and available <= as_of_date:
            eligible.append((available, snapshot))
    if not eligible:
        return None
    eligible.sort(key=lambda item: item[0])
    return eligible[-1][1]


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    code = text.split(".", 1)[0]
    if code.isdigit() and len(code) <= 6:
        return code.zfill(6)
    return text


def load_fundamental_history(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Load a local JSON/JSONL historical snapshot file.

    Accepted JSON forms are ``{"000001.SZ": [{...}]}`` and
    ``{"records": [{"symbol": "000001.SZ", ...}]}``. JSONL accepts one
    record per line with ``symbol``/``ts_code`` plus snapshot fields.
    """

    source_path = Path(path).expanduser()
    if not source_path.exists():
        raise FileNotFoundError(f"fundamental data does not exist: {source_path}")
    result: dict[str, list[dict[str, Any]]] = {}

    def add(symbol: Any, snapshot: Any) -> None:
        key = _normalize_symbol(symbol)
        if not key or not isinstance(snapshot, Mapping):
            return
        item = dict(snapshot)
        item.pop("symbol", None)
        item.pop("ts_code", None)
        result.setdefault(key, []).append(item)

    if source_path.suffix.lower() in {".jsonl", ".ndjson"}:
        with source_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, Mapping):
                    continue
                add(record.get("symbol") or record.get("ts_code"), record)
    else:
        with source_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, Mapping):
            raise ValueError("fundamental JSON must contain an object")
        records = payload.get("records")
        if isinstance(records, list):
            for record in records:
                if isinstance(record, Mapping):
                    add(record.get("symbol") or record.get("ts_code"), record)
        else:
            for symbol, snapshots in payload.items():
                if isinstance(snapshots, Mapping):
                    snapshots = [snapshots]
                if isinstance(snapshots, list):
                    for snapshot in snapshots:
                        add(symbol, snapshot)

    for snapshots in result.values():
        snapshots.sort(key=lambda item: _snapshot_date(item) or date.min)
    return result


def filter_buy_events_by_fundamental(
    symbol: str,
    events: dict[str, list[dict[str, Any]]],
    history: Mapping[str, Iterable[Mapping[str, Any]]] | None,
    config: Mapping[str, Any] | None,
) -> tuple[dict[str, list[dict[str, Any]]], Counter, list[dict[str, Any]]]:
    """Apply the historical fundamental filter to buy events for one symbol."""

    settings = resolve_fundamental_config(config, context="historical")
    if not settings["enabled"]:
        return events, Counter(), []

    snapshots = list((history or {}).get(_normalize_symbol(symbol), ()))
    accepted: list[dict[str, Any]] = []
    skipped: Counter = Counter()
    details: list[dict[str, Any]] = []
    for event in events.get("buy", []):
        day = _coerce_date(event.get("day"))
        snapshot = select_historical_snapshot(snapshots, day) if day else None
        record = {"fundamental_snapshot": snapshot or {}}
        evaluation = evaluate_fundamental(
            record,
            config,
            as_of=day,
            context="historical",
        )
        detail = {
            "symbol": symbol,
            "day": day.isoformat() if day else event.get("day"),
            "signal_type": event.get("signal_type"),
            "status": evaluation.status,
            "reasons": list(evaluation.reasons),
            "warnings": list(evaluation.warnings),
            "metrics": evaluation.metrics,
        }
        if not evaluation.passed:
            skipped.update(evaluation.reasons or ["fundamental_rejected"])
            details.append(detail)
            continue
        enriched = dict(event)
        enriched["fundamental_status"] = evaluation.status
        enriched["fundamental_metrics"] = evaluation.metrics
        enriched["fundamental_warnings"] = list(evaluation.warnings)
        accepted.append(enriched)
        if evaluation.status == "unavailable":
            details.append(detail)

    return {"buy": accepted, "sell": list(events.get("sell", []))}, skipped, details
