import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from backfill_history_cache import backfill_one, to_standard


def bars(start="2020-01-02", periods=5, source="baostock"):
    frame = pd.DataFrame({
        "datetime": pd.bdate_range(start, periods=periods),
        "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
        "volume": 1_000_000.0, "amount": 10_500_000.0,
        "turnover_rate": 2.5, "is_closed": True,
    })
    frame.attrs = {"source": source, "adjust": "qfq", "timeframe": "1d", "volume_unit": "shares"}
    return frame


class HistorySource:
    def __init__(self, frame, name="baostock"):
        self.name = name
        self.frame = frame

    def get_daily_data(self, symbol, start_date=None, end_date=None):
        start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
        return self.frame.loc[self.frame["datetime"].between(start, end)].copy()


class BackfillHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "000001_qfq.pkl"

    def test_historical_turnover_survives_the_disk_roundtrip(self):
        frame = bars()
        frame["turnover_rate"] = [1.0, 2.0, 3.0, 4.0, 5.0]
        backfill_one(HistorySource(frame), "000001", self.path, "2020-01-01", "2020-01-10")
        saved = pd.read_pickle(self.path)
        self.assertEqual([1.0, 2.0, 3.0, 4.0, 5.0], saved["turnover_rate"].tolist())
        self.assertEqual([1_000_000.0] * 5, saved["volume"].tolist())
        self.assertEqual("baostock", saved.attrs["source"])
        self.assertEqual("shares", saved.attrs["volume_unit"])
        self.assertEqual("qfq", saved.attrs["adjust"])

    def test_cross_vendor_series_cannot_silently_corrupt_a_frozen_cache(self):
        existing = bars(source="tencent")
        existing.attrs.pop("volume_unit")
        existing.to_pickle(self.path)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "独立研究缓存"):
            backfill_one(HistorySource(bars()), "000001", self.path, "2020-01-01", "2020-01-10")
        self.assertEqual(before, self.path.read_bytes())

    def test_a_new_adjustment_snapshot_replaces_the_whole_series_without_a_splice(self):
        original = bars(periods=7)
        original.iloc[:5].to_pickle(self.path)
        adjusted = original.copy()
        adjusted[["open", "high", "low", "close"]] *= 0.9
        backfill_one(HistorySource(adjusted), "000001", self.path, "2020-01-08", "2020-01-14")
        saved = pd.read_pickle(self.path)
        self.assertEqual(original["datetime"].tolist(), saved["datetime"].tolist())
        np.testing.assert_allclose(saved["close"], np.full(7, 9.45))
        np.testing.assert_allclose(saved["close"].pct_change().dropna(), np.zeros(6))

    def test_an_incomplete_requery_leaves_the_old_series_unchanged(self):
        original = bars(periods=7)
        original.to_pickle(self.path)
        before = self.path.read_bytes()
        incomplete = original.drop(index=3)
        with self.assertRaisesRegex(ValueError, "缺失既有交易日"):
            backfill_one(HistorySource(incomplete), "000001", self.path, "2020-01-01", "2020-01-14")
        self.assertEqual(before, self.path.read_bytes())

    def test_legacy_overlap_preserves_prices_and_only_fills_missing_liquidity(self):
        original = bars(periods=4, source="akshare")
        original["amount"] = [0.0, 12345.0, np.nan, 0.0]
        original["turnover_rate"] = [np.nan, 2.0, np.nan, 4.0]
        original.to_pickle(self.path)
        incoming = bars(periods=6, source="akshare")
        incoming["close"] = 10.501
        backfill_one(HistorySource(incoming, "akshare"), "000001", self.path,
                     "2020-01-01", "2020-01-14", overwrite_amount=True)
        saved = pd.read_pickle(self.path)
        self.assertEqual([10.5] * 4 + [10.501] * 2, saved["close"].tolist())
        self.assertEqual([10_500_000.0, 12345.0, 10_500_000.0, 10_500_000.0], saved["amount"].iloc[:4].tolist())
        self.assertEqual([2.5, 2.0, 2.5, 4.0], saved["turnover_rate"].iloc[:4].tolist())

    def test_one_bad_splice_bar_cannot_hide_behind_a_small_median(self):
        original = bars(periods=7, source="akshare")
        original.to_pickle(self.path)
        before = self.path.read_bytes()
        incoming = original.copy()
        incoming.loc[incoming.index[-1], ["open", "high", "low", "close"]] *= 1.2
        with self.assertRaisesRegex(ValueError, "复权口径漂移"):
            backfill_one(HistorySource(incoming, "akshare"), "000001", self.path,
                         "2020-01-01", "2020-01-14")
        self.assertEqual(before, self.path.read_bytes())

    def test_a_failed_refresh_keeps_the_previous_file(self):
        class BrokenSource:
            name = "baostock"

            def get_daily_data(self, *args, **kwargs):
                raise RuntimeError("transport interrupted")

        bars().to_pickle(self.path)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "transport interrupted"):
            backfill_one(BrokenSource(), "000001", self.path, "2020-01-01", "2020-01-14")
        self.assertEqual(before, self.path.read_bytes())

    def test_illegal_new_bars_do_not_create_a_cache_file(self):
        for column, value in (("close", float("nan")), ("volume", -1), ("high", 8)):
            with self.subTest(column=column):
                frame = bars()
                frame.loc[0, column] = value
                with self.assertRaises(ValueError):
                    backfill_one(HistorySource(frame), "000001", self.path, "2020-01-01", "2020-01-10")
                self.assertFalse(self.path.exists())

    def test_deterministic_results_mark_done_but_failures_retry(self):
        from backfill_history_cache import marks_done

        for result in ("added:5", "refreshed", "already", "no_data"):
            self.assertTrue(marks_done(result), result)
        for result in ("error:BaostockError: timed out", "error:ValueError: bad"):
            self.assertFalse(marks_done(result), result)

    def test_symbol_list_file_keeps_order_and_ignores_comments(self):
        from backfill_history_cache import read_symbol_list

        source = Path(self.directory.name) / "symbols.txt"
        source.write_text("# stratified order\n600000 000001\n\n300750,601318  # tail\n",
                          encoding="utf-8")
        self.assertEqual(["600000", "000001", "300750", "601318"], read_symbol_list(source))

    def test_unclosed_bars_are_not_saved_as_historical_signals(self):
        frame = bars()
        frame.loc[frame.index[-1], "is_closed"] = False
        saved = to_standard(frame)
        self.assertEqual(frame["datetime"].iloc[:-1].tolist(), saved["datetime"].tolist())


if __name__ == "__main__":
    unittest.main()
