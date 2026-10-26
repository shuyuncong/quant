"""Contract tests for the BaoStock historical daily adapter.

The fake SDK is only an external boundary: assertions are about the normalized
frame (columns, units, attrs, is_closed), the request parameters, and which
failure turns into which exception — not about forwarding calls.
"""

import os
import socket
import sys
import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from data.providers.baostock_provider import (
    BaostockDailyProvider,
    BaostockDataError,
    BaostockError,
    BaostockUnsupportedError,
)

STOCK_FIELDS = "date,code,open,high,low,close,volume,amount,adjustflag,turn,tradestatus"
INDEX_FIELDS = "date,code,open,high,low,close,volume,amount,adjustflag"
PAGE_SIZE = 2000
CN_TZ = timezone(timedelta(hours=8))


def stock_row(date="2020-01-02", open_="10.0", high="10.5", low="9.8", close="10.2",
              volume="12345", amount="123450.0", turn="0.34", status="1", code="sz.000001", adjustflag="2"):
    return [date, code, open_, high, low, close, volume, amount, adjustflag, turn, status]


def index_row(date="2015-01-05", open_="3200.0", high="3250.0", low="3180.0",
              close="3230.0", volume="123456789", amount="9876543210", code="sh.000001", adjustflag="3"):
    return [date, code, open_, high, low, close, volume, amount, adjustflag]


def paged_rows(counts, row_factory, start="2015-01-05"):
    """Build pages of distinct business days (dates are unique across pages)."""
    dates = pd.bdate_range(start, periods=sum(counts))
    rows = [row_factory(date=stamp.strftime("%Y-%m-%d")) for stamp in dates]
    pages, cursor = [], 0
    for count in counts:
        pages.append(rows[cursor:cursor + count])
        cursor += count
    return pages


class FakeResult:
    """Stand-in for ``baostock.data.resultset.ResultData`` (SDK semantics only)."""

    def __init__(self, fields, pages, *, error_code="0", error_msg="", pagination_error=None):
        self.fields = list(fields)
        self._pages = [[list(row) for row in page] for page in pages]
        self._page_index = 0
        self.cur_row_num = 0
        self.error_code = error_code
        self.error_msg = error_msg
        self.pagination_error = pagination_error
        self.cur_page_num = 1
        self.per_page_count = PAGE_SIZE
        self.data = self._pages[0] if self._pages else []

    def _fetch_next_page(self):
        next_index = self._page_index + 1
        if next_index >= len(self._pages):
            if self.pagination_error is not None:
                # Real SDK: send_msg swallows the socket error and next() returns
                # False while bumping error_code (or leaving it at "0" on I/O).
                self.error_code, self.error_msg = self.pagination_error
            return False
        self._page_index = next_index
        self.data = self._pages[next_index]
        self.cur_row_num = 0
        return bool(self.data)

    def next(self):
        if not self.data:
            return False
        if self.cur_row_num < len(self.data):
            return True
        if len(self.data) != PAGE_SIZE:  # partial page or single oversized response
            return False
        return self._fetch_next_page()

    def get_row_data(self):
        row = self.data[self.cur_row_num]
        self.cur_row_num += 1
        return list(row)


class FakeReply:
    def __init__(self, error_code="0", error_msg=""):
        self.error_code = error_code
        self.error_msg = error_msg


class FakeBaoStock:
    def __init__(self, result, *, login_code="0", login_msg=""):
        self.result = result
        self.login_code = login_code
        self.login_msg = login_msg
        self.queries = []
        self.logins = 0
        self.logouts = 0

    def login(self):
        self.logins += 1
        return FakeReply(self.login_code, self.login_msg)

    def logout(self):
        self.logouts += 1
        return FakeReply()

    def query_history_k_data_plus(self, code, fields, start_date=None, end_date=None,
                                  frequency="d", adjustflag="3"):
        self.queries.append(
            {
                "code": code,
                "fields": fields,
                "start_date": start_date,
                "end_date": end_date,
                "frequency": frequency,
                "adjustflag": adjustflag,
            }
        )
        # Successful SDK replies echo their query identity, independently of rows.
        # Tests can supply conflicting metadata explicitly to emulate a late reply.
        for name, value in {"code": code, "start_date": start_date, "end_date": end_date,
                            "frequency": frequency, "adjustflag": adjustflag}.items():
            if not hasattr(self.result, name):
                setattr(self.result, name, value)
        return self.result


class ExplodingBaoStock(FakeBaoStock):
    def query_history_k_data_plus(self, *args, **kwargs):
        raise OSError("connection reset by peer")


