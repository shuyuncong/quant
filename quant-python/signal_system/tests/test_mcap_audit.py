import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcap_audit import board, rank_in_groups, spearman


def test_spearman_uses_average_ranks_for_ties():
    assert spearman([1, 1, 2], [1, 2, 3]) > 0.8


def test_rank_groups_can_be_signal_day_and_industry():
    rows = [
        {"symbol": "000001", "signal_day": "2025-01-01", "market_cap": 10},
        {"symbol": "000002", "signal_day": "2025-01-01", "market_cap": 20},
        {"symbol": "000003", "signal_day": "2025-01-01", "market_cap": 100},
    ]
    industries = {"000001": "A", "000002": "A", "000003": "B"}
    ranks = rank_in_groups(
        rows,
        lambda r: (r["signal_day"], industries[r["symbol"]]),
        lambda r: r["market_cap"],
    )
    assert ranks[id(rows[0])] == 0.0
    assert ranks[id(rows[1])] == 1.0
    assert ranks[id(rows[2])] == 0.5


def test_board_prefix_mapping():
    assert board("688001") == "科创板"
    assert board("300001") == "创业板"
    assert board("002001") == "中小板"
    assert board("600001") == "主板"
