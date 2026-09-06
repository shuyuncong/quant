from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import volume_unit_continuity_audit as audit


def _frame(
    volumes: np.ndarray | list[float],
    *,
    adjustment: str = "qfq",
    start: str = "2026-01-02",
) -> pd.DataFrame:
    values = np.asarray(volumes, dtype=float)
    closes = 10.0 + np.arange(len(values), dtype=float) * 0.01
    frame = pd.DataFrame(
        {
            "datetime": pd.bdate_range(start, periods=len(values)),
            "open": closes,
            "high": closes + 0.2,
            "low": closes - 0.2,
            "close": closes + 0.05,
            "volume": values,
            "amount": np.zeros(len(values)),
            "is_closed": [True] * len(values),
        }
    )
    frame.attrs.update({"adjust": adjustment, "timeframe": "1d"})
    return frame


def _validated(
    volumes: np.ndarray | list[float],
    *,
    adjustment: str = "qfq",
) -> pd.DataFrame:
    return audit._validate_history_frame(
        _frame(volumes, adjustment=adjustment),
        Path(f"history_{adjustment}.pkl"),
        adjustment,
    )


def test_contract_constants_are_frozen():
    assert audit.WINDOW_BARS == 60
    assert audit.BLOCK_COUNT == 6
    assert audit.BLOCK_BARS == 10
    assert audit.VOLUME_UNIT_SHARES == 100.0
    assert audit.MIN_SYNCHRONIZED_SYMBOLS == 20
    assert audit.UNIT_SHIFT_LOWER_RATIO == 0.1
    assert audit.UNIT_SHIFT_UPPER_RATIO == 10.0


@pytest.mark.parametrize("value", ["1", "00001", "000001.SZ", "ABC001", ""])
def test_symbol_requires_six_digits(value: str):
    with pytest.raises(RuntimeError, match="six digits"):
        audit._normalize_symbol(value)


def test_load_identities_ignores_outcomes_and_rejects_symbol_day_duplicates(
    tmp_path: Path,
):
    path = tmp_path / "candidates.jsonl"
    rows = [
        {
            "symbol": "000001",
            "signal_day": "2026-03-01",
            "signal_type": "above",
            "future_40d": 999.0,
        }
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    assert audit._load_identities(path) == [
        {
            "symbol": "000001",
            "signal_day": "2026-03-01",
            "signal_type": "above",
        }
    ]

    rows.append({**rows[0], "signal_type": "buy_1"})
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="symbol x signal_day"):
        audit._load_identities(path)


def test_history_validation_accepts_strict_daily_volume_frame():
    frame = _frame(np.arange(61, dtype=float) + 1.0)
    result = audit._validate_history_frame(frame, Path("qfq.pkl"), "qfq")
    assert list(result.columns) == [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]
    assert len(result) == 61


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda f: f.attrs.update({"adjust": "none"}), "adjustment"),
        (lambda f: f.attrs.update({"timeframe": "5m"}), "timeframe"),
        (lambda f: f.__setitem__("is_closed", [1] * len(f)), "non-bool"),
        (lambda f: f.__setitem__("volume", ["1"] * len(f)), "non-numeric"),
        (lambda f: f.__setitem__("amount", [True] * len(f)), "non-numeric"),
        (
            lambda f: f.__setitem__(
                "open", [math.nan] + [10.0] * (len(f) - 1)
            ),
            "finite",
        ),
        (
            lambda f: f.__setitem__(
                "high", [math.inf] + [11.0] * (len(f) - 1)
            ),
            "finite",
        ),
        (lambda f: f.__setitem__("low", [0.0] * len(f)), "positive"),
        (lambda f: f.__setitem__("volume", [-1.0] * len(f)), "non-negative"),
        (lambda f: f.__setitem__("amount", [-1.0] * len(f)), "non-negative"),
        (lambda f: f.__setitem__("high", f["close"] - 0.1), "relationships"),
        (lambda f: f.__setitem__("low", f["close"] + 0.1), "relationships"),
    ],
)
def test_history_corruption_fails_closed(mutate, message):
    frame = _frame(np.arange(61, dtype=float) + 1.0)
    mutate(frame)
    with pytest.raises(RuntimeError, match=message):
        audit._validate_history_frame(frame, Path("qfq.pkl"), "qfq")


def test_history_non_dataframe_missing_column_and_bad_dates_fail():
    with pytest.raises(RuntimeError, match="not a DataFrame"):
        audit._validate_history_frame(pd.Series([1]), Path("x"), "qfq")

    missing = _frame(np.ones(61)).drop(columns=["low"])
    missing.attrs.update({"adjust": "qfq", "timeframe": "1d"})
    with pytest.raises(RuntimeError, match="missing columns"):
        audit._validate_history_frame(missing, Path("x"), "qfq")

    duplicate = _frame(np.ones(61))
    duplicate.loc[1, "datetime"] = duplicate.loc[0, "datetime"]
    with pytest.raises(RuntimeError, match="duplicate"):
        audit._validate_history_frame(duplicate, Path("x"), "qfq")

    descending = _frame(np.ones(61)).iloc[::-1].reset_index(drop=True)
    descending.attrs.update({"adjust": "qfq", "timeframe": "1d"})
    with pytest.raises(RuntimeError, match="strictly increasing"):
        audit._validate_history_frame(descending, Path("x"), "qfq")


