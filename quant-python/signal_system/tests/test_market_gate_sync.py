from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_winrate import build_market_gate  # noqa: E402
from monitor.service import (  # noqa: E402
    MARKET_GATE_HISTORY_LIMIT,
    SignalMonitor,
    _fast_gate_latch_state,
)
from strategy.gate_observability import (  # noqa: E402
    MARKET_GATE_BLOCKER_ORDER,
    classify_market_gate_blockers,
    summarize_market_gate_history,
)
from strategy.market_gate import (  # noqa: E402
    calculate_strict_regime,
    calculate_trend_gate,
    resolve_market_gate_settings,
)


def index_bars(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": pd.bdate_range("2024-01-02", periods=len(closes)),
            "open": closes,
            "high": [value + 0.2 for value in closes],
            "low": [value - 0.2 for value in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
            "is_closed": [True] * len(closes),
        }
    )


class MarketGateConfigTests(unittest.TestCase):
    def test_resolves_effective_gate_settings(self):
        settings = resolve_market_gate_settings(
            {
                "entry_filters": {
                    "trend_gate_enabled": True,
                    "trend_fast_ma": 8,
                    "trend_slow_ma": 5,
                    "fast_gate_mode": "ANY_LATCH",
                },
                "signal_strategy": {
                    "macd": {"fast": 5, "slow": 13, "signal": 4}
                },
            }
        )

        self.assertTrue(settings["trend_gate_enabled"])
        self.assertEqual(8, settings["trend_fast_ma"])
        self.assertEqual(9, settings["trend_slow_ma"])
        self.assertEqual("any_latch", settings["fast_gate_mode"])
        self.assertEqual(
            {"fast": 5, "slow": 13, "signal": 4},
            settings["macd"],
        )

    def test_invalid_fast_gate_mode_falls_back_to_none(self):
        settings = resolve_market_gate_settings(
            {"entry_filters": {"fast_gate_mode": "unexpected"}}
        )
        self.assertEqual("none", settings["fast_gate_mode"])

    def test_non_default_trend_periods_are_calculated_from_closed_bars(self):
        rising = pd.Series([10.0, 10.0, 10.0, 10.0, 11.0, 12.0, 13.0])
        falling = pd.Series([13.0, 12.0, 11.0, 10.0, 10.0, 10.0, 10.0])

        self.assertTrue(bool(calculate_trend_gate(rising, True, 3, 6).iloc[-1]))
        self.assertFalse(bool(calculate_trend_gate(falling, True, 3, 6).iloc[-1]))

    def test_strict_regime_is_available_when_fast_latch_is_disabled(self):
        rising = pd.Series([100.0] * 10 + [101.0] * 10 + [102.0] * 5)
        regimes = calculate_strict_regime(rising)
        self.assertEqual("bull", regimes.iloc[-1])

        frame = index_bars(rising.tolist())
        gate = build_market_gate(
            frame,
            {
                "entry_filters": {
                    "trend_gate_enabled": False,
                    "fast_gate_mode": "none",
                },
                "signal_strategy": {
                    "macd": {"long_ma_period": 2},
                },
                "backtest": {"market_gate_slope_bars": 1},
            },
        )
        last_day = frame.iloc[-1]["datetime"].date().isoformat()
        self.assertEqual("bull", gate[last_day]["regime"])


