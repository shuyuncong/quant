"""Historical paper replay must match the canonical backtest and stay pure."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from etf_rotation import service
from etf_rotation.paper_replay import replay_paper


def _panels(days: int = 360):
    dates = pd.bdate_range("2023-01-02", periods=days)
    rng = np.random.default_rng(7)
    close = pd.DataFrame({
        "AAA": 10.0 * np.cumprod(1.0 + rng.normal(0.0004, 0.006, days)),
        "BBB": 20.0 * np.cumprod(1.0 + rng.normal(0.0002, 0.004, days)),
    }, index=dates)
    open_ = close * (1.0 + rng.normal(0.0, 0.001, size=close.shape))
    volume = pd.DataFrame(1_000_000.0, index=dates, columns=close.columns)
    return close, open_, volume


def _signal(close, volume, as_of):
    # Fixed weights, weekly; the replay's job is execution parity, not alpha.
    return {"target_weights": {"AAA": 0.6, "BBB": 0.4}}


class PaperReplayTests(unittest.TestCase):
    def test_replay_matches_canonical(self) -> None:
        close, open_, volume = _panels()
        result = replay_paper(
            close, open_, volume,
            start=str(close.index[100].date()), end=str(close.index[-1].date()),
            initial=250_000.0, signal_fn=_signal,
        )
        parity = result["parity"]
        self.assertGreater(parity["points"], 100)
        self.assertLess(abs(parity["total_return_diff_pp"]), 0.1)
        self.assertLess(parity["max_abs_relative_equity_diff_pct"], 0.1)
        self.assertLess(abs(parity["final_equity_diff"]), 100.0)

    def test_replay_does_not_touch_live_ledger(self) -> None:
        close, open_, volume = _panels(160)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.json"
            with mock.patch.object(service, "PAPER_LEDGER", ledger_path):
                replay_paper(
                    close, open_, volume,
                    start=str(close.index[100].date()), end=str(close.index[-1].date()),
                    initial=250_000.0, signal_fn=_signal,
                )
                self.assertFalse(ledger_path.exists())

    def test_replay_orders_are_non_negative(self) -> None:
        close, open_, volume = _panels(200)
        result = replay_paper(
            close, open_, volume,
            start=str(close.index[100].date()), end=str(close.index[-1].date()),
            initial=250_000.0, signal_fn=_signal,
        )
        self.assertGreater(result["paper"]["orders"], 0)
        self.assertGreaterEqual(result["paper"]["final_equity"], 0.0)
        self.assertGreaterEqual(result["paper"]["avg_exposure"], 0.0)
        self.assertLessEqual(result["paper"]["avg_exposure"], 1.0)


if __name__ == "__main__":
    unittest.main()
