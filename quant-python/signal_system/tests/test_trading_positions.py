"""Tests for the position book and the live trade gate."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from models import SignalEvent  # noqa: E402
from storage.signal_store import SignalStore  # noqa: E402
from trading.positions import TradeGate, resolve_trading_limits  # noqa: E402

EQUITY = 100_000.0


def _config(**overrides):
    limits = {
        "enabled": True,
        "account_equity": EQUITY,
        "max_new_positions_per_day": 2,
        "max_trades_per_day": 5,
        "max_trades_per_symbol_per_day_per_side": 1,
        "max_new_position_pct": 0.25,
        "enforce_max_stocks": True,
        "enforce_single_position_cap": True,
        "enforce_single_day_drawdown": True,
    }
    limits.update(overrides)
    return {
        "trading_limits": limits,
        "position": {"max_stocks": 4, "max_position_per_stock": 0.40, "min_stocks": 2},
        "risk": {"max_single_day_drawdown_pct": 0.02},
    }


def _gate(**overrides) -> TradeGate:
    store = SignalStore(str(Path(tempfile.mkdtemp()) / "gate.db"))
    return TradeGate(store, _config(**overrides))


def _event(symbol: str, side: str, signal_type: str = "t") -> SignalEvent:
    return SignalEvent(
        symbol=symbol,
        name="x",
        timeframe="1d",
        signal_type=signal_type,
        side=side,
        price=10.0,
        structure_time="2026-10-04",
        confirmed_at="2026-10-04T15:00:00+08:00",
        score=50,
    )


class ConfigTests(unittest.TestCase):
    def test_defaults_apply_when_block_missing(self):
        limits = resolve_trading_limits({})
        self.assertTrue(limits["enabled"])
        self.assertEqual(limits["max_trades_per_symbol_per_day_per_side"], 1)

    def test_partial_block_merges_with_defaults(self):
        limits = resolve_trading_limits({"trading_limits": {"max_trades_per_day": 9}})
        self.assertEqual(limits["max_trades_per_day"], 9)
        self.assertEqual(limits["max_new_positions_per_day"], 2)

    def test_disabled_gate_allows_everything(self):
        gate = _gate(enabled=False)
        self.assertTrue(gate.check("000001", "buy", account_equity=EQUITY)["allowed"])


class PerSymbolPerDayTests(unittest.TestCase):
    def test_same_symbol_same_side_blocked_after_first(self):
        gate = _gate()
        self.assertTrue(gate.check("000001", "buy", account_equity=EQUITY)["allowed"])
        gate.record_trade("000001", "buy", 100, 10.0)
        verdict = gate.check("000001", "buy", account_equity=EQUITY)
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["rule"], "per_symbol_per_day")

    def test_buy_and_sell_are_tracked_independently(self):
        gate = _gate()
        gate.record_trade("000001", "buy", 100, 10.0)
        # A sell is a different side and must still be allowed once.
        self.assertTrue(gate.check("000001", "sell", account_equity=EQUITY)["allowed"])
        gate.record_trade("000001", "sell", 100, 11.0)
        verdict = gate.check("000001", "sell", account_equity=EQUITY)
        self.assertFalse(verdict["allowed"])

    def test_other_symbols_unaffected(self):
        gate = _gate()
        gate.record_trade("000001", "buy", 100, 10.0)
        self.assertTrue(gate.check("000002", "buy", account_equity=EQUITY)["allowed"])

    def test_configurable_multiplicity(self):
        gate = _gate(max_trades_per_symbol_per_day_per_side=2)
        gate.record_trade("000001", "buy", 100, 10.0)
        self.assertTrue(gate.check("000001", "buy", account_equity=EQUITY)["allowed"])


class DailyCountTests(unittest.TestCase):
    def test_new_position_cap(self):
        gate = _gate(max_trades_per_symbol_per_day_per_side=9)
        gate.record_trade("000001", "buy", 100, 10.0)
        gate.record_trade("000002", "buy", 100, 10.0)
        verdict = gate.check("000003", "buy", account_equity=EQUITY)
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["rule"], "max_new_positions_per_day")

    def test_adding_to_an_existing_holding_is_not_a_new_position(self):
        gate = _gate(max_new_positions_per_day=1, max_trades_per_symbol_per_day_per_side=9)
        gate.record_trade("000001", "buy", 100, 10.0)
        self.assertTrue(gate.check("000001", "buy", account_equity=EQUITY)["allowed"])

    def test_total_trade_cap_covers_buys_and_sells(self):
        gate = _gate(
            max_trades_per_day=2,
            max_new_positions_per_day=99,
            max_trades_per_symbol_per_day_per_side=9,
        )
        gate.record_trade("000001", "buy", 100, 10.0)
        gate.record_trade("000002", "buy", 100, 10.0)
        verdict = gate.check("000003", "sell", account_equity=EQUITY)
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["rule"], "max_trades_per_day")


class PositionCapTests(unittest.TestCase):
    def test_position_count_cap(self):
        gate = _gate(
            max_new_positions_per_day=99, max_trades_per_symbol_per_day_per_side=9
        )
        for symbol in ("000001", "000002", "000003", "000004"):
            gate.record_trade(symbol, "buy", 100, 10.0)
        verdict = gate.check("000005", "buy", account_equity=EQUITY)
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["rule"], "max_stocks")

    def test_single_position_value_cap(self):
        gate = _gate(
            max_new_positions_per_day=99, max_trades_per_symbol_per_day_per_side=9
        )
        verdict = gate.check("000009", "buy", account_equity=EQUITY, quantity=5000, price=10.0)
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["rule"], "max_position_per_stock")

    def test_within_single_position_cap_is_allowed(self):
        gate = _gate(
            max_new_positions_per_day=99, max_trades_per_symbol_per_day_per_side=9
        )
        verdict = gate.check("000009", "buy", account_equity=EQUITY, quantity=3000, price=10.0)
        self.assertTrue(verdict["allowed"])

    def test_cap_check_skipped_without_equity(self):
        """Without a real equity figure the value cap cannot be judged."""
        gate = _gate(
            max_new_positions_per_day=99, max_trades_per_symbol_per_day_per_side=9
        )
        verdict = gate.check("000009", "buy", account_equity=None, quantity=99999, price=10.0)
        self.assertTrue(verdict["allowed"])


class DrawdownTests(unittest.TestCase):
    def test_second_call_sets_the_baseline_then_blocks(self):
        gate = _gate(max_trades_per_symbol_per_day_per_side=9, max_new_positions_per_day=99)
        self.assertTrue(gate.check("000001", "buy", account_equity=EQUITY)["allowed"])
        verdict = gate.check("000002", "buy", account_equity=EQUITY * 0.97)
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["rule"], "single_day_drawdown")

    def test_small_drawdown_does_not_block(self):
        gate = _gate(max_trades_per_symbol_per_day_per_side=9, max_new_positions_per_day=99)
        gate.check("000001", "buy", account_equity=EQUITY)
        self.assertTrue(
            gate.check("000002", "buy", account_equity=EQUITY * 0.99)["allowed"]
        )

    def test_sells_are_never_blocked_by_drawdown(self):
        """A falling day is exactly when selling must still be possible."""
        gate = _gate(max_trades_per_symbol_per_day_per_side=9)
        gate.check("000001", "buy", account_equity=EQUITY)
        gate.check("000002", "buy", account_equity=EQUITY * 0.90)
        self.assertTrue(
            gate.check("000002", "sell", account_equity=EQUITY * 0.90)["allowed"]
        )

    def test_rule_can_be_disabled(self):
        gate = _gate(
            enforce_single_day_drawdown=False,
            max_trades_per_symbol_per_day_per_side=9,
            max_new_positions_per_day=99,
        )
        gate.check("000001", "buy", account_equity=EQUITY)
        self.assertTrue(
            gate.check("000002", "buy", account_equity=EQUITY * 0.90)["allowed"]
        )


class LedgerTests(unittest.TestCase):
    def test_buy_averages_cost_and_sell_reduces_quantity(self):
        gate = _gate()
        gate.record_trade("000001", "buy", 100, 10.0)
        gate.record_trade("000001", "buy", 100, 20.0)
        position = gate.get_position("000001")
        assert position is not None
        self.assertEqual(position["quantity"], 200)
        self.assertAlmostEqual(position["avg_cost"], 15.0)
        gate.record_trade("000001", "sell", 50, 25.0)
        position = gate.get_position("000001")
        assert position is not None
        self.assertEqual(position["quantity"], 150)
        self.assertAlmostEqual(position["avg_cost"], 15.0)

    def test_full_sell_removes_the_position(self):
        gate = _gate()
        gate.record_trade("000001", "buy", 100, 10.0)
        gate.record_trade("000001", "sell", 100, 11.0)
        self.assertIsNone(gate.get_position("000001"))
        self.assertEqual(gate.list_positions(), [])

    def test_record_trade_rejects_bad_input(self):
        gate = _gate()
        cases = [
            ("hold", 100, 10.0),
            ("buy", 0, 10.0),
            ("buy", 100, 0.0),
        ]
        for side, quantity, price in cases:
            with self.assertRaises(ValueError):
                gate.record_trade("000001", side, quantity, price)

    def test_upsert_position_rejects_non_positive_cost(self):
        gate = _gate()
        with self.assertRaises(ValueError):
            gate.upsert_position("000001", 100, 0.0)

    def test_rejections_are_persisted_for_review(self):
        gate = _gate()
        gate.record_trade("000001", "buy", 100, 10.0)
        gate.check("000001", "buy", account_equity=EQUITY)
        rejections = gate.rejections_today()
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["rule"], "per_symbol_per_day")


class FilterEventsTests(unittest.TestCase):
    def test_gate_blocks_at_the_notification_boundary(self):
        gate = _gate()
        gate.record_trade("000001", "buy", 100, 10.0)
        events = [_event("000001", "buy"), _event("000002", "buy"), _event("000001", "sell")]
        allowed, blocked = gate.filter_events(events, account_equity=EQUITY)
        self.assertEqual([e.symbol for e in allowed], ["000002", "000001"])
        self.assertEqual([b["symbol"] for b in blocked], ["000001"])
        self.assertEqual(blocked[0]["rule"], "per_symbol_per_day")

    def test_events_without_a_side_pass_through(self):
        gate = _gate()
        event = _event("000001", "watch")
        allowed, blocked = gate.filter_events([event], account_equity=EQUITY)
        self.assertEqual(len(allowed), 1)
        self.assertEqual(blocked, [])

    def test_blocked_event_carries_signal_type(self):
        gate = _gate()
        gate.record_trade("000001", "buy", 100, 10.0)
        _, blocked = gate.filter_events(
            [_event("000001", "buy", signal_type="macd_golden_cross")],
            account_equity=EQUITY,
        )
        self.assertEqual(blocked[0]["signal_type"], "macd_golden_cross")


class StorageSchemaTests(unittest.TestCase):
    def test_tables_are_created_by_the_store(self):
        store = SignalStore(str(Path(tempfile.mkdtemp()) / "schema.db"))
        with store._connect() as connection:
            names = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for table in ("position", "trade_ledger", "trade_gate_rejection"):
            self.assertIn(table, names)

    def test_migration_is_idempotent_on_an_existing_db(self):
        path = str(Path(tempfile.mkdtemp()) / "again.db")
        SignalStore(path)
        SignalStore(path)  # must not raise on the second open
        gate = TradeGate(SignalStore(path), copy.deepcopy(_config()))
        self.assertEqual(gate.list_positions(), [])


if __name__ == "__main__":
    unittest.main()
