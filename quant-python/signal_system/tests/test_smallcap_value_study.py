"""Offline invariants for the EP/ME small-cap study."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from smallcap_optimized_study import OptConfig
from smallcap_value_study import (
    earnings_yield,
    latest_value,
    load_value,
    select_value_targets,
)


def _features(days: pd.DatetimeIndex, symbols: list[str]) -> dict[str, object]:
    close = pd.DataFrame(10.0, index=days, columns=symbols)
    mv = pd.DataFrame(
        {symbol: 1_000_000_000.0 + index for index, symbol in enumerate(symbols)},
        index=days,
    )
    rank = pd.DataFrame(
        {symbol: float(index + 1) for index, symbol in enumerate(symbols)},
        index=days,
    )
    valid = pd.DataFrame(True, index=days, columns=symbols)
    return {
        "close": close,
        "mv": mv,
        "rank": rank,
        "valid": valid,
        "amount": pd.DataFrame(1.0, index=days, columns=symbols),
        "open": close.copy(),
        "prev_close": close.copy(),
    }


class ValueStudyTests(unittest.TestCase):
    def test_loader_keeps_interim_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "value.csv"
            pd.DataFrame(
                [
                    {
                        "symbol": "000001",
                        "report_year": 2020,
                        "period": "2020-03-31",
                        "net_profit": 10.0,
                        "revenue": 100.0,
                        "available_date": "2020-04-20",
                        "listed_status": "current",
                    },
                    {
                        "symbol": "000001",
                        "report_year": 2020,
                        "period": "2020-06-30",
                        "net_profit": 25.0,
                        "revenue": 200.0,
                        "available_date": "2020-08-20",
                        "listed_status": "current",
                    },
                ]
            ).to_csv(path, index=False)
            panel = load_value(path)
        self.assertEqual(len(panel), 2)
        self.assertEqual(panel["period"].nunique(), 2)

    def test_latest_value_is_asof(self) -> None:
        value = pd.DataFrame(
            [
                {
                    "symbol": "000001",
                    "report_year": 2019,
                    "net_profit": 10.0,
                    "revenue": 100.0,
                    "available_date": pd.Timestamp("2020-04-20"),
                    "listed_status": "current",
                },
                {
                    "symbol": "000001",
                    "report_year": 2020,
                    "net_profit": 20.0,
                    "revenue": 200.0,
                    "available_date": pd.Timestamp("2021-04-20"),
                    "listed_status": "current",
                },
            ]
        )
        before = latest_value(value, pd.Timestamp("2021-04-19"))
        after = latest_value(value, pd.Timestamp("2021-04-20"))
        self.assertEqual(float(before.loc["000001", "net_profit"]), 10.0)
        self.assertEqual(float(after.loc["000001", "net_profit"]), 20.0)

    def test_latest_value_handles_interim_periods(self) -> None:
        value = pd.DataFrame(
            [
                {
                    "symbol": "000001",
                    "report_year": 2020,
                    "period": pd.Timestamp("2020-03-31"),
                    "net_profit": 10.0,
                    "available_date": pd.Timestamp("2020-04-20"),
                },
                {
                    "symbol": "000001",
                    "report_year": 2020,
                    "period": pd.Timestamp("2020-06-30"),
                    "net_profit": 25.0,
                    "available_date": pd.Timestamp("2020-08-20"),
                },
                {
                    "symbol": "000001",
                    "report_year": 2020,
                    "period": pd.Timestamp("2020-12-31"),
                    "net_profit": 40.0,
                    "available_date": pd.Timestamp("2021-04-20"),
                },
            ]
        )
        q2 = latest_value(value, pd.Timestamp("2020-08-20"))
        annual = latest_value(value, pd.Timestamp("2021-04-20"))
        self.assertEqual(float(q2.loc["000001", "net_profit"]), 25.0)
        self.assertEqual(float(annual.loc["000001", "net_profit"]), 40.0)

    def test_earnings_yield_uses_float_market_cap(self) -> None:
        latest = pd.DataFrame(
            {"net_profit": [100.0, -10.0]}, index=pd.Index(["000001", "000002"])
        )
        mv = pd.Series([1000.0, 1000.0], index=["000001", "000002"])
        ep = earnings_yield(latest, mv)
        self.assertEqual(list(ep.index), ["000001"])
        self.assertAlmostEqual(float(ep.iloc[0]), 0.1)

    def test_ep_top_prefers_higher_yield(self) -> None:
        days = pd.bdate_range("2020-01-06", periods=2)
        symbols = [f"00000{index}" for index in range(1, 11)]
        features = _features(days, symbols)
        # 10 valid names: rank 3, 4, 5 are the 30%-50% band.
        candidates = symbols[2:5]
        value = pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "report_year": 2019,
                    "net_profit": profit,
                    "revenue": 100.0,
                    "available_date": pd.Timestamp("2019-04-20"),
                    "listed_status": "current",
                }
                for symbol, profit in zip(candidates, [100.0, 300.0, 200.0])
            ]
        )
        adv20 = pd.DataFrame(1e9, index=days, columns=symbols)
        cfg = OptConfig(
            max_names=2,
            rank_low=1,
            rank_high=999999,
            rank_pct_low=0.3,
            rank_pct_high=0.5,
            max_sector_weight=0.5,
        )
        industry = {symbol: symbol for symbol in symbols}
        selected, _ = select_value_targets(
            features, adv20, industry, value, cfg, days[0], "ep_top"
        )
        self.assertEqual(selected[:2], ["000004", "000005"])

    def test_sector_cap_applies_to_value_selection(self) -> None:
        days = pd.bdate_range("2020-01-06", periods=2)
        symbols = [f"00000{index}" for index in range(1, 11)]
        features = _features(days, symbols)
        candidates = symbols[2:5]
        value = pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "report_year": 2019,
                    "net_profit": 100.0,
                    "revenue": 100.0,
                    "available_date": pd.Timestamp("2019-04-20"),
                    "listed_status": "current",
                }
                for symbol in candidates
            ]
        )
        adv20 = pd.DataFrame(1e9, index=days, columns=symbols)
        cfg = OptConfig(
            max_names=2,
            rank_low=1,
            rank_high=999999,
            rank_pct_low=0.3,
            rank_pct_high=0.5,
            max_sector_weight=0.5,
        )
        industry = {
            "000003": "bank",
            "000004": "bank",
            "000005": "tech",
        }
        selected, _ = select_value_targets(
            features, adv20, industry, value, cfg, days[0], "ep_top"
        )
        self.assertEqual(len(selected), 2)
        self.assertEqual(sum(industry[symbol] == "bank" for symbol in selected), 1)


if __name__ == "__main__":
    unittest.main()
