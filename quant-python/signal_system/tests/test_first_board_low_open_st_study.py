import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import first_board_low_open_st_study as study

SIGNALS = pd.DataFrame(
    [
        ("000001", "2024-03-01", "2024-03-04"),  # 首板日已是 ST
        ("000002", "2024-03-01", "2024-03-04"),  # 买入当天才戴帽
        ("000003", "2024-03-01", "2024-03-04"),  # 正常
        ("000004", "2024-03-01", "2024-03-04"),  # 数据源没有这只票
    ],
    columns=["symbol", "board_date", "buy_date"],
)
ANSWERS = {
    "000001": {"2024-03-01": 1, "2024-03-04": 1},
    "000002": {"2024-03-01": 0, "2024-03-04": 1},
    "000003": {"2024-03-01": 0, "2024-03-04": 0},
    "000004": {},
}


class StFlagTests(unittest.TestCase):
    def test_st_is_judged_on_the_board_day_and_missing_data_is_unknown_not_clean(self):
        with tempfile.TemporaryDirectory() as folder:
            flags = study.load_flags(SIGNALS, lambda symbol, start, end: ANSWERS[symbol], Path(folder) / "f.json")
        table = study.flag_signals(SIGNALS, flags).set_index("symbol")
        self.assertEqual(table["st"].tolist(), [True, False, False, False])
        self.assertEqual(table["st_any"].tolist(), [True, True, False, False])
        self.assertEqual(table["unknown"].tolist(), [False, False, False, True])

    def test_cached_answers_including_missing_ones_are_not_fetched_again(self):
        calls: list[str] = []

        def query(symbol, start, end):
            calls.append(symbol)
            return ANSWERS[symbol]

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "flags.json"
            first = study.load_flags(SIGNALS, query, path)
            second = study.load_flags(SIGNALS, query, path)
        self.assertEqual(calls, ["000001", "000002", "000003", "000004"])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