class FastGateReplayTests(unittest.TestCase):
    def test_ma10_latch_sets_holds_and_recovers(self):
        falling = index_bars([100.0] * 10 + [99.0, 98.0, 97.0])
        self.assertTrue(
            _fast_gate_latch_state(falling, "ma10_latch")["ma10_latch_bear"]
        )

        mixed = index_bars([100.0] * 10 + [99.0, 98.0, 97.0, 98.0])
        self.assertTrue(
            _fast_gate_latch_state(mixed, "ma10_latch")["ma10_latch_bear"]
        )

        recovered = index_bars(
            [100.0] * 10 + [99.0, 98.0, 97.0, 98.0, 99.0, 100.0, 101.0]
        )
        self.assertFalse(
            _fast_gate_latch_state(recovered, "ma10_latch")["ma10_latch_bear"]
        )

    def test_macd_latch_uses_configured_periods(self):
        frame = index_bars([100.0] * 12)
        dif = pd.Series([1.0] * 11 + [-1.0])
        dea = pd.Series([0.0] * 12)
        fake_macd = pd.DataFrame(
            {"dif": dif, "dea": dea, "hist": (dif - dea) * 2.0}
        )

        with patch("monitor.service.calculate_macd", return_value=fake_macd) as mocked:
            state = _fast_gate_latch_state(
                frame,
                "macd_death_latch",
                macd_fast=5,
                macd_slow=13,
                macd_signal=4,
            )

        mocked.assert_called_once()
        _, kwargs = mocked.call_args
        self.assertEqual(
            {"fast": 5, "slow": 13, "signal": 4},
            kwargs,
        )
        self.assertTrue(state["macd_latch_bear"])

    def test_live_replay_matches_backtest_latches(self):
        closes = (
            [100.0 + index * 0.15 for index in range(80)]
            + [112.0 - index * 0.3 for index in range(35)]
            + [101.5 + index * 0.25 for index in range(45)]
        )
        frame = index_bars(closes)
        for mode in ("ma10_latch", "macd_death_latch", "any_latch"):
            with self.subTest(mode=mode):
                config = {
                    "entry_filters": {
                        "trend_gate_enabled": True,
                        "trend_fast_ma": 8,
                        "trend_slow_ma": 21,
                        "fast_gate_mode": mode,
                    },
                    "signal_strategy": {
                        "macd": {
                            "fast": 5,
                            "slow": 13,
                            "signal": 4,
                            "long_ma_period": 30,
                        }
                    },
                    "backtest": {"market_gate_slope_bars": 5},
                }
                gate = build_market_gate(frame, config)
                for index in range(35, len(frame)):
                    day = frame.iloc[index]["datetime"].date().isoformat()
                    live = _fast_gate_latch_state(
                        frame.iloc[: index + 1],
                        mode,
                        macd_fast=5,
                        macd_slow=13,
                        macd_signal=4,
                    )
                    self.assertEqual(
                        gate[day]["ma10_latch_bear"],
                        live["ma10_latch_bear"],
                        day,
                    )
                    self.assertEqual(
                        gate[day]["macd_latch_bear"],
                        live["macd_latch_bear"],
                        day,
                    )


class LiveMarketContextTests(unittest.TestCase):
    def test_chan_sell_context_does_not_become_a_live_only_death_cross_gate(self):
        frame = index_bars([100.0 + index * 0.1 for index in range(300)])
        monitor = SignalMonitor.__new__(SignalMonitor)
        monitor.config = {
            "entry_filters": {
                "market_gate_enabled": True,
                "market_gate_fail_open": False,
                "market_index_code": "000001.SH",
                "trend_gate_enabled": True,
                "trend_fast_ma": 20,
                "trend_slow_ma": 60,
                "fast_gate_mode": "none",
            },
            "signal_strategy": {
                "macd": {"fast": 12, "slow": 26, "signal": 9}
            },
        }
        monitor.market = MagicMock()
        monitor.market.get_index_bars.return_value = frame
        monitor.analyzer = MagicMock()
        monitor.analyzer.analyze.return_value = {
            "timeframes": {
                "1d": {
                    "status": "ok",
                    "indicators": {
                        "ma_long": 110.0,
                        "death_cross": False,
                        "ma_long_down": False,
                        "above_ma_long": True,
                        "ma_long_up": True,
                    },
                    "chan": {
                        "fresh_signals": [
                            {"side": "sell", "signal_type": "sell_1"}
                        ]
                    },
                }
            }
        }

        context = monitor._market_entry_context()

        self.assertTrue(context["allows_entries"])
        self.assertFalse(context["death_cross"])
        self.assertTrue(context["bearish_structure"])