def test_unused_unclosed_bad_ohlc_is_validated_before_filtering():
    frame = _frame(np.ones(61))
    frame.loc[len(frame) - 1, "is_closed"] = False
    frame["high"] = frame["high"].astype(object)
    frame.loc[len(frame) - 1, "high"] = "broken"
    with pytest.raises(RuntimeError, match="non-numeric"):
        audit._validate_history_frame(frame, Path("x"), "qfq")


def test_required_window_includes_signal_day_and_requires_sixty_positive_bars():
    validated = _validated(np.arange(61, dtype=float) + 1.0)
    day = validated.index[-1].date().isoformat()
    window = audit._required_window(validated, day)
    assert len(window) == 60
    assert window.index[-1] == validated.index[-1]
    assert window.iloc[0]["volume"] == 2.0

    with pytest.raises(RuntimeError, match="signal day is absent"):
        audit._required_window(validated, "2026-12-31")
    with pytest.raises(RuntimeError, match="insufficient"):
        audit._required_window(validated.head(59), validated.index[58].date().isoformat())

    zero = validated.copy()
    zero.loc[zero.index[-1], "volume"] = 0.0
    with pytest.raises(RuntimeError, match="non-positive"):
        audit._required_window(zero, day)


def test_volume_features_match_known_block_means_and_diagnostics():
    block_means = np.asarray([10, 20, 30, 40, 50, 60], dtype=float)
    volume = np.repeat(block_means, 10)
    result = audit._volume_features(volume)
    mean_block = float(np.mean(block_means))
    positions = np.arange(6, dtype=float)
    centered = positions - positions.mean()
    slope = float(
        np.sum(centered * (block_means - block_means.mean()))
        / np.sum(np.square(centered))
    )
    assert result["volume_instability_cv_6x10"] == pytest.approx(
        np.std(block_means, ddof=0) / mean_block
    )
    assert result["daily_volume_cv_60"] == pytest.approx(
        np.std(volume, ddof=0) / np.mean(volume)
    )
    assert result["block_mean_volume_range_ratio_6x10"] == pytest.approx(
        50 / mean_block
    )
    assert result["latest_block_mean_volume_ratio_6x10"] == pytest.approx(
        60 / mean_block
    )
    assert result["minimum_block_mean_volume_ratio_6x10"] == pytest.approx(
        10 / mean_block
    )
    assert result["block_mean_volume_trend_slope_ratio_6x10"] == pytest.approx(
        slope / mean_block
    )


def test_volume_features_are_scale_invariant_and_no_floor_is_used():
    volume = np.repeat(np.asarray([1, 2, 3, 4, 5, 6], dtype=float), 10)
    expected = audit._volume_features(volume)
    scaled = audit._volume_features(volume * 100.0)
    tiny = audit._volume_features(volume * (2.0**-40))
    for key in (
        "volume_instability_cv_6x10",
        "daily_volume_cv_60",
        "block_mean_volume_range_ratio_6x10",
        "latest_block_mean_volume_ratio_6x10",
        "minimum_block_mean_volume_ratio_6x10",
        "block_mean_volume_trend_slope_ratio_6x10",
    ):
        assert scaled[key] == pytest.approx(expected[key])
        assert tiny[key] == pytest.approx(expected[key])
    assert audit._volume_features(np.ones(60))["volume_instability_cv_6x10"] == 0.0


def test_volume_features_reject_overflowed_derived_values():
    extreme = np.repeat(
        np.asarray([1.0e200, 1.0, 1.0e200, 1.0, 1.0e200, 1.0]),
        10,
    )
    with pytest.raises(RuntimeError, match="features must all be finite"):
        audit._volume_features(extreme)


@pytest.mark.parametrize(
    "values",
    [np.ones(59), np.zeros(60), np.r_[np.ones(59), -1.0], np.r_[np.ones(59), np.nan]],
)
def test_volume_features_reject_invalid_input(values):
    with pytest.raises(RuntimeError):
        audit._volume_features(values)


def _unit_frame(symbol_index: int, jump: float = 1.0) -> pd.DataFrame:
    values = np.ones(61, dtype=float) * (100.0 + symbol_index)
    values[30:] *= jump
    return _validated(values)


def test_unit_shift_detector_rejects_synchronized_100x_switch():
    dates = _unit_frame(0).index
    required = {f"{index + 1:06d}": set(dates[1:]) for index in range(20)}
    frames = {
        f"{index + 1:06d}": _unit_frame(index, jump=100.0)
        for index in range(20)
    }
    report = audit._unit_shift_report(required, frames)
    assert report["all_pass"] is False
    assert any(item["median_ratio"] >= 100.0 for item in report["failures"])