def make_provider(result, **kwargs):
    provider = BaostockDailyProvider(socket_timeout_seconds=5.0)
    provider._bs = FakeBaoStock(result, **kwargs)
    provider._logged_in = True
    return provider


class BaostockDailyProviderTests(unittest.TestCase):
    # ---------------------------------------------------------------- requests

    def test_requests_qfq_and_writes_truthful_attrs(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[
            stock_row(date="2020-01-03"),
            stock_row(date="2020-01-02"),
        ]])
        provider = make_provider(result)

        frame = provider.get_daily_data("000001.SZ", start_date="2020-01-01", end_date="2020-01-10")

        query = provider._bs.queries[0]
        self.assertEqual("sz.000001", query["code"])
        self.assertEqual(STOCK_FIELDS, query["fields"])
        self.assertEqual("2020-01-01", query["start_date"])
        self.assertEqual("2020-01-10", query["end_date"])
        self.assertEqual("d", query["frequency"])
        self.assertEqual("2", query["adjustflag"])

        self.assertEqual(
            ["trade_date", "datetime", "open", "high", "low", "close",
             "volume", "amount", "turnover_rate", "is_closed"],
            list(frame.columns),
        )
        self.assertEqual(["20200102", "20200103"], frame["trade_date"].tolist())
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(frame["datetime"]))
        self.assertEqual(
            {
                "source": "baostock",
                "adjust": "qfq",
                "timeframe": "1d",
                "timezone": "Asia/Shanghai",
                "timestamp_semantics": "end",
                "volume_unit": "shares",
                "amount_unit": "CNY",
                "turnover_rate_unit": "percent",
                "ts_code": "000001.SZ",
            },
            frame.attrs,
        )

    def test_suspended_days_do_not_become_fake_bars(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[
            stock_row(date="2020-01-02"),
            # BaoStock 停牌行：OHLC=前收、volume/amount=0、turn 为空
            stock_row(date="2020-01-03", open_="10.2", high="10.2", low="10.2",
                      close="10.2", volume="0", amount="0", turn="", status="0"),
            stock_row(date="2020-01-06", open_="10.4", high="10.9", low="10.3", close="10.7"),
        ]])
        frame = make_provider(result).get_daily_data("000001.SZ", "20200101", "20200110")

        self.assertEqual(["20200102", "20200106"], frame["trade_date"].tolist())
        self.assertGreater(len(frame), 0)
        self.assertTrue((frame["volume"] > 0).all())
        self.assertTrue(frame["is_closed"].all())

    def test_units_are_not_rescaled(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[
            stock_row(volume="12345", amount="1234500.0", turn="0.34"),
        ]])
        frame = make_provider(result).get_daily_data("000001.SZ", "20200101", "20200102")

        # 股，不是手（不除 100）；元，不是千元/万元；换手率是 0.34%，不是 0.0034
        self.assertEqual(12345.0, float(frame["volume"].iloc[0]))
        self.assertEqual(1234500.0, float(frame["amount"].iloc[0]))
        self.assertEqual(0.34, float(frame["turnover_rate"].iloc[0]))
        self.assertEqual("shares", frame.attrs["volume_unit"])
        self.assertEqual("CNY", frame.attrs["amount_unit"])
        self.assertEqual("percent", frame.attrs["turnover_rate_unit"])

    def test_blank_amount_and_turn_stay_missing(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[
            stock_row(amount="", turn=""),
        ]])
        frame = make_provider(result).get_daily_data("000001.SZ", "20200101", "20200102")

        self.assertTrue(pd.isna(frame["amount"].iloc[0]))
        self.assertTrue(pd.isna(frame["turnover_rate"].iloc[0]))
        self.assertEqual(12345.0, float(frame["volume"].iloc[0]))


    def test_a_late_reply_for_another_stock_is_rejected(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[stock_row()]])
        result.code = "sz.000002"
        provider = make_provider(result)
        with self.assertRaisesRegex(BaostockDataError, "响应串线"):
            provider.get_daily_data("000001.SZ", "20200101", "20200102")
        self.assertFalse(provider._logged_in)

    def test_a_wrong_adjustment_reply_cannot_be_relabeled_qfq(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[stock_row(adjustflag="3")]])
        with self.assertRaisesRegex(BaostockDataError, "adjustflag"):
            make_provider(result).get_daily_data("000001.SZ", "20200101", "20200102")

    def test_another_stocks_row_inside_a_valid_envelope_is_rejected(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[
            stock_row(), stock_row(date="2020-01-03", code="sz.000002"),
        ]])
        with self.assertRaisesRegex(BaostockDataError, "不同证券"):
            make_provider(result).get_daily_data("000001.SZ", "20200101", "20200103")

    def test_a_failed_query_closes_its_socket_before_the_next_stock(self):
        local, peer = socket.socketpair()
        self.addCleanup(peer.close)
        self.addCleanup(local.close)
        peer.settimeout(1)
        provider = make_provider(FakeResult(STOCK_FIELDS.split(","), [[]], error_code="10002007"))
        provider._session_socket = local
        with self.assertRaises(BaostockError):
            provider.get_daily_data("000001.SZ", "20200101", "20200102")
        self.assertEqual(b"", peer.recv(1))
        provider._bs.result = FakeResult(STOCK_FIELDS.split(","), [[stock_row(code="sz.000002")]])
        clean = provider.get_daily_data("000002.SZ", "20200101", "20200102")
        self.assertEqual("000002.SZ", clean.attrs["ts_code"])
        self.assertEqual([10.2], clean["close"].tolist())

    # -------------------------------------------------------------- validation

    def test_illegal_bars_are_rejected(self):
        cases = {
            "high_below_low": stock_row(high="9.0", low="9.5"),
            "close_above_high": stock_row(high="10.5", close="11.0"),
            "open_at_zero": stock_row(open_="0"),
            "nan_price": stock_row(low=""),
            "negative_volume": stock_row(volume="-1"),
            "missing_volume": stock_row(volume=""),
            "negative_amount": stock_row(amount="-1"),
            "negative_turnover": stock_row(turn="-0.1"),
        }
        for name, row in cases.items():
            with self.subTest(case=name):
                result = FakeResult(STOCK_FIELDS.split(","), [[row]])
                provider = make_provider(result)
                with self.assertRaises(BaostockDataError):
                    provider.get_daily_data("000001.SZ", "20200101", "20200102")

    def test_unknown_tradestatus_is_rejected(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[stock_row(status="2")]])
        with self.assertRaises(BaostockDataError):
            make_provider(result).get_daily_data("000001.SZ", "20200101", "20200102")

    # ------------------------------------------------------------- pagination

    def test_pagination_error_after_partial_result_raises(self):
        pages = paged_rows([PAGE_SIZE], stock_row)
        result = FakeResult(
            STOCK_FIELDS.split(","),
            pages,
            pagination_error=("10002008", "网络接收超时"),
        )
        provider = make_provider(result)

        with self.assertRaises(BaostockError) as caught:
            provider.get_daily_data("000001.SZ", "20150101", "20160101")
        self.assertIs(BaostockError, type(caught.exception))
        self.assertIn("10002008", str(caught.exception))

    def test_truncated_full_terminal_page_raises(self):
        # I/O failure leaves error_code at "0": only the full terminal page reveals it.
        pages = paged_rows([PAGE_SIZE], stock_row)
        result = FakeResult(STOCK_FIELDS.split(","), pages)
        provider = make_provider(result)

        with self.assertRaisesRegex(BaostockDataError, "截断"):
            provider.get_daily_data("000001.SZ", "20150101", "20160101")

    def test_complete_pagination_returns_every_row(self):
        pages = paged_rows([PAGE_SIZE, 3], stock_row)
        result = FakeResult(STOCK_FIELDS.split(","), pages)
        frame = make_provider(result).get_daily_data("000001.SZ", "20150101", "20160101")

        self.assertEqual(PAGE_SIZE + 3, len(frame))
        self.assertEqual(
            pd.Timestamp(pages[-1][-1][0]).strftime("%Y%m%d"),
            frame["trade_date"].iloc[-1],
        )
        self.assertTrue(frame["is_closed"].all())

    # ------------------------------------------------------- error vs no data

    def test_query_error_is_not_no_data(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[]], error_code="10004011",
                            error_msg="无效的证券代码")
        provider = make_provider(result)

        with self.assertRaises(BaostockError) as caught:
            provider.get_daily_data("000001.SZ", "20200101", "20200102")
        self.assertIn("10004011", str(caught.exception))

    def test_empty_result_is_empty_frame_with_contract(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[]])
        frame = make_provider(result).get_daily_data("000001.SZ", "20200101", "20200102")

        self.assertTrue(frame.empty)
        self.assertEqual(
            ["trade_date", "datetime", "open", "high", "low", "close",
             "volume", "amount", "turnover_rate", "is_closed"],
            list(frame.columns),
        )
        self.assertEqual("baostock", frame.attrs["source"])
        self.assertEqual("qfq", frame.attrs["adjust"])
        self.assertEqual("shares", frame.attrs["volume_unit"])

    def test_sdk_exception_is_wrapped_as_baostock_error(self):
        provider = BaostockDailyProvider(socket_timeout_seconds=5.0)
        fake = ExplodingBaoStock(FakeResult(STOCK_FIELDS.split(","), [[]]))
        provider._bs = fake
        provider._logged_in = True

        with self.assertRaisesRegex(BaostockError, "connection reset"):
            provider.get_daily_data("000001.SZ", "20200101", "20200102")

    def test_login_failure_raises_instead_of_empty(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[stock_row()]])
        provider = BaostockDailyProvider()
        provider._bs = FakeBaoStock(result, login_code="10002007", login_msg="网络接收错误")

        with self.assertRaises(BaostockError):
            provider.get_daily_data("000001.SZ", "20200101", "20200102")
        self.assertEqual([], provider._bs.queries)

    # ---------------------------------------------------------- symbol guards

    def test_beijing_exchange_is_rejected_explicitly(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[stock_row()]])
        provider = make_provider(result)

        with self.assertRaises(BaostockUnsupportedError):
            provider.get_daily_data("830799.BJ", "20200101", "20200102")
        self.assertEqual([], provider._bs.queries)

    # ------------------------------------------------------------------ index

    def test_index_uses_unadjusted_and_index_attrs(self):
        result = FakeResult(INDEX_FIELDS.split(","), [[
            index_row(date="2015-01-06"),
            index_row(date="2015-01-05"),
        ]])
        provider = make_provider(result)

        frame = provider.get_index_daily("000001.SH", start_date="20150101", end_date="20150110")

        query = provider._bs.queries[0]
        self.assertEqual("sh.000001", query["code"])
        self.assertEqual(INDEX_FIELDS, query["fields"])
        self.assertEqual("3", query["adjustflag"])
        self.assertEqual(["20150105", "20150106"], frame["trade_date"].tolist())
        self.assertNotIn("turnover_rate", frame.columns)
        self.assertIn("is_closed", frame.columns)
        self.assertEqual(123456789.0, float(frame["volume"].iloc[0]))
        self.assertEqual("baostock", frame.attrs["source"])
        self.assertEqual("none", frame.attrs["adjust"])
        self.assertEqual("1d", frame.attrs["timeframe"])
        self.assertEqual("Asia/Shanghai", frame.attrs["timezone"])
        self.assertEqual("end", frame.attrs["timestamp_semantics"])
        self.assertEqual("shares", frame.attrs["volume_unit"])
        self.assertEqual("000001.SH", frame.attrs["ts_code"])

    # -------------------------------------------------------------- freshness

    def test_today_and_future_bars_are_not_marked_closed(self):
        today = datetime.now(CN_TZ).date()
        yesterday = today - timedelta(days=1)
        result = FakeResult(STOCK_FIELDS.split(","), [[
            stock_row(date=yesterday.strftime("%Y-%m-%d")),
            stock_row(date=today.strftime("%Y-%m-%d")),
            stock_row(date=(today + timedelta(days=1)).strftime("%Y-%m-%d")),
        ]])
        frame = make_provider(result).get_daily_data("000001.SZ", "20200101", "20200102")

        self.assertEqual([True, False, False], frame["is_closed"].tolist())

    def test_long_history_has_no_gaps_when_large_responses_are_unavailable(self):
        dates = pd.bdate_range("2016-12-01", "2024-12-31")
        source_rows = [stock_row(date=day.strftime("%Y-%m-%d")) for day in dates]

        class BoundedServer(FakeBaoStock):
            def query_history_k_data_plus(self, code, fields, start_date=None, end_date=None,
                                          frequency="d", adjustflag="3"):
                if (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days > 729:
                    raise TimeoutError("large response unavailable")
                selected = [row for row in source_rows if start_date <= row[0] <= end_date]
                self.result = FakeResult(fields.split(","), [selected])
                return super().query_history_k_data_plus(
                    code, fields, start_date, end_date, frequency, adjustflag,
                )

        provider = BaostockDailyProvider()
        provider._bs = BoundedServer(None)
        provider._logged_in = True
        result = provider.get_daily_data("000001.SZ", "20161201", "20241231")
        self.assertEqual(dates.strftime("%Y%m%d").tolist(), result["trade_date"].tolist())
        self.assertEqual([0.34] * len(dates), result["turnover_rate"].tolist())

    # ------------------------------------------------------------------ close

    def test_close_is_idempotent(self):
        result = FakeResult(STOCK_FIELDS.split(","), [[stock_row()]])
        provider = make_provider(result)

        provider.close()
        provider.close()

        self.assertEqual(1, provider._bs.logouts)
        self.assertFalse(provider._logged_in)


if __name__ == "__main__":
    unittest.main()
