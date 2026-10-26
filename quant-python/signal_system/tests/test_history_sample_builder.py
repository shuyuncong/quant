import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import scale_out_take_profit_study as study


class HistoricalSampleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cache = self.root / "new_vendor_history"
        self.output = self.root / "frozen_sample"
        self.cache.mkdir()
        self.output.mkdir()
        previous = study._WORKER.copy()
        self.addCleanup(lambda: (study._WORKER.clear(), study._WORKER.update(previous)))

    def frame(self):
        dates = pd.bdate_range("2018-01-02", periods=450)
        close = np.linspace(10, 12, len(dates)) + np.sin(np.arange(len(dates)) / 10) * 0.2
        result = pd.DataFrame({
            "datetime": dates, "open": close, "high": close + 0.2,
            "low": close - 0.2, "close": close, "volume": 1_000_000.0,
            "amount": 10_000_000.0, "turnover_rate": 2.5, "is_closed": True,
        })
        result.attrs = {"source": "baostock", "adjust": "qfq", "timeframe": "1d"}
        return result

    def test_future_holdout_rows_cannot_enter_a_frozen_historical_sample(self):
        frame = self.frame()
        start = frame["datetime"].iloc[300].strftime("%Y-%m-%d")
        end = frame["datetime"].iloc[350].strftime("%Y-%m-%d")
        frame.to_pickle(self.cache / "000001_qfq.pkl")
        study._init_sample_worker(json.dumps({}), start, end, str(self.output), str(self.cache))
        symbol, reason = study._out_of_sample_job("000001")
        self.assertEqual(("000001", ""), (symbol, reason))
        frozen = pd.read_pickle(self.output / "inputs/000001.pkl")
        pd.testing.assert_frame_equal(frame.iloc[:351].reset_index(drop=True), frozen)
        payload = json.loads((self.output / "000001.signals.json").read_text(encoding="utf-8"))
        for indices in payload["signals"].values():
            self.assertTrue(all(index <= 350 for index in indices))
        self.assertEqual(frame["datetime"].iloc[300:351].dt.strftime("%Y-%m-%d").tolist(), payload["dates"])

    def test_unadjusted_history_is_excluded_instead_of_mislabeled_qfq(self):
        frame = self.frame()
        frame.attrs["adjust"] = "none"
        frame.to_pickle(self.cache / "000001_qfq.pkl")
        study._init_sample_worker(json.dumps({}), "2019-03-01", "2019-06-30",
                                  str(self.output), str(self.cache))
        symbol, reason = study._out_of_sample_job("000001")
        self.assertEqual("000001", symbol)
        self.assertIn("前复权日线", reason)
        self.assertFalse((self.output / "inputs/000001.pkl").exists())


if __name__ == "__main__":
    unittest.main()
