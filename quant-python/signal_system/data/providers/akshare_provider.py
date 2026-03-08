"""AKShare-backed daily market data provider.

This provider intentionally avoids the Eastmoney-backed AKShare APIs that are
blocked in the current runtime environment. The implementation only uses the
APIs that were verified locally on this machine:

- stock list: ``stock_info_a_code_name``
- stock daily bars: ``stock_zh_a_daily``
- index daily bars: ``stock_zh_index_daily`` / ``stock_zh_index_daily_tx``
- trade calendar: ``tool_trade_date_hist_sina``
- fundamentals: ``stock_financial_abstract_ths``
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import logging
import os
from typing import Dict, Optional

import pandas as pd

from ..symbols import normalize_ts_code, split_ts_code, to_akshare_symbol
from .base import BaseMarketDataProvider

logger = logging.getLogger(__name__)


class AkshareDailyProvider(BaseMarketDataProvider):
    """Provider for daily market data sourced from AKShare."""

    name = "akshare"

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config=config)
        self._ak = None
        self._daily_cache: dict[tuple[str, str, str], pd.DataFrame] = {}
        self._index_cache: dict[tuple[str, str, str], pd.DataFrame] = {}
        self._financial_cache: dict[str, Optional[Dict]] = {}

    def get_stock_list(self, exchange: str = "", list_status: str = "L") -> pd.DataFrame:
        """Return the active A-share stock list in internal ts_code format."""
        ak = self._get_akshare()
        if ak is None:
            return pd.DataFrame()

        try:
            with self._network_context():
                df = ak.stock_info_a_code_name()
            if df is None or df.empty:
                return pd.DataFrame()

            stock_list = df.rename(columns={"code": "symbol", "name": "name"}).copy()
            stock_list["ts_code"] = stock_list["symbol"].map(normalize_ts_code)

            # Keep the default active universe clean by dropping ST and delisting names.
            if "name" in stock_list.columns:
                stock_list = stock_list[
                    ~stock_list["name"].astype(str).str.contains("ST|退", na=False)
                ]

            if exchange:
                stock_list = stock_list[stock_list["ts_code"].str.endswith(f".{exchange.upper()}")]
            if list_status == "L":
                stock_list = stock_list.reset_index(drop=True)

            return stock_list.loc[:, ["symbol", "name", "ts_code"]].reset_index(drop=True)
        except Exception as exc:
            logger.error("AKShare get_stock_list failed: %s", exc)
            return pd.DataFrame()

    def get_daily_data(
        self,
        ts_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: int = 250,
    ) -> pd.DataFrame:
        """Return normalized daily OHLCV bars for one stock."""
        ak = self._get_akshare()
        if ak is None:
            return pd.DataFrame()

        start_date, end_date = self._resolve_date_range(start_date=start_date, end_date=end_date, period=period)
        cache_key = (normalize_ts_code(ts_code), start_date, end_date)
        cached = self._daily_cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        symbol = to_akshare_symbol(ts_code, with_exchange_prefix=True)
        try:
            with self._network_context():
                df = ak.stock_zh_a_daily(
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq",
                )
            normalized = self._normalize_stock_daily_frame(df, ts_code=ts_code)
            self._daily_cache[cache_key] = normalized
            return normalized.copy()
        except Exception as exc:
            logger.error("AKShare get_daily_data failed for %s: %s", ts_code, exc)
            return pd.DataFrame()

    def get_index_daily(
        self,
        ts_code: str = "000001.SH",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: int = 250,
    ) -> pd.DataFrame:
        """Return normalized daily bars for a major index."""
        ak = self._get_akshare()
        if ak is None:
            return pd.DataFrame()

        start_date, end_date = self._resolve_date_range(start_date=start_date, end_date=end_date, period=period)
        cache_key = (normalize_ts_code(ts_code), start_date, end_date)
        cached = self._index_cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        code, exchange = split_ts_code(ts_code)
        symbol = f"{exchange.lower()}{code}"

        frames: list[pd.DataFrame] = []
        with self._network_context():
            try:
                frames.append(ak.stock_zh_index_daily(symbol=symbol))
            except Exception as exc:
                logger.warning("AKShare stock_zh_index_daily failed for %s: %s", ts_code, exc)
            try:
                frames.append(ak.stock_zh_index_daily_tx(symbol=symbol))
            except Exception as exc:
                logger.warning("AKShare stock_zh_index_daily_tx failed for %s: %s", ts_code, exc)

        for frame in frames:
            normalized = self._normalize_index_frame(frame, ts_code=ts_code)
            if normalized.empty:
                continue
            normalized = normalized[
                (normalized["trade_date"] >= start_date) & (normalized["trade_date"] <= end_date)
            ].reset_index(drop=True)
            self._index_cache[cache_key] = normalized
            return normalized.copy()

        logger.error("AKShare get_index_daily failed for %s: no working index API", ts_code)
        return pd.DataFrame()

    def get_daily_basic(self, ts_code: str, trade_date: Optional[str] = None) -> Optional[Dict]:
        """Return lightweight daily metrics required by the selector.

        This method no longer depends on AKShare spot interfaces because they are
        unstable in the current environment. Instead it derives the necessary
        fields from verified daily bars and financial summaries.
        """
        target_date = trade_date or self.get_latest_trade_date()
        daily_df = self.get_daily_data(ts_code, end_date=target_date, period=60)
        if daily_df.empty:
            return None

        latest = daily_df.iloc[-1]
        financial = self._get_financial_snapshot(ts_code)
        latest_close = self._to_float(latest.get("close"))
        outstanding_share = self._to_float(latest.get("outstanding_share"))
        latest_volume = self._to_float(latest.get("vol"))
        avg_volume = self._to_float(daily_df["vol"].iloc[-6:-1].mean()) if len(daily_df) >= 6 else None

        eps = financial.get("eps") if financial else None
        bps = financial.get("bps") if financial else None
        total_mv = None
        if latest_close is not None and outstanding_share:
            # Keep the legacy unit in 10k CNY so upper layers can keep dividing by 10000.
            total_mv = latest_close * outstanding_share / 10000

        return {
            "ts_code": normalize_ts_code(ts_code),
            "trade_date": str(latest.get("trade_date", target_date)),
            "turnover_rate": self._to_float(latest.get("turnover_rate")),
            "volume_ratio": (latest_volume / avg_volume) if latest_volume is not None and avg_volume else None,
            "pe": (latest_close / eps) if latest_close is not None and eps not in (None, 0) else None,
            "pb": (latest_close / bps) if latest_close is not None and bps not in (None, 0) else None,
            "total_mv": total_mv,
            "circ_mv": total_mv,
        }

    def get_financial_data(self, ts_code: str, period: Optional[str] = None) -> Optional[Dict]:
        """Return the latest selector-level financial snapshot."""
        snapshot = self._get_financial_snapshot(ts_code)
        if snapshot is None:
            return None

        result = {
            "ts_code": snapshot["ts_code"],
            "period": snapshot["period"],
            "roe": snapshot["roe"],
            "debt_to_assets": snapshot["debt_to_assets"],
            "current_ratio": snapshot["current_ratio"],
            "quick_ratio": snapshot["quick_ratio"],
            "eps": snapshot.get("eps"),
            "bps": snapshot.get("bps"),
        }
        if period:
            result["period"] = period
        return result

    def get_trade_calendar(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Return normalized trading calendar."""
        ak = self._get_akshare()
        if ak is None:
            return pd.DataFrame()

        try:
            with self._network_context():
                df = ak.tool_trade_date_hist_sina()
            if df is None or df.empty:
                return pd.DataFrame()

            calendar = df.copy()
            calendar["trade_date"] = pd.to_datetime(calendar["trade_date"]).dt.strftime("%Y%m%d")
            if start_date:
                calendar = calendar[calendar["trade_date"] >= start_date]
            if end_date:
                calendar = calendar[calendar["trade_date"] <= end_date]
            return calendar.reset_index(drop=True)
        except Exception as exc:
            logger.error("AKShare get_trade_calendar failed: %s", exc)
            return pd.DataFrame()

    def get_latest_trade_date(self) -> Optional[str]:
        """Return the latest trading day from the verified Sina calendar API."""
        calendar = self.get_trade_calendar(
            start_date=(datetime.now() - timedelta(days=14)).strftime("%Y%m%d"),
            end_date=datetime.now().strftime("%Y%m%d"),
        )
        if calendar.empty:
            return datetime.now().strftime("%Y%m%d")
        return str(calendar.iloc[-1]["trade_date"])

    def _get_akshare(self):
        if self._ak is not None:
            return self._ak
        try:
            import akshare as ak

            self._ak = ak
            return ak
        except Exception as exc:
            logger.error("AKShare import failed: %s", exc)
            return None

    def _get_financial_snapshot(self, ts_code: str) -> Optional[Dict]:
        """Fetch and normalize the latest financial summary row."""
        normalized_code = normalize_ts_code(ts_code)
        if normalized_code in self._financial_cache:
            cached = self._financial_cache[normalized_code]
            return dict(cached) if cached else None

        ak = self._get_akshare()
        if ak is None:
            return None

        code = to_akshare_symbol(normalized_code)
        try:
            with self._network_context():
                df = ak.stock_financial_abstract_ths(symbol=code)
            if df is None or df.empty:
                self._financial_cache[normalized_code] = None
                return None

            working = df.copy()
            working["报告期_dt"] = pd.to_datetime(working["报告期"], errors="coerce")
            working = working.sort_values("报告期_dt").reset_index(drop=True)
            latest = working.iloc[-1]

            snapshot = {
                "ts_code": normalized_code,
                "period": latest.get("报告期", "").replace("-", ""),
                "roe": self._parse_percent(latest.get("净资产收益率"))
                or self._parse_percent(latest.get("净资产收益率-摊薄")),
                "debt_to_assets": self._parse_percent(latest.get("资产负债率")),
                "current_ratio": self._parse_numeric(latest.get("流动比率")),
                "quick_ratio": self._parse_numeric(latest.get("速动比率")),
                "eps": self._parse_numeric(latest.get("基本每股收益")),
                "bps": self._parse_numeric(latest.get("每股净资产")),
            }
            self._financial_cache[normalized_code] = snapshot
            return dict(snapshot)
        except Exception as exc:
            logger.error("AKShare get_financial_data failed for %s: %s", ts_code, exc)
            self._financial_cache[normalized_code] = None
            return None

    @contextmanager
    def _network_context(self):
        """Temporarily disable system proxies when configured to do so."""
        if not self.config.get("data", {}).get("disable_system_proxy", False):
            yield
            return

        proxy_keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
        backup = {key: os.environ.get(key) for key in proxy_keys}
        try:
            for key in proxy_keys:
                os.environ.pop(key, None)
            yield
        finally:
            for key, value in backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    @staticmethod
    def _resolve_date_range(
        start_date: Optional[str],
        end_date: Optional[str],
        period: int,
    ) -> tuple[str, str]:
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=period * 2)).strftime("%Y%m%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")
        return start_date, end_date

    @classmethod
    def _normalize_stock_daily_frame(cls, df: pd.DataFrame, ts_code: str) -> pd.DataFrame:
        """Normalize AKShare stock daily bars into the project's standard schema."""
        if df is None or df.empty:
            return pd.DataFrame()

        renamed = df.rename(
            columns={
                "date": "trade_date",
                "open": "open",
                "close": "close",
                "high": "high",
                "low": "low",
                "volume": "vol",
                "amount": "amount",
                "turnover": "turnover_rate",
                "outstanding_share": "outstanding_share",
            }
        ).copy()

        renamed["trade_date"] = pd.to_datetime(renamed["trade_date"]).dt.strftime("%Y%m%d")
        renamed["datetime"] = pd.to_datetime(renamed["trade_date"])

        numeric_columns = ["open", "close", "high", "low", "vol", "amount", "turnover_rate", "outstanding_share"]
        for column in numeric_columns:
            if column in renamed.columns:
                renamed[column] = pd.to_numeric(renamed[column], errors="coerce")

        if "turnover_rate" in renamed.columns and renamed["turnover_rate"].dropna().abs().max() <= 1.0:
            renamed["turnover_rate"] = renamed["turnover_rate"] * 100

        renamed["volume"] = renamed["vol"]
        renamed.attrs["ts_code"] = normalize_ts_code(ts_code)
        return renamed.sort_values("trade_date").reset_index(drop=True)

    @classmethod
    def _normalize_index_frame(cls, df: pd.DataFrame, ts_code: str) -> pd.DataFrame:
        """Normalize AKShare index bars into the project's standard schema."""
        if df is None or df.empty:
            return pd.DataFrame()

        renamed = df.rename(
            columns={
                "date": "trade_date",
                "open": "open",
                "close": "close",
                "high": "high",
                "low": "low",
                "volume": "vol",
                "amount": "amount",
            }
        ).copy()
        renamed["trade_date"] = pd.to_datetime(renamed["trade_date"]).dt.strftime("%Y%m%d")
        renamed["datetime"] = pd.to_datetime(renamed["trade_date"])

        for column in ("open", "close", "high", "low", "vol", "amount"):
            if column in renamed.columns:
                renamed[column] = pd.to_numeric(renamed[column], errors="coerce")

        if "vol" in renamed.columns:
            renamed["volume"] = renamed["vol"]

        renamed.attrs["ts_code"] = normalize_ts_code(ts_code)
        return renamed.sort_values("trade_date").reset_index(drop=True)

    @classmethod
    def _parse_percent(cls, value) -> Optional[float]:
        numeric = cls._parse_numeric(value)
        if numeric is None:
            return None
        return numeric

    @staticmethod
    def _to_float(value) -> Optional[float]:
        try:
            if value is None or pd.isna(value):
                return None
            return float(value)
        except Exception:
            return None

    @classmethod
    def _parse_numeric(cls, value) -> Optional[float]:
        """Parse financial strings such as ``11.09%`` or ``57.51亿`` into floats."""
        if value in (None, "", "-", "--", False, "False", "None"):
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)

        text = str(value).strip().replace(",", "")
        if not text or text in {"False", "None"}:
            return None

        multiplier = 1.0
        if text.endswith("%"):
            text = text[:-1]
        elif text.endswith("亿"):
            text = text[:-1]
            multiplier = 1e8
        elif text.endswith("万"):
            text = text[:-1]
            multiplier = 1e4

        try:
            return float(text) * multiplier
        except Exception:
            return None
