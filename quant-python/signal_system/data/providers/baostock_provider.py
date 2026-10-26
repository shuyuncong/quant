"""BaoStock-backed historical daily bars (research backfill only).

能力范围（刻意窄）：
- ``get_daily_data``：A 股个股日线，BaoStock ``adjustflag=2``（前复权）；
- ``get_index_daily``：指数日线，``adjustflag=3``（不复权）；
- ``get_trade_calendar``：交易日历。

**不是** ``BaseMarketDataProvider`` 的完整实现：没有股票列表、每日指标、财务数据、
分钟线或实时行情，也不写数据库、不发通知、不下单。调用方按 duck typing 使用
（``name`` / ``get_daily_data`` / ``get_index_daily`` / ``close``），不要把它当
通用的 Provider 到处传。

单位契约（BaoStock 字段定义的原值，本 provider 不做任何缩放）：
- ``volume``：股（BaoStock「成交量（累计 单位：股）」）。不除以 100 变"手"，
  也不声称与腾讯（通常以手为单位）一致。
- ``amount``：元（人民币）。
- ``turnover_rate``：百分数（``0.34`` 表示 0.34%），即 BaoStock ``turn`` 原值；
  指数没有该字段，因此指数结果不含此列。
- 停牌日（``tradestatus=0``）：BaoStock 仍会返回 OHLC=前收、``volume``/``amount``=0、
  ``turn`` 为空的行。这些**不是**真实交易 bar，provider 直接丢弃，因此返回序列中
  不存在"停牌假 bar"。
- 当日 bar：``is_closed=False``。BaoStock 盘中是否已刷新当日日线未经证实，保守地
  不把当日 bar 当作已收盘；历史 bar 恒为 ``True``。

错误语义（请求失败 ≠ no_data）：
- 登录/网络/服务端错误 → :class:`BaostockError`；
- 返回数据不可用（非法 OHLC、负成交量、未知 tradestatus、分页被截断）→
  :class:`BaostockDataError`；
- 不支持的证券（北交所等）→ :class:`BaostockUnsupportedError`。
只有"查询成功、区间内没有任何交易 bar"才返回空 DataFrame。

超时：BaoStock SDK 用 ``socket.socket()`` 建连且从不调用 ``settimeout``，所以查询
默认可以无限阻塞。本 provider 在登录成功后对 SDK 自己持有的那个 socket 设置
per-socket timeout（``socket_timeout_seconds``，默认 120s）—— 不改
``socket.setdefaulttimeout``、不另开探测/预连接、不重试。登录阶段的 DNS 解析与
connect 由 SDK 直接执行，本 provider 没有（也不假装有）对这部分的超时控制。

分页完整性：SDK 的 ``ResultData.next()`` 在翻页请求失败时（socket 超时被
``send_msg`` 吞掉）直接返回 ``False`` 且**不更新 ``error_code``**，服务端错误则会把
错误码写进 ``error_code``。因此这里 query 之后与迭代结束之后各核验一次
``error_code``，并额外用"最后一页是否恰好为满页"来识别被截断的分页 —— 宁可报错，
也绝不把截断的历史当成功保存。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..symbols import normalize_ts_code, split_ts_code

logger = logging.getLogger(__name__)

_TIMEZONE = "Asia/Shanghai"
# 这里只判断当前上海日期；当前采用 UTC+08:00，不依赖 Windows 的时区数据库。
_CN_TZ = timezone(timedelta(hours=8))
_TIMEFRAME = "1d"
_FREQUENCY = "d"

_STOCK_FIELDS = "date,code,open,high,low,close,volume,amount,adjustflag,turn,tradestatus"
_INDEX_FIELDS = "date,code,open,high,low,close,volume,amount,adjustflag"
_STOCK_ADJUST = "qfq"
_STOCK_ADJUSTFLAG = "2"
_INDEX_ADJUST = "none"
_INDEX_ADJUSTFLAG = "3"

_BAR_COLUMNS = (
    "trade_date",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "turnover_rate",
    "is_closed",
)
_INDEX_BAR_COLUMNS = tuple(column for column in _BAR_COLUMNS if column != "turnover_rate")
_COLUMN_DTYPES = {
    "trade_date": "object",
    "datetime": "datetime64[ns]",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
    "amount": "float64",
    "turnover_rate": "float64",
    "is_closed": "bool",
}

# SDK 未把 BAOSTOCK_PER_PAGE_COUNT 暴露到顶层，按常量文件取，取不到时用同值兜底。
_DEFAULT_PAGE_SIZE = 2000


class BaostockError(RuntimeError):
    """BaoStock 请求失败（登录/网络/服务端错误）——调用方应记为 error 而非 no_data。"""


class BaostockDataError(BaostockError):
    """BaoStock 返回了不可用的数据（非法 OHLC、负量、未知状态、分页截断）。"""


class BaostockUnsupportedError(BaostockError):
    """BaoStock 不支持的证券（例如北交所 ``*.BJ``）。"""


def _shanghai_today():
    """上海市场的"今天"（固定 +08:00，不引入 tzdata 依赖）。"""
    return datetime.now(_CN_TZ).date()


class BaostockDailyProvider:
    """Historical daily-bar adapter for BaoStock (duck-typed, not a full provider)."""

    name = "baostock"

    def __init__(self, config: dict | None = None, *, socket_timeout_seconds: float = 120.0):
        self.config = dict(config or {})
        if socket_timeout_seconds <= 0:
            raise ValueError("socket_timeout_seconds 必须为正数")
        self.socket_timeout_seconds = float(socket_timeout_seconds)
        self._bs = None
        self._logged_in = False
        self._session_socket = None

    # ------------------------------------------------------------------ login

    def _get_baostock(self):
        """Lazily import the optional SDK and keep one logged-in session.

        失败抛 :class:`BaostockError`；SDK 的 ``login`` 把网络异常都翻译成非 0
        ``error_code``，所以这里同时核验返回值与异常两条路径。
        """
        if self._bs is None:
            import baostock  # 延迟导入：可选依赖，见 requirements-baostock.txt

            self._bs = baostock
        if not self._logged_in:
            try:
                result = self._bs.login()
                self._apply_socket_timeout()
            except Exception as exc:
                self._discard_session()
                raise BaostockError(f"BaoStock 登录异常：{type(exc).__name__}: {exc}") from exc
            code = str(getattr(result, "error_code", "") or "")
            if code != "0":
                self._discard_session()
                raise BaostockError(
                    f"BaoStock 登录失败：{code or '<missing>'} {getattr(result, 'error_msg', '')}"
                )
            self._logged_in = True
        return self._bs

    def _apply_socket_timeout(self) -> None:
        """Bound later queries with a per-socket timeout (no global socket default)."""
        try:
            from baostock.common import context as bs_context
        except Exception:  # noqa: BLE001 - 取不到上下文时保持 SDK 默认行为
            logger.debug("BaoStock SDK 无 context 模块，跳过 socket timeout 设置")
            return
        sock = getattr(bs_context, "default_socket", None)
        self._session_socket = sock
        if sock is None:
            logger.warning("BaoStock 登录后未找到 default_socket，查询仍无超时保护")
            return
        try:
            sock.settimeout(self.socket_timeout_seconds)
        except (OSError, AttributeError) as exc:
            logger.warning("BaoStock socket 设置超时失败（%s），查询仍无超时保护", exc)

    def _discard_session(self) -> None:
        """A failed read leaves an unframed response in the TCP stream: never reuse it."""
        self._logged_in = False
        sock, self._session_socket = self._session_socket, None
        if sock is not None:
            try:
                sock.close()
            finally:
                bs_context = sys.modules.get("baostock.common.context")

                if getattr(bs_context, "default_socket", None) is sock:
                    bs_context.default_socket = None

    def close(self) -> None:
        """Logout the SDK session (idempotent; a later call logs in again)."""
        if self._bs is not None and self._logged_in:
            try:
                self._bs.logout()
            except Exception as exc:  # noqa: BLE001 - 退出清理不抛
                logger.debug("BaoStock logout 失败：%s", exc)
            finally:
                self._logged_in = False

    # ------------------------------------------------------------- daily bars

    def get_daily_data(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        period: int = 250,
    ) -> pd.DataFrame:
        """Return one stock's daily bars (qfq, volume in shares, amount in CNY).

        ``period`` 只在 ``start_date`` 缺省时用于推起点（每根 bar 约两个自然日，
        与 ``AkshareDailyProvider`` 一致）。停牌日不出现；当日 bar ``is_closed=False``。
        """
        symbol, normalized = self._to_symbol(ts_code)
        start, end = self._resolve_date_range(start_date, end_date, period)
        rows = self._query_daily(
            symbol=symbol,
            ts_code=normalized,
            fields=_STOCK_FIELDS,
            start_date=start,
            end_date=end,
            adjustflag=_STOCK_ADJUSTFLAG,
        )
        raw = self._rows_frame(rows, _STOCK_FIELDS, ts_code=normalized)
        if not raw.empty:
            status = raw["tradestatus"].astype(str).str.strip()
            unknown = sorted(set(status.unique()) - {"0", "1"})
            if unknown:
                raise BaostockDataError(f"BaoStock 返回未知 tradestatus={unknown}（{normalized}）")
            raw = raw.loc[status == "1"]
        return self._build_frame(
            raw,
            ts_code=normalized,
            adjust=_STOCK_ADJUST,
            with_turnover=True,
            require_volume=True,
        )

    def get_index_daily(
        self,
        ts_code: str = "000001.SH",
        start_date: str | None = None,
        end_date: str | None = None,
        period: int = 250,
    ) -> pd.DataFrame:
        """Return one index's daily bars (unadjusted, ``adjustflag=3``)."""
        symbol, normalized = self._to_symbol(ts_code)
        start, end = self._resolve_date_range(start_date, end_date, period)
        rows = self._query_daily(
            symbol=symbol,
            ts_code=normalized,
            fields=_INDEX_FIELDS,
            start_date=start,
            end_date=end,
            adjustflag=_INDEX_ADJUSTFLAG,
        )
        raw = self._rows_frame(rows, _INDEX_FIELDS, ts_code=normalized)
        return self._build_frame(
            raw,
            ts_code=normalized,
            adjust=_INDEX_ADJUST,
            with_turnover=False,
            require_volume=False,
        )

    # ---------------------------------------------------------------- calendar

    def get_trade_calendar(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        """Return BaoStock's trading days as ``trade_date`` (``YYYYMMDD``)."""
        bs = self._get_baostock()
        start = self._fmt(start_date or "1990-12-19")
        end = self._fmt(end_date or _shanghai_today().strftime("%Y%m%d"))
        try:
            result = bs.query_trade_dates(start_date=start, end_date=end)
        except Exception as exc:
            raise BaostockError(f"BaoStock 交易日历查询异常：{type(exc).__name__}: {exc}") from exc
        self._raise_on_error(result, ts_code="trade_calendar", stage="交易日历查询")
        rows: list[list[str]] = []
        while result.next():
            rows.append(result.get_row_data())
        self._raise_on_error(result, ts_code="trade_calendar", stage="交易日历分页")
        page_size = self._page_size()
        if self._ended_on_full_page(result, page_size):
            raise BaostockDataError(
                f"BaoStock 交易日历分页未正常结束：最后一页为满页 {page_size} 条，结果可能被截断"
            )
        if not rows:
            return pd.DataFrame({"trade_date": pd.Series(dtype="object")})
        frame = pd.DataFrame(rows, columns=result.fields)
        missing = {"calendar_date", "is_trading_day"} - set(frame.columns)
        if missing:
            raise BaostockDataError(f"BaoStock 交易日历缺少字段：{sorted(missing)}")
        trading = frame.loc[frame["is_trading_day"].astype(str).str.strip() == "1", "calendar_date"]
        dates = pd.to_datetime(trading, format="%Y-%m-%d", errors="coerce")
        if dates.isna().any():
            raise BaostockDataError("BaoStock 交易日历含无法解析的日期")
        return pd.DataFrame({"trade_date": dates.dt.strftime("%Y%m%d")}).reset_index(drop=True)

    # -------------------------------------------------------------- internals

    def _query_daily(self, *, symbol: str, ts_code: str, fields: str,
                     start_date: str, end_date: str, adjustflag: str) -> list[list[str]]:
        """Bound response sizes; save nothing unless every non-overlapping chunk succeeds."""
        cursor, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
        if cursor > end:
            raise ValueError("历史区间起点不能晚于终点")
        rows: list[list[str]] = []
        while cursor <= end:
            chunk_end = min(cursor + pd.Timedelta(days=729), end)
            rows.extend(self._query_chunk(
                symbol=symbol, ts_code=ts_code, fields=fields,
                start_date=cursor.strftime("%Y-%m-%d"), end_date=chunk_end.strftime("%Y-%m-%d"),
                adjustflag=adjustflag,
            ))
            cursor = chunk_end + pd.Timedelta(days=1)
        return rows

    def _query_chunk(
        self,
        *,
        symbol: str,
        ts_code: str,
        fields: str,
        start_date: str,
        end_date: str,
        adjustflag: str,
    ) -> list[list[str]]:
        """Run one paginated query, verifying SDK error codes on both ends."""
        bs = self._get_baostock()
        try:
            result = bs.query_history_k_data_plus(
                symbol, fields, start_date=self._fmt(start_date), end_date=self._fmt(end_date),
                frequency=_FREQUENCY, adjustflag=adjustflag,
            )
            self._raise_on_error(result, ts_code=ts_code, stage="查询")
            expected = fields.split(",")
            returned = [str(field).strip() for field in (getattr(result, "fields", None) or [])]
            if returned != expected:
                raise BaostockDataError(f"BaoStock 返回字段与请求不一致（{ts_code}）：{returned}")
            identity = {
                "code": symbol, "start_date": self._fmt(start_date), "end_date": self._fmt(end_date),
                "frequency": _FREQUENCY,
            }
            self._validate_response_identity(result, identity)
            code_index = expected.index("code")
            # The public server leaves result.adjustflag blank. The explicitly
            # requested row field is authoritative and identifies every page's basis.
            adjustment_index = expected.index("adjustflag")
            rows: list[list[str]] = []
            while result.next():
                row = result.get_row_data()
                if len(row) != len(expected) or row[code_index] != symbol:
                    raise BaostockDataError(f"BaoStock 返回了不同证券或列数错误的历史行（请求 {symbol}）")
                if row[adjustment_index] != adjustflag:
                    raise BaostockDataError(
                        f"BaoStock 行复权口径错误：adjustflag={row[adjustment_index]!r}，请求为 {adjustflag!r}"
                    )
                rows.append(row)
            self._raise_on_error(result, ts_code=ts_code, stage="分页")
            self._validate_response_identity(result, identity)
            page_size = self._page_size()
            if self._ended_on_full_page(result, page_size):
                raise BaostockDataError(f"BaoStock 分页被截断（{ts_code}）：末页仍为 {page_size} 条满页")
            return rows
        except BaostockError:
            self._discard_session()
            raise
        except Exception as exc:
            self._discard_session()
            raise BaostockError(f"BaoStock 查询异常（{ts_code}）：{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _validate_response_identity(result, expected: dict) -> None:
        for key, value in expected.items():
            actual = str(getattr(result, key, ""))
            if actual != value:
                raise BaostockDataError(f"BaoStock 响应串线：{key}={actual!r}，请求为 {value!r}")

    @staticmethod
    def _rows_frame(rows: list[list[str]], fields: str, *, ts_code: str) -> pd.DataFrame:
        """Build the raw frame, rejecting rows whose width drifts from the fields."""
        if not rows:
            return pd.DataFrame()
        width = len(fields.split(","))
        ragged = [index for index, row in enumerate(rows) if len(row) != width]
        if ragged:
            raise BaostockDataError(
                f"BaoStock 返回行列数与请求字段不一致（{ts_code}）：行 {ragged[:3]}"
            )
        return pd.DataFrame(rows, columns=fields.split(","))

    @staticmethod
    def _raise_on_error(result, *, ts_code: str, stage: str) -> None:
        if not hasattr(result, "error_code"):
            raise BaostockDataError(f"BaoStock {stage}返回对象缺少 error_code（{ts_code}）")
        code = str(result.error_code or "")
        if code != "0":
            raise BaostockError(
                f"BaoStock {stage}失败（{ts_code}）：{code or '<empty>'} "
                f"{getattr(result, 'error_msg', '')}"
            )

    @staticmethod
    def _page_size() -> int:
        try:
            from baostock.common.contants import BAOSTOCK_PER_PAGE_COUNT

            return int(BAOSTOCK_PER_PAGE_COUNT)
        except Exception:  # noqa: BLE001 - SDK 版本差异时用同值兜底
            return _DEFAULT_PAGE_SIZE

    @staticmethod
    def _ended_on_full_page(result, page_size: int) -> bool:
        """Detect pagination that stopped on an exactly-full page (SDK swallows it).

        SDK 只在"当前页消费完且条数 < 每页条数"时判定结束；成功翻到空页时
        ``data`` 会被替换成 ``[]``，所以"结束时的 ``data`` 恰好等于每页条数"
        只可能来自未能完成的分页请求。
        """
        page = list(getattr(result, "data", None) or [])
        if not page:
            return False
        if len(page) > page_size:
            return False  # SDK 单次返回整段（不翻页）的模式
        return len(page) == page_size

    @staticmethod
    def _to_symbol(ts_code: str) -> tuple[str, str]:
        """Map ts_code to BaoStock ``sh.600000`` form, rejecting unsupported markets."""
        normalized = normalize_ts_code(ts_code)
        code, exchange = split_ts_code(normalized)
        if exchange == "BJ":
            raise BaostockUnsupportedError(f"BaoStock 不支持北交所证券：{ts_code}")
        if exchange not in {"SH", "SZ"} or len(code) != 6 or not code.isdigit():
            raise BaostockUnsupportedError(
                f"无法识别的证券代码：{ts_code!r}（需要 000001.SZ / 600000.SH 形式）"
            )
        return f"{exchange.lower()}.{code}", normalized

    @staticmethod
    def _fmt(date: str | None) -> str:
        """Accept ``YYYYMMDD`` / ``YYYY-MM-DD``; Baostock wants ``YYYY-MM-DD``."""
        if not date:
            return ""
        raw = str(date).replace("-", "").strip()
        if len(raw) != 8 or not raw.isdigit():
            raise ValueError(f"日期格式应为 YYYYMMDD 或 YYYY-MM-DD，实际为 {date!r}")
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"

    @classmethod
    def _resolve_date_range(
        cls,
        start_date: str | None,
        end_date: str | None,
        period: int,
    ) -> tuple[str, str]:
        if start_date is None:
            if period <= 0:
                raise ValueError("period 必须为正数")
            start_date = (datetime.now(_CN_TZ) - timedelta(days=period * 2)).strftime("%Y%m%d")
        if end_date is None:
            end_date = datetime.now(_CN_TZ).strftime("%Y%m%d")
        start = str(start_date).replace("-", "").strip()
        end = str(end_date).replace("-", "").strip()
        # 先做格式校验，避免把非法日期送进 SDK 后拿到含糊的错误码。
        cls._fmt(start)
        cls._fmt(end)
        return start, end

    @classmethod
    def _build_frame(
        cls,
        raw: pd.DataFrame,
        *,
        ts_code: str,
        adjust: str,
        with_turnover: bool,
        require_volume: bool,
    ) -> pd.DataFrame:
        columns = _BAR_COLUMNS if with_turnover else _INDEX_BAR_COLUMNS
        if raw.empty:
            return cls._empty_frame(columns, ts_code=ts_code, adjust=adjust, with_turnover=with_turnover)

        datetimes = pd.to_datetime(raw["date"], format="%Y-%m-%d", errors="coerce")
        if datetimes.isna().any():
            bad = raw.loc[datetimes.isna(), "date"].astype(str).tolist()[:3]
            raise BaostockDataError(f"BaoStock 日线日期无法解析（{ts_code}）：{bad}")
        if datetimes.duplicated().any():
            duplicated = datetimes.loc[datetimes.duplicated()].dt.strftime("%Y%m%d").tolist()[:3]
            raise BaostockDataError(f"BaoStock 日线日期重复（{ts_code}）：{duplicated}")

        out = pd.DataFrame(
            {
                "trade_date": datetimes.dt.strftime("%Y%m%d"),
                "datetime": datetimes,
                "open": pd.to_numeric(raw["open"], errors="coerce"),
                "high": pd.to_numeric(raw["high"], errors="coerce"),
                "low": pd.to_numeric(raw["low"], errors="coerce"),
                "close": pd.to_numeric(raw["close"], errors="coerce"),
                "volume": pd.to_numeric(raw["volume"], errors="coerce"),
                "amount": pd.to_numeric(raw["amount"], errors="coerce"),
                "is_closed": datetimes.dt.normalize() < pd.Timestamp(_shanghai_today()),
            }
        )
        if with_turnover:
            out["turnover_rate"] = pd.to_numeric(raw["turn"], errors="coerce")
        out = out.loc[:, list(columns)]
        cls._validate_bars(out, ts_code=ts_code, require_volume=require_volume)
        out = out.sort_values("datetime", kind="stable").reset_index(drop=True)
        return cls._attach_attrs(out, ts_code=ts_code, adjust=adjust, with_turnover=with_turnover)

    @staticmethod
    def _validate_bars(frame: pd.DataFrame, *, ts_code: str, require_volume: bool) -> None:
        """Reject rows that cannot be a real trading bar (never silently mark valid)."""
        prices = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
        illegal = ~np.isfinite(prices).all(axis=1) | (prices <= 0).any(axis=1)
        if illegal.any():
            raise BaostockDataError(
                f"BaoStock 日线 OHLC 非法（{ts_code}）：{frame.loc[illegal, 'trade_date'].tolist()[:3]}"
            )
        outside = (
            (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
            | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
        )
        if outside.any():
            raise BaostockDataError(
                f"BaoStock 日线 OHLC 关系非法（{ts_code}）：{frame.loc[outside, 'trade_date'].tolist()[:3]}"
            )

        volume = frame["volume"]
        if require_volume:
            bad_volume = volume.isna() | (volume < 0) | np.isinf(volume)
        else:
            bad_volume = (volume < 0) | np.isinf(volume)
        if bad_volume.any():
            raise BaostockDataError(
                f"BaoStock 日线成交量非法（{ts_code}）：{frame.loc[bad_volume, 'trade_date'].tolist()[:3]}"
            )

        amount = frame["amount"]
        bad_amount = (amount < 0) | np.isinf(amount)
        if bad_amount.any():
            raise BaostockDataError(
                f"BaoStock 日线成交额非法（{ts_code}）：{frame.loc[bad_amount, 'trade_date'].tolist()[:3]}"
            )
        if "turnover_rate" in frame.columns:
            turn = frame["turnover_rate"]
            bad_turn = (turn < 0) | np.isinf(turn)
            if bad_turn.any():
                raise BaostockDataError(
                    f"BaoStock 日线换手率非法（{ts_code}）："
                    f"{frame.loc[bad_turn, 'trade_date'].tolist()[:3]}"
                )

    @classmethod
    def _empty_frame(
        cls,
        columns: Sequence[str],
        *,
        ts_code: str,
        adjust: str,
        with_turnover: bool,
    ) -> pd.DataFrame:
        frame = pd.DataFrame({column: pd.Series(dtype=_COLUMN_DTYPES[column]) for column in columns})
        return cls._attach_attrs(frame, ts_code=ts_code, adjust=adjust, with_turnover=with_turnover)

    @staticmethod
    def _attach_attrs(
        frame: pd.DataFrame,
        *,
        ts_code: str,
        adjust: str,
        with_turnover: bool,
    ) -> pd.DataFrame:
        """Attach the provenance/unit contract downstream audits read from ``attrs``."""
        attrs = {
            "source": "baostock",
            "adjust": adjust,
            "timeframe": _TIMEFRAME,
            "timezone": _TIMEZONE,
            "timestamp_semantics": "end",
            "volume_unit": "shares",
            "amount_unit": "CNY",
            "ts_code": ts_code,
        }
        if with_turnover:
            attrs["turnover_rate_unit"] = "percent"
        frame.attrs = attrs
        return frame
