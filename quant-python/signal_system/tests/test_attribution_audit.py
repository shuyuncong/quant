import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from attribution_audit import exit_reason_category, judge


def test_exit_reason_semantics_distinguish_chan_sell_from_risk_stop():
    assert exit_reason_category("sell_1") == "chan_sell_1"
    assert exit_reason_category("stop_loss") == "risk_stop_loss"
    assert exit_reason_category("timeout_ma_break") == "time_limit_ma_break"
    assert exit_reason_category("timeout_hard_cap") == "time_limit_hard_cap"


def test_judge_marks_positive_future_negative_trade_as_exit_problem():
    rows = [
        {
            "future_40d": 5.0,
            "mfe": 8.0,
            "trade_pnl_pct": -2.0,
            "post_exit_20d": 1.0,
        }
        for _ in range(3)
    ]
    assert judge(rows).startswith("exit_problem")