def test_unit_shift_detector_deduplicates_overlaps_and_uses_prior_outside_window():
    frame = _unit_frame(0)
    dates = set(frame.index[1:])
    report = audit._unit_shift_report({"000001": dates}, {"000001": frame})
    assert report["unique_symbol_date_observations"] == 60
    assert report["all_pass"] is True

    duplicate_dates = set(dates)
    duplicate_dates.update(dates)
    same = audit._unit_shift_report(
        {"000001": duplicate_dates}, {"000001": frame}
    )
    assert same["unique_symbol_date_observations"] == 60


def test_unit_shift_detector_rejects_missing_or_nonpositive_preceding_bar():
    frame = _unit_frame(0)
    with pytest.raises(RuntimeError, match="preceding closed bar"):
        audit._unit_shift_report(
            {"000001": {frame.index[0]}}, {"000001": frame}
        )
    bad = frame.copy()
    bad.iloc[0, bad.columns.get_loc("volume")] = 0.0
    with pytest.raises(RuntimeError, match="non-positive"):
        audit._unit_shift_report(
            {"000001": {bad.index[1]}}, {"000001": bad}
        )


def test_current_producer_contract_is_read_only_and_explicitly_limited():
    report = audit._validate_producer_contract(audit.CONFIG_PATH.resolve())
    assert report["all_pass"] is True
    assert report["volume_unit_shares"] == 100.0
    assert report["historical_history_producer_reconstructable"] is False
    assert report["current_code_is_interpretation_provenance_only"] is True


def _install_audit_integration_harness(
    monkeypatch: pytest.MonkeyPatch,
    qfq: pd.DataFrame,
    none: pd.DataFrame,
) -> None:
    signal_day = qfq.index[-1].date().isoformat()
    identity = {
        "symbol": "000001",
        "signal_day": signal_day,
        "signal_type": "macd_above",
    }
    monkeypatch.setattr(
        audit,
        "_validate_producer_contract",
        lambda config_path: {"all_pass": True},
    )

    def fake_sha(path: Path) -> str:
        name = Path(path).name
        if name == "candidate_integrity_manifest.json":
            return audit.EXPECTED_INTEGRITY_MANIFEST_SHA256
        for split in audit.SPLITS:
            if name == f"candidates_{split}.jsonl":
                return audit.EXPECTED_CANONICAL_SHA256[split]
        return "fixture"

    monkeypatch.setattr(audit, "file_sha256", fake_sha)
    monkeypatch.setattr(
        audit,
        "_load_identities",
        lambda path: [dict(identity)],
    )
    monkeypatch.setattr(
        audit,
        "_validate_integrity_manifest",
        lambda *args: {"all_pass": True},
    )

    def fake_snapshot(paths):
        return {
            name: {
                "path": f"C:/fixture/000001_{name.split(':')[0]}.pkl",
                "size_bytes": 1,
                "sha256": name,
            }
            for name in paths
        }

    monkeypatch.setattr(audit, "_snapshot_named_paths", fake_snapshot)
    monkeypatch.setattr(
        audit,
        "_snapshot_manifest",
        lambda snapshot: audit.EXPECTED_DUAL_HISTORY_MANIFEST_SHA256,
    )
    monkeypatch.setattr(
        audit,
        "_load_history",
        lambda path, adjustment: (
            qfq.copy() if adjustment == "qfq" else none.copy()
        ),
    )


@pytest.mark.parametrize("mismatch", ["date", "volume"])
def test_audit_integration_rejects_qfq_none_window_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
):
    qfq = _validated(np.arange(61, dtype=float) + 100.0)
    none = _validated(
        np.arange(61, dtype=float) + 100.0,
        adjustment="none",
    )
    if mismatch == "date":
        changed = list(none.index)
        changed[1] = changed[1] - pd.Timedelta(days=1)
        none.index = pd.DatetimeIndex(changed)
    else:
        none.loc[none.index[-1], "volume"] += 1.0
    _install_audit_integration_harness(monkeypatch, qfq, none)
    with pytest.raises(RuntimeError, match=f"required {mismatch}"):
        audit.audit(Path("C:/canonical"), Path("C:/config.yaml"))


def test_audit_integration_rejects_out_of_range_amount_implied_vwap(
    monkeypatch: pytest.MonkeyPatch,
):
    qfq = _validated(np.arange(61, dtype=float) + 100.0)
    none = _validated(
        np.arange(61, dtype=float) + 100.0,
        adjustment="none",
    )
    timestamp = qfq.index[-1]
    qfq.loc[timestamp, "amount"] = (
        qfq.loc[timestamp, "volume"] * audit.VOLUME_UNIT_SHARES * 1000.0
    )
    _install_audit_integration_harness(monkeypatch, qfq, none)
    with pytest.raises(RuntimeError, match="amount-implied unit check failed"):
        audit.audit(Path("C:/canonical"), Path("C:/config.yaml"))
