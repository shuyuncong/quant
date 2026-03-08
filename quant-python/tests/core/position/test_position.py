import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.position.position import Position


class PositionTest(unittest.TestCase):
    def test_tracks_base_and_mobile_tranches(self):
        position = Position(ts_code="000001.SZ", name="平安银行", current_price=10.0)
        position.add_base(1000, 9.5)
        position.add_mobile(500, 10.0)
        position.update_price(10.8)

        self.assertEqual(position.total_shares, 1500)
        self.assertAlmostEqual(position.average_cost, 9.6666667, places=4)
        self.assertAlmostEqual(position.market_value, 16200.0, places=2)
        self.assertGreater(position.profit_loss, 0)

    def test_snapshot_contains_standard_fields(self):
        position = Position(
            ts_code="600036.SH",
            name="招商银行",
            base_shares=1000,
            base_cost=35.0,
            mobile_shares=200,
            mobile_cost=36.0,
            current_price=37.5,
        )

        snapshot = position.snapshot()
        self.assertEqual(snapshot["ts_code"], "600036.SH")
        self.assertEqual(snapshot["base_shares"], 1000)
        self.assertEqual(snapshot["mobile_shares"], 200)
        self.assertIn("profit_rate", snapshot)


if __name__ == "__main__":
    unittest.main()