class GateParityTests(unittest.TestCase):
    """闸门（blocked_by）与 regime（by_regime 用）是两层，不可互相反推。

    回归：曾经 monitor 把 blocker 折叠成 regime="bear" 再反推 allows_entries，
    导致 strict_regime == bear 被重复计入闸门 —— 在"长期趋势完好但短期回调"
    的日子（close > MA250、MA250 上行、MA20 > MA60，但 close < MA20 且 MA20 下行）
    多拦一批开仓，与回测口径分歧 28/2103 天。
    """

    def test_live_gate_does_not_double_count_strict_regime(self):
        # 长期缓涨 + 近期急跌 15 根：
        #   close < MA20 且 MA20 下行 → strict_regime == "bear"
        #   但 close > MA250、MA250 上行、MA20 > MA60 → 五个 blocker 全清
        # 需 >=250 根才能算出 MA250，否则会命中 insufficient_history。
        closes = [100.0 + 0.8 * index for index in range(280)]
        closes += [closes[-1] - 2.0 * step for step in range(1, 16)]
        frame = index_bars(closes)

        config = {
            "entry_filters": {
                "market_gate_enabled": True,
                "market_gate_fail_open": False,
                "trend_gate_enabled": True,
                "trend_fast_ma": 20,
                "trend_slow_ma": 60,
                "fast_gate_mode": "none",
            },
            "signal_strategy": {"macd": {"fast": 12, "slow": 26, "signal": 9}},
            "backtest": {"market_gate_slope_bars": 5},
        }

        gate = build_market_gate(frame, config)
        last = frame.iloc[-1]["datetime"].date().isoformat()
        backtest_allows = gate[last]["allows_entries"]
        backtest_blocked_by = gate[last]["blocked_by"]
        strict_regime = gate[last]["regime"]

        # 前置：这一天的确处于 strict_regime == bear 且 blocker 全清
        self.assertEqual([], backtest_blocked_by)
        self.assertEqual("bear", strict_regime)
        self.assertTrue(backtest_allows)

        monitor = SignalMonitor.__new__(SignalMonitor)
        monitor.config = config
        monitor.market = MagicMock()
        monitor.market.get_index_bars.return_value = frame
        monitor.analyzer = MagicMock()
        monitor.analyzer.analyze.return_value = {
            "timeframes": {
                "1d": {
                    "status": "ok",
                    "indicators": {
                        "ma_long": float(
                            frame["close"].rolling(250, min_periods=1).mean().iloc[-1]
                        ),
                        "death_cross": False,
                        "ma_long_down": False,
                        "above_ma_long": True,
                        "ma_long_up": True,
                    },
                    "chan": {"fresh_signals": []},
                }
            }
        }

        context = monitor._market_entry_context()

        # 生产必须与回测一致：blocker 全清即放行，regime 仍如实报告 bear
        self.assertTrue(context["allows_entries"])
        self.assertFalse(context["blocked"])
        self.assertEqual("bear", context["regime"])
        self.assertEqual([], context["blocked_by"])


def _gate_config(**overrides) -> dict:
    config = {
        "entry_filters": {
            "market_gate_enabled": True,
            "market_gate_fail_open": False,
            "market_index_code": "000001.SH",
            "trend_gate_enabled": True,
            "trend_fast_ma": 20,
            "trend_slow_ma": 60,
            "fast_gate_mode": "ma10_latch",
        },
        "signal_strategy": {
            "macd": {"fast": 12, "slow": 26, "signal": 9, "long_ma_period": 250}
        },
        "backtest": {"market_gate_slope_bars": 5},
    }
    config["entry_filters"].update(overrides)
    return config


