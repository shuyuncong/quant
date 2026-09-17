"""年线(MA250)股票池: 信号逻辑 / 存储迁移 / 扫描 / 桥接测试。

全部使用本地 SQLite fixture 与合成日线, 不联网、不连生产库、不用 Holdout。
"""

from __future__ import annotations

import io
import json
import os
import contextlib
import sqlite3
import sys
from datetime import date
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import web_bridge
from storage.signal_store import SignalStore
from strategy.yearline import (
    POOL_TYPE_YEARLINE,
    add_yearline_indicators,
    last_bar_pullback_candidate,
    prepare_yearline_bars,
    pullback_signal_at,
)
from monitor.service import SignalMonitor


# --------------------------------------------------------------------------- #
# 合成日线
# --------------------------------------------------------------------------- #
def _signal_frame(
    *,
    n: int = 300,
    slope: float = 0.02,
    low_frac: float = 0.01,
    close_frac: float = 0.03,
    open_frac: float = 0.0,
    volume: float = 1_000_000,
    end: date = date(2026, 8, 25),
) -> pd.DataFrame:
    """构造一根满足 yearline_pullback 全部条件的 300 根日线。

    线性上涨序列: MA60 > MA120 > MA250、MA250 上行、前收在年线上方;
    最后一根: 最低进入年线回踩区间、收盘不破年线且 >= 开盘。
    """
    index = pd.date_range(end=pd.Timestamp(end), periods=n, freq="B")
    closes = 10.0 + slope * np.arange(n, dtype=float)
    df = pd.DataFrame(
        {
            "datetime": index,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": float(volume),
        }
    )
    last = n - 1
    ma250 = closes[last - 249 : last + 1].mean()
    df.loc[last, "low"] = ma250 * (1 + low_frac)
    df.loc[last, "open"] = ma250 * (1 + open_frac)
    df.loc[last, "close"] = ma250 * (1 + close_frac)
    df.loc[last, "high"] = max(float(df.loc[last, "close"]), ma250 * (1 + 0.05))
    return df


# --------------------------------------------------------------------------- #
# 信号逻辑
# --------------------------------------------------------------------------- #
class TestYearlineSignal:
    def test_unclosed_last_bar_is_excluded(self):
        df = _signal_frame()
        df["is_closed"] = True
        df.loc[len(df) - 1, "is_closed"] = False
        prepared = prepare_yearline_bars(df)
        assert len(prepared) == len(df) - 1
        assert prepared["datetime"].iloc[-1] != df["datetime"].iloc[-1]
        assert last_bar_pullback_candidate("600036", "", df) is None

    def test_pullback_signal_hits_on_synthetic_frame(self):
        df = _signal_frame()
        candidate = last_bar_pullback_candidate("600036", "招商银行", df)
        assert candidate is not None
        assert candidate["symbol"] == "600036"
        assert candidate["pool_type"] == POOL_TYPE_YEARLINE
        assert candidate["signal_type"] == "yearline_pullback"
        assert candidate["signal_date"] == "2026-08-25"
        assert candidate["entry_reference"] == "next_day_open"
        assert candidate["research_only"] is True
        assert candidate["ma60"] > candidate["ma120"] > candidate["ma250"]
        assert candidate["ma250_slope"] > 0
        assert 5.0 <= candidate["stop_suggestion_pct"] <= 8.0
        assert candidate["volume_ratio"] > 0
        assert candidate["atr14_pct"] > 0

    def test_stop_suggestion_clips_to_5pct_when_atr_tiny(self):
        df = _signal_frame(slope=0.02, close_frac=0.005, low_frac=0.001)
        candidate = last_bar_pullback_candidate("600036", "", df)
        assert candidate is not None
        assert candidate["stop_suggestion_pct"] == pytest.approx(5.0)

    def test_close_below_ma250_rejected(self):
        df = _signal_frame()
        last = len(df) - 1
        ma250 = float(df["close"].iloc[last - 249 : last + 1].mean())
        df.loc[last, "close"] = ma250 * (1 - 0.01)
        df.loc[last, "open"] = ma250 * (1 - 0.02)
        assert last_bar_pullback_candidate("600036", "", df) is None

    def test_low_outside_zone_rejected(self):
        df = _signal_frame()
        last = len(df) - 1
        ma250 = float(df["close"].iloc[last - 249 : last + 1].mean())
        df.loc[last, "low"] = ma250 * (1 + 0.05)
        assert last_bar_pullback_candidate("600036", "", df) is None

    def test_close_below_open_rejected(self):
        df = _signal_frame()
        last = len(df) - 1
        df.loc[last, "open"] = float(df.loc[last, "close"]) * 1.01
        assert last_bar_pullback_candidate("600036", "", df) is None

    def test_falling_ma250_rejected(self):
        df = _signal_frame(slope=-0.02)
        assert last_bar_pullback_candidate("600036", "", df) is None

    def test_prev_close_below_ma250_rejected(self):
        df = _signal_frame()
        last = len(df) - 1
        prev = last - 1
        prev_ma250 = float(df["close"].iloc[prev - 249 : prev + 1].mean())
        df.loc[prev, "close"] = prev_ma250 * 0.99
        assert last_bar_pullback_candidate("600036", "", df) is None

    def test_broken_alignment_rejected(self):
        df = _signal_frame()
        last = len(df) - 1
        df.loc[last - 60 : last - 1, "close"] = 8.0
        assert last_bar_pullback_candidate("600036", "", df) is None

    def test_signal_is_causal_no_future_leak(self):
        df = _signal_frame()
        before = pullback_signal_at(add_yearline_indicators(df), -1)
        assert before is not None
        mutated = df.copy()
        # 篡改未来数据(信号日之后)不应改变信号
        extra = _signal_frame(n=30, end=date(2026, 10, 30))
        mutated = pd.concat([mutated, extra], ignore_index=True)
        after = pullback_signal_at(add_yearline_indicators(mutated), len(df) - 1)
        assert after == before

    def test_insufficient_history_rejected(self):
        df = _signal_frame(n=100)
        assert last_bar_pullback_candidate("600036", "", df) is None


