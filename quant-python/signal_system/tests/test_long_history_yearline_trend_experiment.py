from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from long_history_yearline_trend_experiment import (  # noqa: E402
    PULLBACK_ABOVE_TOLERANCE,
    Signal,
    _add_indicators,
    _combined_signals,
    _guard_development_path,
    _raw_signals,
    _simulate,
    _stop_loss_pct,
)


def _frame(count: int = 330) -> pd.DataFrame:
    close = np.linspace(8.0, 12.0, count)
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range("2023-01-02", periods=count),
            "open": close - 0.02,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": np.full(count, 100.0),
            "is_closed": True,
        }
    )


def _signal(index: int, signal_type: str) -> Signal:
    return Signal(
        symbol="000001",
        name="test",
        signal_type=signal_type,
        index=index,
        signal_day="2025-01-02",
        signal_close=10.0,
        ma250=9.5,
        ma250_slope_20=0.01,
        ma60=10.0,
        ma120=9.8,
        volume_ratio_20=1.5,
        atr14_pct=0.03,
    )


def _execution(max_holding_bars: int = 2) -> dict:
    return {
        "commission_pct": 0.0,
        "minimum_commission": 0.0,
        "stamp_tax_pct": 0.0,
        "slippage_pct": 0.0,
        "lot_size": 100,
        "t_plus_one": True,
        "price_limit_model": "none",
        "intrabar_conflict": "stop_first",
        "take_profit_pct": 0.30,
        "profit_protection": {"mode": "none"},
        "chan_zero_axis": {
            "max_holding_bars": max_holding_bars,
            "timeout_exit_mode": "fixed",
        },
    }


def _trade_frame() -> tuple[pd.DataFrame, list]:
    days = list(pd.bdate_range("2025-01-02", periods=5).date)
    frame = pd.DataFrame(
        {
            "datetime": pd.to_datetime(days),
            "open": [10.0, 11.0, 11.2, 11.4, 11.5],
            "high": [10.2, 11.3, 11.4, 11.6, 11.7],
            "low": [9.8, 10.8, 11.0, 11.2, 11.3],
            "close": [10.0, 11.1, 11.3, 11.5, 11.6],
            "volume": [100.0] * 5,
            "is_closed": [True] * 5,
        }
    )
    return frame, days


def test_dynamic_stop_is_clipped_to_five_and_eight_percent():
    low = _signal(1, "yearline_volume_breakout")
    high = Signal(**{**low.__dict__, "atr14_pct": 0.06})
    assert _stop_loss_pct(low, "dynamic_sl5_sl8") == pytest.approx(0.06)
    assert _stop_loss_pct(Signal(**{**low.__dict__, "atr14_pct": 0.01}), "dynamic_sl5_sl8") == 0.05
    assert _stop_loss_pct(high, "dynamic_sl5_sl8") == 0.08


def test_breakout_volume_baseline_excludes_current_bar():
    raw = _frame()
    enriched = _add_indicators(raw)
    index = 300
    enriched.loc[index - 1, "close"] = enriched.loc[index - 1, "ma250"] * 0.999
    enriched.loc[index, "close"] = enriched.loc[index, "ma250"] * 1.001
    enriched.loc[index, "volume"] = enriched.loc[index, "prior_volume_mean20"] * 1.5
    enriched.loc[index, "ma60"] = enriched.loc[index, "ma120"] + 0.2
    enriched.loc[index, "ma120"] = enriched.loc[index, "ma250"] + 0.2
    signals = _raw_signals("000001", "test", enriched)["volume_breakout"]
    assert any(signal.index == index for signal in signals)
    assert next(signal for signal in signals if signal.index == index).volume_ratio_20 == pytest.approx(1.5)


def test_pullback_requires_touch_zone_close_hold_and_bullish_recovery():
    raw = _frame()
    enriched = _add_indicators(raw)
    index = 300
    ma250 = float(enriched.loc[index, "ma250"])
    enriched.loc[index, "ma60"] = enriched.loc[index, "ma120"] + 0.2
    enriched.loc[index, "ma120"] = ma250 + 0.2
    enriched.loc[index - 1, "close"] = float(enriched.loc[index - 1, "ma250"]) * 1.01
    enriched.loc[index, "low"] = ma250 * (1 + PULLBACK_ABOVE_TOLERANCE)
    enriched.loc[index, "open"] = ma250 * 1.001
    enriched.loc[index, "close"] = ma250 * 1.005
    signals = _raw_signals("000001", "test", enriched)["yearline_pullback"]
    assert any(signal.index == index for signal in signals)
    enriched.loc[index, "close"] = ma250 * 0.999
    rejected = _raw_signals("000001", "test", enriched)["yearline_pullback"]
    assert not any(signal.index == index for signal in rejected)


def test_combined_route_applies_cross_type_cooldown_with_breakout_priority():
    breakout = _signal(100, "yearline_volume_breakout")
    pullback = _signal(100, "yearline_pullback_hold")
    later = _signal(121, "yearline_pullback_hold")
    combined = _combined_signals(
        {"volume_breakout": [breakout], "yearline_pullback": [pullback, later]}
    )
    assert [signal.signal_type for signal in combined] == [
        "yearline_volume_breakout",
        "yearline_pullback_hold",
    ]
    assert [signal.index for signal in combined] == [100, 121]


def test_simulation_enters_at_next_open_and_keeps_candidate_ids_across_stops():
    frame, days = _trade_frame()
    signal = Signal(
        **{
            **_signal(0, "yearline_volume_breakout").__dict__,
            "signal_day": days[0].isoformat(),
        }
    )
    trades = [
        _simulate(signal, frame, days, {}, _execution(), mode)[0]
        for mode in ("fixed_sl8", "fixed_sl5", "dynamic_sl5_sl8")
    ]
    assert all(trade is not None for trade in trades)
    assert {trade["candidate_id"] for trade in trades if trade is not None} == {
        signal.candidate_id
    }
    assert {trade["entry_day"] for trade in trades if trade is not None} == {
        days[1].isoformat()
    }
    assert {trade["entry_price"] for trade in trades if trade is not None} == {11.0}


def test_yearline_break_at_entry_close_exits_at_following_open():
    frame, days = _trade_frame()
    signal = Signal(
        **{
            **_signal(0, "yearline_pullback_hold").__dict__,
            "signal_day": days[0].isoformat(),
        }
    )
    trade, reason = _simulate(
        signal,
        frame,
        days,
        {1: "yearline_trend_break"},
        _execution(max_holding_bars=3),
        "dynamic_sl5_sl8",
    )
    assert reason is None
    assert trade is not None
    assert trade["exit_reason"] == "yearline_trend_break"
    assert trade["exit_trigger_day"] == days[1].isoformat()
    assert trade["exit_day"] == days[2].isoformat()
    assert trade["exit_price"] == 11.2
    assert trade["exit_session"] == "open"


def test_holdout_paths_are_blocked():
    with pytest.raises(ValueError, match="Holdout path is blocked"):
        _guard_development_path(Path("D:/tmp/yearline_holdout"))
