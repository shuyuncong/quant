"""Runtime / research parity guards for the ETF strategy.

Pins the facts that broke on 2026-11-04:

* the canonical allocation is core 40% + satellite <=35% + backfill 35%, not the
  undocumented 65% that shipped in `engine.py`;
* a crash no longer bypasses the market gate (that branch was never documented);
* the page backtest (`etf_rotation.backtest`) and the research entry
  (`etf_extended_study`) produce the same curve from the same panels;
* the paper ledger sizes buy targets on pre-trade equity, matching the backtest,
  and only plans on the week's last trading day.
"""

from __future__ import annotations

import contextlib
import io
import json
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

import etf_extended_study as study
from etf_rotation import backtest, bridge, engine, service


def series(days: int, values: np.ndarray, index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(values, index=index)


def flat_panels(*, gate_open: bool = True, crash: bool = False,
                trending_satellites: bool = False) -> tuple[pd.DatetimeIndex, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    days = 420
    index = pd.bdate_range("2022-01-03", periods=days)
    steps = np.arange(days, dtype=float)
    columns = list(engine.NAMES)
    close = pd.DataFrame(1.0, index=index, columns=columns)

    if crash:
        market = np.where(steps < 300, 120.0,
                          np.where(steps < 390, 120.0 - 0.667 * (steps - 300),
                                   60.0 + 0.2 * (steps - 390)))
    elif gate_open:
        market = 100.0 + 0.25 * steps
    else:
        market = 100.0 - 0.05 * steps
    close[engine.MARKET] = market
    close["518880"] = 6.0 - 0.003 * steps          # negative 12-1 momentum
    close["511260"] = 100.0 + 0.05 * steps         # positive 12-1 momentum

    if trending_satellites:
        close["159611"] = 1.0 + 0.002 * steps
        close["159770"] = 1.0 + 0.0016 * steps
        close["159611"] += 0.004 * np.sin(steps / 5.0)
        close["159770"] += 0.004 * np.sin(steps / 7.0)
    volume = pd.DataFrame(1e7, index=index, columns=columns)
    open_ = close.shift(1).bfill()
    return index, close, open_, volume


class CanonicalRuleTests(unittest.TestCase):
    def test_core_weight_inside_published_constraint(self) -> None:
        self.assertAlmostEqual(engine.CORE_WEIGHT, 0.40)
        self.assertLessEqual(engine.CORE_WEIGHT, 0.40)

    def test_closed_gate_backfills_and_keeps_cash(self) -> None:
        index, close, _open, volume = flat_panels(gate_open=False)
        signal = engine.compute_signal(close, volume, as_of=index[-1])
        self.assertFalse(signal["gate"])
        weights = signal["target_weights"]
        self.assertAlmostEqual(weights["511260"], 0.61, places=6)
        self.assertAlmostEqual(weights["518880"], 0.14, places=6)
        self.assertAlmostEqual(signal["cash_weight"], 0.25, places=6)
        self.assertEqual(signal["picks"], [])

    def test_crash_does_not_bypass_gate(self) -> None:
        index, close, _open, volume = flat_panels(gate_open=False, crash=True,
                                                  trending_satellites=True)
        signal = engine.compute_signal(close, volume, as_of=index[-1])
        self.assertTrue(signal["crash"], "synthetic series must trip the crash condition")
        self.assertFalse(signal["gate"], "synthetic series must keep the gate closed")
        self.assertEqual(signal["target_weights"].keys() - {"518880", "511260"}, set())
        self.assertNotIn("159611", signal["target_weights"])


class BacktestParityTests(unittest.TestCase):
    def test_page_backtest_matches_research_entry(self) -> None:
        index, close, open_, volume = flat_panels(gate_open=True, trending_satellites=True)
        start = str(index[300].date())
        end = str(index[-1].date())

        panels = study.build_panels(close, volume)
        signals = study.build_signals(close, {s: engine.SATELLITE[s] for s in engine.SATELLITE},
                                      panels, close.index)
        research = study.run_v5(
            close, open_, panels, signals, label="research", start=start, end=end,
            max_picks=1, target_vol=0.10, breadth=True, crash_overlay=False,
            core_w=0.40, sat_max=0.35, backfill=True,
        )
        runtime = backtest.run_panels(close, open_, volume, start=start, end=end)

        self.assertGreater(runtime["trades"], 0, "synthetic window must trade")
        self.assertAlmostEqual(float(research.curve.iloc[-1]),
                               runtime["curve"][-1]["equity"], places=4)
        self.assertEqual(research.trades, runtime["trades"])


def snapshot_payload(strategy_id: str, total_return_pct: float) -> dict:
    return {
        "strategy": "ETF v5.1",
        "strategy_id": strategy_id,
        "as_of": "2026-11-04",
        "mode": "local_refresh",
        "metrics": {"total_return_pct": total_return_pct},
        "signal": {},
        "target": [],
        "curve": [],
    }


class SnapshotVersionTests(unittest.TestCase):
    def test_stale_runtime_snapshot_falls_back_to_committed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            committed = root / "snapshot.json"
            runtime = root / "runtime.json"
            committed.write_text(json.dumps(snapshot_payload(service.CANONICAL["strategy_id"], 90.01)),
                                 encoding="utf-8")
            runtime.write_text(json.dumps(snapshot_payload("etf_v5.1_backfill_65_35", 123.63)),
                               encoding="utf-8")
            with mock.patch.object(service, "SNAPSHOT_PATH", committed), \
                    mock.patch.object(service, "RUNTIME_SNAPSHOT", runtime):
                loaded = service.load_snapshot()
            self.assertEqual(loaded["strategy_id"], service.CANONICAL["strategy_id"])
            self.assertEqual(loaded["metrics"]["total_return_pct"], 90.01)

    def test_current_runtime_snapshot_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            committed = root / "snapshot.json"
            runtime = root / "runtime.json"
            committed.write_text(json.dumps(snapshot_payload(service.CANONICAL["strategy_id"], 90.01)),
                                 encoding="utf-8")
            runtime.write_text(json.dumps(snapshot_payload(service.CANONICAL["strategy_id"], 91.5)),
                               encoding="utf-8")
            with mock.patch.object(service, "SNAPSHOT_PATH", committed), \
                    mock.patch.object(service, "RUNTIME_SNAPSHOT", runtime):
                loaded = service.load_snapshot()
            self.assertEqual(loaded["metrics"]["total_return_pct"], 91.5)


class RefreshBootstrapTests(unittest.TestCase):
    def test_missing_cache_triggers_one_sync(self) -> None:
        calls = {"refresh": 0, "sync": 0}

        def fake_refresh() -> None:
            calls["refresh"] += 1
            if calls["refresh"] == 1:
                raise engine.EtfDataError("缺少 ETF 行情缓存: /app/cache/159611_qfq.pkl")

        def fake_sync(**kwargs) -> dict:
            calls["sync"] += 1
            return {"updated": 52, "total": 52}

        buffer = io.StringIO()
        with mock.patch.object(service, "refresh_snapshot", side_effect=fake_refresh), \
                mock.patch.object(service, "get_overview", return_value={"strategy": "x"}), \
                mock.patch("etf_rotation.data_sync.sync", side_effect=fake_sync), \
                contextlib.redirect_stdout(buffer):
            code = bridge._refresh("config/config.yaml")

        self.assertEqual(code, 0)
        self.assertEqual(calls, {"refresh": 2, "sync": 1})
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["data"]["bootstrap_sync"])


class SyncBudgetTests(unittest.TestCase):
    """补缓存必须在 HTTP 超时前退出，否则前端只能看到网关错误页。"""

    @staticmethod
    def _frame() -> pd.DataFrame:
        frame = pd.DataFrame(
            {
                "datetime": pd.to_datetime(["2026-11-02", "2026-11-03", "2026-11-04"]),
                "open": [1.0, 1.0, 1.0],
                "high": [1.0, 1.0, 1.0],
                "low": [1.0, 1.0, 1.0],
                "close": [1.0, 1.0, 1.0],
                "volume": [1_000_000.0, 1_000_000.0, 1_000_000.0],
            }
        )
        frame.attrs["source"] = "tencent"
        return frame

    def test_budget_exhaustion_reports_partial_progress(self) -> None:
        from etf_rotation import data_sync

        client = mock.MagicMock()
        client.get_bars.return_value = self._frame()
        client.latest_expected_trade_date.return_value = pd.Timestamp("2026-11-04").date()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            data_sync, "MarketDataClient", return_value=client
        ):
            with self.assertRaises(data_sync.EtfSyncError) as raised:
                data_sync.sync(data_dir=Path(directory), time_budget_s=0)

        message = str(raised.exception)
        self.assertIn("etf-cycle", message)
        self.assertIn("已完成 0/", message)

    def test_sync_reports_the_source_that_served_each_symbol(self) -> None:
        from etf_rotation import data_sync

        client = mock.MagicMock()
        client.get_bars.return_value = self._frame()
        client.latest_expected_trade_date.return_value = pd.Timestamp("2026-11-04").date()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            data_sync, "MarketDataClient", return_value=client
        ):
            result = data_sync.sync(data_dir=Path(directory))

        self.assertEqual(len(data_sync.SYMBOLS), result["updated"])
        self.assertEqual({"tencent": len(data_sync.SYMBOLS)}, result["sources"])
        self.assertEqual({}, result["errors"])

    def test_refresh_bootstrap_passes_a_bounded_budget(self) -> None:
        """兜底同步必须有预算：AkShare 约 4 秒/只，47 只足以撞穿 45s 路由超时。"""
        from etf_rotation import bridge, engine, service

        calls = {"refresh": 0}

        def fake_refresh() -> None:
            calls["refresh"] += 1
            if calls["refresh"] == 1:
                raise engine.EtfDataError("缺少 ETF 行情缓存: /app/cache/159611_qfq.pkl")

        seen: dict = {}

        def fake_sync(**kwargs) -> dict:
            seen.update(kwargs)
            return {"updated": 47, "total": 47}

        with mock.patch.object(service, "refresh_snapshot", side_effect=fake_refresh), \
                mock.patch.object(service, "get_overview", return_value={}), \
                mock.patch("etf_rotation.data_sync.sync", side_effect=fake_sync), \
                contextlib.redirect_stdout(io.StringIO()):
            bridge._refresh("config/config.yaml")

        self.assertEqual(bridge.BOOTSTRAP_SYNC_BUDGET_S, seen["time_budget_s"])


class PaperLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(service, "STATE_DIR", root),
            mock.patch.object(service, "PAPER_LEDGER", root / "ledger.json"),
            mock.patch.object(service, "HISTORY_PATH", root / "history.jsonl"),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_buy_target_uses_pretrade_equity(self) -> None:
        ledger = {
            "initialized": True,
            "capital": 1_001_000.0,
            "cash": 1_000.0,
            "positions": {
                "AAA": {"symbol": "AAA", "name": "甲", "shares": 1_000_000,
                         "avg_cost": 1.0, "last_price": 1.0},
            },
            "orders": [],
            "nav_history": [],
        }
        snapshot = {
            "as_of": "2024-01-02",
            "target": [
                {"symbol": "AAA", "name": "甲", "weight": 0.0, "price": 1.0},
                {"symbol": "BBB", "name": "乙", "weight": 0.5, "price": 1.0},
            ],
        }
        result = service._rebalance_paper(ledger, snapshot)

        equity_before = 1_001_000.0
        expected = int(equity_before * 0.5 / 1.0 / service.LOT) * service.LOT
        post_sell_basis = int((equity_before - 800.8) * 0.5 / 1.0 / service.LOT) * service.LOT
        self.assertEqual(result["positions"]["BBB"]["shares"], expected)
        self.assertNotEqual(expected, post_sell_basis, "fixture must separate the two bases")
        self.assertNotIn("AAA", result["positions"])
        self.assertEqual(result["strategy_id"], service.CANONICAL["strategy_id"])

    def test_plan_records_equity_basis(self) -> None:
        service._save_paper({
            "initialized": True, "capital": 250_000.0, "cash": 250_000.0,
            "positions": {}, "orders": [], "nav_history": [],
        })
        snapshot = {
            "as_of": "2024-01-02",
            "target": [{"symbol": "511260", "name": "国债", "weight": 0.61, "price": 100.0}],
        }
        with mock.patch.object(service, "load_snapshot", return_value=snapshot):
            ledger = service.plan_paper()
        plan = ledger["pending_plan"]
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["equity_basis"], "ledger_last_price")

    def test_duplicate_signal_day_is_rejected(self) -> None:
        service._save_paper({
            "initialized": True, "capital": 250_000.0, "cash": 250_000.0,
            "positions": {}, "orders": [], "nav_history": [],
            "last_plan": {"as_of": "2024-01-02", "status": "filled"},
        })
        snapshot = {
            "as_of": "2024-01-02",
            "target": [{"symbol": "511260", "name": "国债", "weight": 0.61, "price": 100.0}],
        }
        with mock.patch.object(service, "load_snapshot", return_value=snapshot):
            with self.assertRaises(ValueError):
                service.plan_paper()

    def test_legacy_plan_is_superseded_not_filled(self) -> None:
        service._save_paper({
            "initialized": True, "capital": 250_000.0, "cash": 250_000.0,
            "positions": {}, "orders": [], "nav_history": [],
            "pending_plan": {
                "as_of": "2026-11-03", "status": "planned",
                "target": [{"symbol": "511260", "name": "国债", "weight": 0.86}],
            },
        })
        with self.assertRaises(ValueError):
            service.fill_paper()
        ledger = service._load_paper()
        self.assertEqual(ledger["pending_plan"]["status"], "superseded")
        self.assertEqual(ledger["last_plan"]["status"], "superseded")
        self.assertEqual(ledger["orders"], [])

    def test_only_week_end_plans(self) -> None:
        index = pd.bdate_range("2026-11-02", periods=10)  # Mon 11-02 .. Fri 11-13
        friday = pd.Timestamp("2026-11-06")
        thursday = pd.Timestamp("2026-11-05")

        # No calendar: conservative Friday rule, never every day.
        self.assertTrue(service._week_end(index, friday)["is_week_end"])
        self.assertEqual(service._week_end(index, friday)["calendar"], "friday_fallback")
        self.assertFalse(service._week_end(index, thursday)["is_week_end"])

        # Exchange calendar: Thursday before a Friday holiday is the week's last session.
        holiday_calendar = {d.date() for d in pd.bdate_range("2026-11-02", periods=4)}
        holiday_calendar |= {d.date() for d in pd.bdate_range("2026-11-09", periods=5)}
        holiday = service._week_end(index, thursday, holiday_calendar)
        self.assertTrue(holiday["is_week_end"])
        self.assertEqual(holiday["week_last_session"], "2026-11-05")
        self.assertEqual(holiday["calendar"], "exchange")

        # Exchange calendar: a normal Thursday must not plan.
        normal_calendar = {d.date() for d in pd.bdate_range("2026-11-02", periods=10)}
        self.assertFalse(service._week_end(index, thursday, normal_calendar)["is_week_end"])
        self.assertTrue(service._week_end(index, friday, normal_calendar)["is_week_end"])


if __name__ == "__main__":
    unittest.main()