# --------------------------------------------------------------------------- #
# 存储迁移与双池共存
# --------------------------------------------------------------------------- #
class TestCandidateStoreMigration:
    def _legacy_db(self, path: Path):
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE candidate (
                symbol TEXT PRIMARY KEY, name TEXT NOT NULL, score INTEGER NOT NULL,
                expires_on TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE expired_candidate (
                symbol TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', score INTEGER NOT NULL DEFAULT 0,
                expired_on TEXT NOT NULL, reason TEXT NOT NULL DEFAULT 'expired',
                payload TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO candidate VALUES ('600036','招商银行',180,'2026-12-31',?,?)",
            (json.dumps({"symbol": "600036", "score": 180}), "2026-09-01T10:00:00"),
        )
        conn.execute(
            "INSERT INTO candidate VALUES ('000001','平安银行',90,'2026-12-31',?,?)",
            (json.dumps({"symbol": "000001", "score": 90}), "2026-09-01T10:00:00"),
        )
        conn.execute(
            "INSERT INTO expired_candidate VALUES ('000002','万科A',0,'2026-01-01','no_longer_qualified',?,?)",
            (json.dumps({"symbol": "000002"}), "2026-01-01T10:00:00"),
        )
        conn.commit()
        conn.close()

    def test_legacy_rows_migrate_to_macd_zero_axis_with_content_preserved(self, tmp_path):
        db = tmp_path / "legacy.db"
        self._legacy_db(db)
        store = SignalStore(str(db))
        rows = store.active_candidates(pool_type="all")
        assert sorted(row["symbol"] for row in rows) == ["000001", "600036"]
        assert all(row["pool_type"] == "macd_zero_axis" for row in rows)
        macd_row = next(row for row in rows if row["symbol"] == "600036")
        assert macd_row["score"] == 180
        assert macd_row["name"] == "招商银行"
        expired = store.list_expired_candidates(pool_type="macd_zero_axis")
        assert [(row["symbol"], row["pool_type"]) for row in expired] == [("000002", "macd_zero_axis")]

    def test_migration_is_idempotent_and_repeatable(self, tmp_path):
        db = tmp_path / "legacy.db"
        self._legacy_db(db)
        SignalStore(str(db))
        store_again = SignalStore(str(db))  # 第二次打开不重复迁移、不丢数据
        rows = store_again.active_candidates(pool_type="all")
        assert len(rows) == 2
        info = sqlite3.connect(str(db)).execute("PRAGMA table_info(candidate)").fetchall()
        pk_columns = {row[1] for row in info if row[5] > 0}
        assert pk_columns == {"symbol", "pool_type"}

    def test_same_symbol_can_live_in_both_pools(self, tmp_path):
        store = SignalStore(str(tmp_path / "fresh.db"))
        store.upsert_candidates([{"symbol": "600036", "score": 50}], pool_type="macd_zero_axis")
        store.upsert_candidates([{"symbol": "600036", "score": 90}], pool_type="yearline_pullback")
        assert len(store.active_candidates(pool_type="all")) == 2
        assert store.active_candidates(pool_type="yearline_pullback")[0]["score"] == 90

    def test_capacity_truncation_is_scoped_per_pool(self, tmp_path):
        store = SignalStore(str(tmp_path / "fresh.db"))
        store.upsert_candidates(
            [{"symbol": "A", "score": 1}, {"symbol": "B", "score": 2}],
            capacity=1,
            pool_type="yearline_pullback",
        )
        store.upsert_candidates(
            [{"symbol": "C", "score": 9}],
            capacity=1,
            pool_type="macd_zero_axis",
        )
        yearline = sorted(row["symbol"] for row in store.active_candidates(pool_type="yearline_pullback"))
        macd = sorted(row["symbol"] for row in store.active_candidates(pool_type="macd_zero_axis"))
        assert yearline == ["B"]
        assert macd == ["C"]

    def test_default_read_returns_only_macd_pool(self, tmp_path):
        store = SignalStore(str(tmp_path / "fresh.db"))
        store.upsert_candidates([{"symbol": "600036", "score": 1}], pool_type="yearline_pullback")
        assert store.active_candidates() == []
        store.upsert_candidates([{"symbol": "000001", "score": 2}], pool_type="macd_zero_axis")
        assert [row["symbol"] for row in store.active_candidates()] == ["000001"]

    def test_expiry_cleanup_scoped_per_pool(self, tmp_path):
        store = SignalStore(str(tmp_path / "fresh.db"))
        store.upsert_candidates([{"symbol": "OLD", "score": 1}], pool_type="yearline_pullback")
        store.upsert_candidates([{"symbol": "KEEP", "score": 1}], pool_type="macd_zero_axis")
        with store._connect() as conn:
            conn.execute(
                "UPDATE candidate SET expires_on='2020-01-01' WHERE symbol='OLD'"
            )
        assert store.active_candidates(pool_type="yearline_pullback") == []
        assert [row["symbol"] for row in store.active_candidates(pool_type="macd_zero_axis")] == ["KEEP"]
        expired = store.list_expired_candidates(pool_type="yearline_pullback")
        assert [row["symbol"] for row in expired] == ["OLD"]
        assert store.expired_candidate_count(pool_type="macd_zero_axis") == 0


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
class _FakeYearlineMarket:
    def __init__(self, frames: dict[str, pd.DataFrame], stock_list: pd.DataFrame | None = None):
        self.frames = {s.replace(".SH", "").replace(".SZ", ""): f for s, f in frames.items()}
        self.stock_list = stock_list
        self.expected = date(2026, 8, 25)

    def get_bars(self, symbol, timeframe="1d", limit=300):
        key = symbol.replace(".SH", "").replace(".SZ", "")
        frame = self.frames.get(key)
        if frame is None:
            raise RuntimeError("no data")
        return frame

    def get_stock_list(self):
        if self.stock_list is None:
            raise RuntimeError("no list")
        return self.stock_list

    def latest_expected_trade_date(self):
        return self.expected


class TestScanYearline:
    def _monitor(
        self,
        directory: Path,
        watchlist: list[str],
        max_scan_symbols: int = 500,
        universe_mode: str = "watchlist",
    ):
        config = {
            "market_data": {"cache_dir": str(directory / "cache")},
            "runtime": {
                "database_path": str(directory / "signals.db"),
                "output_dir": str(directory / "output"),
            },
            "monitor": {
                "watchlist": watchlist,
                "max_scan_symbols_per_run": max_scan_symbols,
                "candidate_limit": 100,
                "candidate_ttl_business_days": 5,
            },
            "scan": {"universe_mode": universe_mode},
        }
        monitor = SignalMonitor(config)
        monitor.dispatch_outbox = mock.MagicMock(return_value={"delivered": 0, "failed": 0})
        return monitor

    def test_watchlist_scan_writes_yearline_pool_only(self, tmp_path):
        frame = _signal_frame()
        monitor = self._monitor(tmp_path, watchlist=["600036"])
        monitor.market.get_stock_list = mock.MagicMock(
            return_value=pd.DataFrame([{"code": "600036", "name": "招商银行"}])
        )
        monitor._yearline_market = _FakeYearlineMarket({"600036": frame})

        report = monitor.scan_yearline(notify=False)

        assert report["pool_type"] == "yearline_pullback"
        assert report["research_only"] is True
        assert report["completed_round"] is True
        assert report["candidate_count"] == 1
        candidate = report["candidates"][0]
        assert candidate["symbol"] == "600036"
        assert candidate["signal_type"] == "yearline_pullback"
        assert candidate["research_only"] is True
        assert candidate["stop_suggestion_pct"] is not None
        # 写入的候选属于 yearline 池, 默认读(生产行为)看不到
        assert monitor.store.active_candidates(pool_type="yearline_pullback")
        assert monitor.store.active_candidates() == []
        monitor.dispatch_outbox.assert_not_called()
        # 报告文件落盘
        assert report["output_file"] and Path(report["output_file"]).exists()

    def test_all_a_batches_until_complete_then_syncs(self, tmp_path):
        frame = _signal_frame()
        stock_list = pd.DataFrame(
            [{"code": "600036", "name": "招商银行"},
             {"code": "000001", "name": "平安银行"},
             {"code": "300750", "name": "宁德时代"}]
        )
        monitor = self._monitor(
            tmp_path, watchlist=[], max_scan_symbols=2, universe_mode="all_a"
        )
        monitor.market.get_stock_list = mock.MagicMock(return_value=stock_list)
        monitor._yearline_market = _FakeYearlineMarket(
            {"600036": frame, "000001": frame, "300750": frame}, stock_list
        )

        first = monitor.scan_yearline(notify=False)
        assert first["coverage"] < 1.0
        assert first["completed_round"] is False

        second = monitor.scan_yearline(notify=False)
        assert second["coverage"] == 1.0
        assert second["completed_round"] is True
        # 整轮完成: 3 只全部入 yearline 池
        assert len(monitor.store.active_candidates(pool_type="yearline_pullback")) == 3

    def test_yearline_candidates_never_enter_monitoring(self, tmp_path):
        frame = _signal_frame()
        monitor = self._monitor(tmp_path, watchlist=[])
        monitor.market.get_stock_list = mock.MagicMock(
            return_value=pd.DataFrame([{"code": "600036", "name": "招商银行"}])
        )
        monitor._yearline_market = _FakeYearlineMarket({"600036": frame})
        monitor.scan_yearline(notify=False)

        # 年线候选存在但监控范围不含它
        symbols, _ = monitor.monitoring_symbols()
        assert "600036" not in symbols

        # MACD 池候选正常进入监控
        monitor.store.upsert_candidates(
            [{"symbol": "000001", "name": "平安银行", "score": 90}],
            pool_type="macd_zero_axis",
        )
        symbols, _ = monitor.monitoring_symbols()
        assert "000001" in symbols
        assert "600036" not in symbols

    def test_stale_daily_defers_symbol_and_keeps_upserting(self, tmp_path):
        frame = _signal_frame()
        stock_list = pd.DataFrame(
            [{"code": "600036", "name": "招商银行"}, {"code": "000001", "name": "平安银行"}]
        )
        stale_frame = frame.copy()
        stale_frame.loc[stale_frame.index[-1], "datetime"] = pd.Timestamp("2026-08-20")
        monitor = self._monitor(
            tmp_path, watchlist=[], max_scan_symbols=1, universe_mode="all_a"
        )
        monitor.market.get_stock_list = mock.MagicMock(return_value=stock_list)
        monitor._yearline_market = _FakeYearlineMarket(
            {"600036": stale_frame, "000001": frame}, stock_list
        )

        first = monitor.scan_yearline(notify=False)
        assert first["completed_round"] is False  # 600036 延迟, 未覆盖全
        second = monitor.scan_yearline(notify=False)
        assert second["completed_round"] is True
        assert second["candidate_count"] == 1  # 只有 000001 命中
        assert [row["symbol"] for row in monitor.store.active_candidates(pool_type="yearline_pullback")] == ["000001"]

    def test_retryable_data_error_does_not_complete_or_sync(self, tmp_path):
        frame = _signal_frame()
        bad_frame = frame.drop(columns=["volume"])
        stock_list = pd.DataFrame(
            [{"code": "600036", "name": "招商银行"}, {"code": "000001", "name": "平安银行"}]
        )
        monitor = self._monitor(
            tmp_path, watchlist=[], max_scan_symbols=2, universe_mode="all_a"
        )
        monitor.market.get_stock_list = mock.MagicMock(return_value=stock_list)
        monitor._yearline_market = _FakeYearlineMarket(
            {"600036": bad_frame, "000001": frame}, stock_list
        )

        report = monitor.scan_yearline(notify=False)

        assert report["completed_round"] is False
        assert report["retryable_error_count"] == 1
        assert report["stale_symbol_count"] == 0

    def test_new_trade_day_resets_previous_partial_round(self, tmp_path):
        frame = _signal_frame()
        stock_list = pd.DataFrame(
            [
                {"code": "600036", "name": "招商银行"},
                {"code": "000001", "name": "平安银行"},
                {"code": "300750", "name": "宁德时代"},
            ]
        )
        monitor = self._monitor(
            tmp_path, watchlist=[], max_scan_symbols=1, universe_mode="all_a"
        )
        monitor.market.get_stock_list = mock.MagicMock(return_value=stock_list)
        market = _FakeYearlineMarket(
            {"600036": frame, "000001": frame, "300750": frame}, stock_list
        )
        monitor._yearline_market = market

        first = monitor.scan_yearline(notify=False)
        assert first["batch_start"] == 0
        assert first["completed_round"] is False
        assert monitor.store.get_state("yearline_bootstrap_success")

        market.expected = date(2026, 8, 26)
        second = monitor.scan_yearline(notify=False)
        assert second["batch_start"] == 0
        assert monitor.store.get_state("yearline_bootstrap_success") == "[]"


# --------------------------------------------------------------------------- #
# 桥接
# --------------------------------------------------------------------------- #
class _FakeStore:
    def __init__(self, candidates=None):
        self.candidates = candidates or []
        self.expired = []

    def active_candidates(self, limit=100, pool_type="macd_zero_axis"):
        if pool_type in (None, "all"):
            return self.candidates
        return [c for c in self.candidates if c.get("pool_type") == pool_type]

    def list_expired_candidates(self, limit=200, pool_type=None):
        return self.expired

    def expired_candidate_count(self, pool_type=None):
        return len(self.expired)


class _FakeMonitor:
    def __init__(self, store=None, scanned=None):
        self.store = store or _FakeStore()
        self.candidate_limit = 100
        self.candidate_ttl = 5
        self.scanned = scanned

    def scan_yearline(self, notify=True):
        return {"mode": "scan", "pool_type": POOL_TYPE_YEARLINE, "notify": notify, "ok": True}

    def scan_zero_axis(self, notify=True):
        return {"mode": "scan", "pool_type": "macd_zero_axis", "notify": notify, "ok": True}


class TestBridgePoolType:
    def _bridge_cmd(self, command, payload, monitor):
        with mock.patch.object(web_bridge, "_make_monitor", return_value=monitor):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = web_bridge.COMMANDS[command](web_bridge._default_config_path(), payload)
        return code, buffer.getvalue()

    def test_candidates_defaults_to_macd_pool(self):
        store = _FakeStore([
            {"symbol": "000001", "pool_type": "macd_zero_axis"},
            {"symbol": "600036", "pool_type": "yearline_pullback"},
        ])
        code, output = self._bridge_cmd("candidates", {}, _FakeMonitor(store))
        assert code == 0
        data = json.loads(output)["data"]
        assert data["pool_type"] == "macd_zero_axis"
        assert [row["symbol"] for row in data["candidates"]] == ["000001"]

    def test_candidates_yearline_and_all(self):
        store = _FakeStore([
            {"symbol": "000001", "pool_type": "macd_zero_axis"},
            {"symbol": "600036", "pool_type": "yearline_pullback"},
        ])
        code, output = self._bridge_cmd(
            "candidates", {"pool_type": "yearline_pullback"}, _FakeMonitor(store)
        )
        data = json.loads(output)["data"]
        assert [row["symbol"] for row in data["candidates"]] == ["600036"]

        code, output = self._bridge_cmd("candidates", {"pool_type": "all"}, _FakeMonitor(store))
        data = json.loads(output)["data"]
        assert sorted(row["symbol"] for row in data["candidates"]) == ["000001", "600036"]

    def test_candidates_rejects_unknown_pool_type(self):
        code, output = self._bridge_cmd(
            "candidates", {"pool_type": "bogus"}, _FakeMonitor()
        )
        assert code == 2
        assert "bogus" in output

    def test_scan_dispatches_yearline_kind(self):
        monitor = _FakeMonitor()
        code, output = self._bridge_cmd(
            "scan", {"scan_kind": "yearline_pullback", "notify": False}, monitor
        )
        assert code == 0
        report = json.loads(output)["data"]["report"]
        assert report["pool_type"] == POOL_TYPE_YEARLINE
        assert report["notify"] is False

    def test_scan_defaults_to_macd(self):
        monitor = _FakeMonitor()
        code, output = self._bridge_cmd("scan", {"notify": True}, monitor)
        assert code == 0
        report = json.loads(output)["data"]["report"]
        assert report["pool_type"] == "macd_zero_axis"

    def test_scan_rejects_unknown_scan_kind(self):
        code, output = self._bridge_cmd("scan", {"scan_kind": "bogus"}, _FakeMonitor())
        assert code == 2