class BlockerSplitTests(unittest.TestCase):
    """blocked_by 的六个名字必须各自绑到自己的条件上（拆分错了就是错误的可观测性）。"""

    OPEN = {
        "above_ma_long": True,
        "ma_long_down": False,
        "death_cross": False,
        "trend_up": True,
        "ma10_latch_bear": False,
        "macd_latch_bear": False,
    }

    def test_gate_open_has_no_blockers(self):
        self.assertEqual([], classify_market_gate_blockers(**self.OPEN))

    def test_each_condition_fires_exactly_its_own_name(self):
        cases = (
            ("above_ma_long", False, "below_ma_long"),
            ("ma_long_down", True, "ma_long_down"),
            ("death_cross", True, "macd_death_cross"),
            ("trend_up", False, "trend_down"),
            ("ma10_latch_bear", True, "ma10_latch_bear"),
            ("macd_latch_bear", True, "macd_latch_bear"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                self.assertEqual(
                    [expected],
                    classify_market_gate_blockers(**{**self.OPEN, field: value}),
                )

    def test_multiple_conditions_are_all_reported_in_stable_order(self):
        fired = classify_market_gate_blockers(
            **{
                **self.OPEN,
                "above_ma_long": False,
                "death_cross": True,
                "ma10_latch_bear": True,
            }
        )
        self.assertEqual(["below_ma_long", "macd_death_cross", "ma10_latch_bear"], fired)
        self.assertEqual(
            ["below_ma_long", "macd_death_cross", "ma10_latch_bear"],
            [name for name in MARKET_GATE_BLOCKER_ORDER if name in fired],
        )


class LiveBlockerParityTests(unittest.TestCase):
    """逐日对拍：生产的 blocked_by/allows_entries 必须与回测口径逐字一致。"""

    def _frame(self) -> pd.DataFrame:
        # 缓涨 → 急跌（跌破 MA250 / 趋势反转 / MA10 latch）→ 反弹修复，
        # 覆盖闸门开、单条件、多条件同时成立的日型。
        closes = [100.0 + index * 0.5 for index in range(300)]
        closes += [closes[-1] - 3.0 * step for step in range(1, 30)]
        closes += [closes[-1] + 1.2 * step for step in range(1, 70)]
        return index_bars(closes)

    def test_live_blocked_by_matches_backtest_every_day(self):
        frame = self._frame()
        config = _gate_config()
        backtest = build_market_gate(frame, config)
        monitor = SignalMonitor.__new__(SignalMonitor)
        monitor.config = config
        monitor.market = MagicMock()
        monitor.analyzer = MagicMock()
        from strategy.multi_timeframe import MultiTimeframeAnalyzer

        monitor.analyzer = MultiTimeframeAnalyzer(config)

        compared = 0
        observed_states = set()
        for index in range(260, len(frame)):
            day = frame.iloc[index]["datetime"].date().isoformat()
            monitor.market.get_index_bars.return_value = (
                frame.iloc[: index + 1].tail(300).reset_index(drop=True)
            )
            context = monitor._market_entry_context()
            expected = backtest[day]
            with self.subTest(day=day):
                self.assertEqual(
                    expected["allows_entries"], context["allows_entries"], day
                )
                self.assertEqual(expected["blocked_by"], context["blocked_by"], day)
                self.assertEqual(expected["regime"], context["regime"], day)
                # 判定与观测同源：blocked 只能等于"名单非空"。
                self.assertEqual(context["blocked"], bool(context["blocked_by"]), day)
                self.assertEqual(
                    context["allows_entries"], not bool(context["blocked_by"]), day
                )
            observed_states.add((context["blocked"], tuple(context["blocked_by"])))
            compared += 1
        self.assertGreater(compared, 100)
        # 对拍必须真的覆盖到"有 blocker"的日子，否则等于只测了恒真。
        self.assertTrue(any(blocked for blocked, _ in observed_states))
        self.assertTrue(any(not blocked for blocked, _ in observed_states))
        self.assertTrue(
            any(set(names) - {"below_ma_long"} for _, names in observed_states),
            f"sample never exercised a non-price blocker: {sorted(observed_states)[:5]}",
        )

    def test_fail_closed_path_reports_gate_unavailable(self):
        monitor = SignalMonitor.__new__(SignalMonitor)
        monitor.config = _gate_config()
        monitor.market = MagicMock()
        monitor.market.get_index_bars.side_effect = RuntimeError("指数日线获取失败")
        monitor.analyzer = MagicMock()

        context = monitor._market_entry_context()

        self.assertFalse(context["allows_entries"])
        self.assertTrue(context["blocked"])
        self.assertEqual(["gate_unavailable"], context["blocked_by"])
        self.assertEqual("unknown", context["regime"])

    def test_disabled_gate_is_not_recorded_as_open(self):
        monitor = SignalMonitor.__new__(SignalMonitor)
        monitor.config = _gate_config(market_gate_enabled=False)
        monitor.market = MagicMock()
        monitor.analyzer = MagicMock()

        context = monitor._market_entry_context()

        self.assertFalse(context["enabled"])
        self.assertEqual([], context["blocked_by"])
        self.assertFalse(context["blocked"])


class _FakeStateStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get_state(self, key: str, default: str | None = None) -> str | None:
        return self.data.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.data[key] = value


def _observation(day: str, blocked_by, allows: bool) -> dict:
    return {
        "date": day,
        "allows_entries": allows,
        "blocked": not allows,
        "blocked_by": list(blocked_by),
    }


class MarketGateHistoryTests(unittest.TestCase):
    def test_summary_counts_hits_over_days_not_symbols(self):
        observations = [
            _observation("2026-01-05", [], True),
            _observation("2026-01-06", ["ma_long_down", "below_ma_long"], False),
            _observation("2026-01-07", ["ma_long_down"], False),
            _observation("2026-01-08", ["ma_long_down", "trend_down"], False),
        ]

        summary = summarize_market_gate_history(observations, window_days=240)

        self.assertEqual(4, summary["days_recorded"])
        self.assertEqual(1, summary["allowed_days"])
        self.assertEqual(3, summary["blocked_days"])
        self.assertEqual(0.25, summary["allowed_ratio"])
        self.assertEqual("2026-01-05", summary["window_start"])
        self.assertEqual("2026-01-08", summary["window_end"])
        self.assertEqual("3/4", summary["hits"]["ma_long_down"]["display"])
        self.assertEqual("1/4", summary["hits"]["below_ma_long"]["display"])
        self.assertEqual("0/4", summary["hits"]["macd_death_cross"]["display"])
        self.assertEqual(
            [
                {"date": "2026-01-05", "blocked_by": []},
                {"date": "2026-01-06", "blocked_by": ["ma_long_down", "below_ma_long"]},
                {"date": "2026-01-07", "blocked_by": ["ma_long_down"]},
                {"date": "2026-01-08", "blocked_by": ["ma_long_down", "trend_down"]},
            ],
            summary["blocked_by_daily"],
        )

    def test_summary_keeps_only_the_requested_window(self):
        observations = [
            _observation(f"2026-01-{day:02d}", ["trend_down"], False) for day in range(1, 11)
        ]

        summary = summarize_market_gate_history(observations, window_days=3)

        self.assertEqual(3, summary["days_recorded"])
        self.assertEqual("2026-01-08", summary["window_start"])
        self.assertEqual("3/3", summary["hits"]["trend_down"]["display"])

    def test_summary_reports_unknown_blockers_separately(self):
        summary = summarize_market_gate_history(
            [_observation("2026-01-05", ["gate_unavailable"], False)], window_days=240
        )

        self.assertEqual(0, summary["hits"]["below_ma_long"]["days"])
        self.assertEqual({"gate_unavailable": 1}, summary["other_blockers"])

    def test_history_is_one_record_per_day_and_capped(self):
        monitor = SignalMonitor.__new__(SignalMonitor)
        monitor.store = _FakeStateStore()
        context = {
            "enabled": True,
            "allows_entries": False,
            "blocked": True,
            "blocked_by": ["ma_long_down"],
            "regime": "bear",
            "index_last_day": "2026-01-05",
        }

        monitor._record_market_gate_history(context)
        monitor._record_market_gate_history(context)
        self.assertEqual(1, len(monitor._load_market_gate_history()))

        # 同日重跑用新判定覆盖旧判定，而不是各记一条。
        monitor._record_market_gate_history({**context, "allows_entries": True, "blocked": False, "blocked_by": []})
        history = monitor._load_market_gate_history()
        self.assertEqual(1, len(history))
        self.assertTrue(history[0]["allows_entries"])
        self.assertEqual([], history[0]["blocked_by"])

        # 历史按日期有序，且超过上限时保留的是最近的 N 天（不是最早的 N 天）。
        for offset in range(MARKET_GATE_HISTORY_LIMIT + 5):
            monitor._record_market_gate_history(
                {**context, "index_last_day": f"2026-{offset // 28 + 1:02d}-{offset % 28 + 1:02d}"}
            )
        history = monitor._load_market_gate_history()
        self.assertEqual(MARKET_GATE_HISTORY_LIMIT, len(history))
        dates = [row["date"] for row in history]
        self.assertEqual(sorted(dates), dates)
        # 截断保留最近 N 天：最早那批日期必须已经被丢掉。
        self.assertNotIn("2026-01-05", dates)
        self.assertEqual(f"2026-{offset // 28 + 1:02d}-{offset % 28 + 1:02d}", dates[-1])

    def test_disabled_gate_is_not_written_to_history(self):
        monitor = SignalMonitor.__new__(SignalMonitor)
        monitor.store = _FakeStateStore()

        monitor._record_market_gate_history({"enabled": False, "allows_entries": True})

        self.assertEqual([], monitor._load_market_gate_history())

    def test_daily_signal_counts_accumulate_across_batches(self):
        monitor = SignalMonitor.__new__(SignalMonitor)
        monitor.store = _FakeStateStore()
        batch = {
            "signals_screened": 3,
            "blocked_by_market_gate": 3,
            "blocked_by_position_gate": 1,
            "policy_observe_only": 0,
            "candidates_published": 2,
            "batches": 1,
        }

        first = monitor._accumulate_market_gate_signals("2026-01-05", batch, False)
        second = monitor._accumulate_market_gate_signals("2026-01-05", batch, False)

        self.assertEqual(3, first["blocked_by_market_gate"])
        self.assertEqual(6, second["blocked_by_market_gate"])
        self.assertEqual(6, second["signals_screened"])
        self.assertEqual(2, second["batches"])

        # 完成轮：日报读到的是当日合计，读完即清桶，避免明天重复计入。
        monitor._accumulate_market_gate_signals("2026-01-06", batch, True)
        fresh = monitor._accumulate_market_gate_signals("2026-01-06", batch, True)
        self.assertEqual(3, fresh["blocked_by_market_gate"])
        self.assertEqual(1, fresh["batches"])

    def test_summary_lines_show_gate_state_even_without_signals(self):
        closed = {
            "enabled": True,
            "today": {
                "date": "2026-01-05",
                "allows_entries": False,
                "active_blockers": ["ma_long_down", "below_ma_long"],
                "regime": "bear",
            },
            "days_recorded": 240,
            "allowed_days": 60,
            "window_start": "2025-01-02",
            "window_end": "2026-01-05",
            "hits": {"ma_long_down": {"days": 240, "display": "240/240"}},
        }

        lines = SignalMonitor._market_gate_summary_lines(closed, {})
        text = "\n".join(lines)
        self.assertIn("闸门：关", text)
        self.assertIn("ma_long_down", text)
        self.assertIn("放行 60/240", text)
        self.assertIn("ma_long_down:240/240", text)
        # 空场次（无信号）也要能看出"闸门开/关"。
        self.assertNotIn("因闸门被拦下的信号", text)

        opened = {**closed, "today": {**closed["today"], "allows_entries": True, "active_blockers": []}}
        self.assertIn("闸门：开", "\n".join(SignalMonitor._market_gate_summary_lines(opened, {})))

        blocked = SignalMonitor._market_gate_summary_lines(
            closed, {"signals_screened": 12, "blocked_by_market_gate": 12}
        )
        self.assertIn("因闸门被拦下的信号：12 只", "\n".join(blocked))

    def test_summary_lines_mark_a_disabled_gate_as_disabled(self):
        lines = SignalMonitor._market_gate_summary_lines(
            {"enabled": False, "today": {"date": "2026-01-05", "allows_entries": True}}, {}
        )
        self.assertIn("未启用", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
