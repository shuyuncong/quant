"""pytdx-backed minute market data provider."""

from __future__ import annotations

import logging
from typing import Dict, Iterable, Optional

import pandas as pd

from ..symbols import normalize_ts_code, to_pytdx_params
from .base import BaseMarketDataProvider

logger = logging.getLogger(__name__)


class PytdxMinuteProvider(BaseMarketDataProvider):
    """Provider for minute-level market data sourced from pytdx."""

    name = "pytdx"

    INTERVAL_CATEGORY = {
        "5m": 0,
        "15m": 1,
        "30m": 2,
        "60m": 3,
        "1d": 4,
        "1m": 8,
    }

    DEFAULT_HOSTS = (
        ("180.153.18.170", 7709),
        ("119.147.212.81", 7709),
        ("114.80.63.12", 7709),
    )

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config=config)
        data_config = self.config.get("data", {})
        self.host = str(data_config.get("pytdx_host", "180.153.18.170"))
        self.port = int(data_config.get("pytdx_port", 7709))
        self.hosts = list(data_config.get("pytdx_hosts", []))

    def get_stock_list(self, exchange: str = "", list_status: str = "L") -> pd.DataFrame:
        return pd.DataFrame()

    def get_daily_data(
        self,
        ts_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: int = 250,
    ) -> pd.DataFrame:
        return pd.DataFrame()

    def get_index_daily(
        self,
        ts_code: str = "000001.SH",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: int = 250,
    ) -> pd.DataFrame:
        return pd.DataFrame()

    def get_daily_basic(self, ts_code: str, trade_date: Optional[str] = None) -> Optional[Dict]:
        return None

    def get_financial_data(self, ts_code: str, period: Optional[str] = None) -> Optional[Dict]:
        return None

    def get_minute_data(
        self,
        ts_code: str,
        interval: str = "5m",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        count: Optional[int] = None,
    ) -> pd.DataFrame:
        """Return recent minute bars using the first reachable TDX server."""
        del start_date, end_date

        api = self._connect()
        if api is None:
            return pd.DataFrame()

        category = self.INTERVAL_CATEGORY.get(interval, 0)
        market, code = to_pytdx_params(ts_code)
        bars_count = min(int(count or 240), 800)

        try:
            raw = api.get_security_bars(category, market, code, 0, bars_count)
            if not raw:
                return pd.DataFrame()

            df = api.to_df(raw)
            if df.empty:
                return pd.DataFrame()

            if "datetime" in df.columns:
                df["datetime"] = pd.to_datetime(df["datetime"])
                df["trade_date"] = df["datetime"].dt.strftime("%Y%m%d")

            renamed = df.rename(
                columns={
                    "vol": "vol",
                    "amount": "amount",
                    "open": "open",
                    "close": "close",
                    "high": "high",
                    "low": "low",
                }
            ).copy()
            renamed["volume"] = renamed["vol"]
            renamed.attrs["ts_code"] = normalize_ts_code(ts_code)
            return renamed.reset_index(drop=True)
        except Exception as exc:
            logger.error("pytdx get_minute_data failed for %s: %s", ts_code, exc)
            return pd.DataFrame()
        finally:
            try:
                api.disconnect()
            except Exception:
                pass

    def _connect(self):
        """Connect to the first reachable TDX server from the configured pool."""
        try:
            from pytdx.hq import TdxHq_API
        except Exception as exc:
            logger.error("pytdx import failed: %s", exc)
            return None

        for host, port in self._iter_endpoints():
            api = TdxHq_API()
            try:
                result = api.connect(host, port, time_out=10)
                if result:
                    logger.info("pytdx connected: %s:%s", host, port)
                    return api
                logger.warning("pytdx connect failed: %s:%s", host, port)
            except Exception as exc:
                logger.warning("pytdx connect error: %s:%s error=%s", host, port, exc)
            try:
                api.disconnect()
            except Exception:
                pass

        logger.error("pytdx connect failed for all configured hosts")
        return None

    def _iter_endpoints(self) -> Iterable[tuple[str, int]]:
        """Yield configured hosts first, then fall back to known-good defaults."""
        seen = set()

        configured = [(self.host, self.port)]
        for item in self.hosts:
            if isinstance(item, dict):
                configured.append((str(item.get("host", "")), int(item.get("port", 7709))))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                configured.append((str(item[0]), int(item[1])))
            elif isinstance(item, str):
                if ":" in item:
                    host, port = item.rsplit(":", 1)
                    configured.append((host, int(port)))
                else:
                    configured.append((item, 7709))

        for host, port in configured + list(self.DEFAULT_HOSTS):
            if not host or (host, port) in seen:
                continue
            seen.add((host, port))
            yield host, port
