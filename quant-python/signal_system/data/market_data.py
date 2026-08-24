"""A-share market-data adapters, normalization, caching and session resampling."""

from __future__ import annotations

from datetime import date, datetime, time as clock_time, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import pickle
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from utils.time_utils import now_shanghai


logger = logging.getLogger(__name__)

TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "120m": 120}
STANDARD_COLUMNS = [
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "is_closed",
]


def normalize_symbol(symbol: str) -> str:
    return str(symbol).strip().upper().split(".")[0].zfill(6)


def tushare_symbol(symbol: str) -> str:
    code = normalize_symbol(symbol)
    if code.startswith(("5", "6", "9")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def eastmoney_secid(symbol: str) -> str:
    code = normalize_symbol(symbol)
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    return f"{market}.{code}"


def eastmoney_index_secid(symbol: str) -> str:
    text = str(symbol).strip().upper()
    code = normalize_symbol(text)
    if text.endswith(".SZ"):
        return f"0.{code}"
    if text.endswith(".SH") or code.startswith("000"):
        return f"1.{code}"
    return eastmoney_secid(text)


def tencent_symbol(symbol: str) -> str:
    code = normalize_symbol(symbol)
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def standardize_bars(
    raw: pd.DataFrame,
    timeframe: str,
    source: str,
    adjust: str,
    now: datetime | None = None,
    minute_timestamp: str = "end",
) -> pd.DataFrame:
    if raw is None or raw.empty:
        result = pd.DataFrame(columns=STANDARD_COLUMNS)
        result.attrs.update({"source": source, "timeframe": timeframe, "adjust": adjust})
        return result

    aliases = {
        "日期": "datetime",
        "时间": "datetime",
        "trade_date": "datetime",
        "date": "datetime",
        "day": "datetime",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "最新价": "close",
        "成交量": "volume",
        "vol": "volume",
        "成交额": "amount",
    }
    frame = raw.rename(columns={column: aliases.get(column, column) for column in raw.columns}).copy()
    required = {"datetime", "open", "high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"行情缺少字段: {sorted(missing)}")
    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    if "amount" not in frame.columns:
        frame["amount"] = 0.0

    text_dates = frame["datetime"].astype(str).str.replace("-", "", regex=False)
    if timeframe == "1d" and text_dates.str.fullmatch(r"\d{8}").all():
        frame["datetime"] = pd.to_datetime(text_dates, format="%Y%m%d", errors="coerce")
    else:
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["datetime", "open", "high", "low", "close"])
    frame = frame.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime")

    current = now or now_shanghai()
    if "is_closed" not in frame.columns:
        if timeframe == "1d":
            today = pd.Timestamp(current.date())
            frame["is_closed"] = (frame["datetime"].dt.normalize() < today) | (
                (frame["datetime"].dt.normalize() == today) & (current.time() >= clock_time(15, 0))
            )
        else:
            minutes = TIMEFRAME_MINUTES[timeframe]
            close_time = frame["datetime"]
            if minute_timestamp == "start":
                close_time = close_time + pd.to_timedelta(minutes, unit="m")
            frame["is_closed"] = close_time <= pd.Timestamp(current)
    frame["is_closed"] = frame["is_closed"].astype(bool)
    frame = frame[STANDARD_COLUMNS].reset_index(drop=True)
    frame.attrs.update(
        {
            "source": source,
            "timeframe": timeframe,
            "adjust": adjust,
            "timezone": "Asia/Shanghai",
            "timestamp_semantics": "end" if minute_timestamp == "end" else "start",
        }
    )
    return frame


def resample_session_bars(
    frame: pd.DataFrame,
    target_minutes: int,
    source_minutes: int = 1,
) -> pd.DataFrame:
    """Aggregate bars inside each A-share trading session without crossing lunch."""
    if frame.empty:
        return frame.copy()
    if target_minutes % source_minutes:
        raise ValueError("目标周期必须是源周期的整数倍")

    source = frame[frame["is_closed"]].copy()
    source["datetime"] = pd.to_datetime(source["datetime"])
    rows: list[dict[str, Any]] = []
    grouped: dict[tuple[date, str, int], list[pd.Series]] = {}

    for _, row in source.iterrows():
        stamp = pd.Timestamp(row["datetime"])
        current_time = stamp.time()
        if clock_time(9, 30) < current_time <= clock_time(11, 30):
            session_name = "am"
            session_start = pd.Timestamp.combine(stamp.date(), clock_time(9, 30))
        elif clock_time(13, 0) < current_time <= clock_time(15, 0):
            session_name = "pm"
            session_start = pd.Timestamp.combine(stamp.date(), clock_time(13, 0))
        else:
            continue
        elapsed = int((stamp - session_start).total_seconds() // 60)
        bucket = max(0, (max(elapsed, 1) - 1) // target_minutes)
        grouped.setdefault((stamp.date(), session_name, bucket), []).append(row)

    expected_count = target_minutes // source_minutes
    for (trade_date, session_name, bucket), items in sorted(grouped.items()):
        session_start_time = clock_time(9, 30) if session_name == "am" else clock_time(13, 0)
        session_start = pd.Timestamp.combine(trade_date, session_start_time)
        expected_end = session_start + pd.Timedelta(minutes=(bucket + 1) * target_minutes)
        actual_end = pd.Timestamp(items[-1]["datetime"])
        if len(items) < expected_count or actual_end < expected_end:
            continue
        rows.append(
            {
                "datetime": expected_end,
                "open": float(items[0]["open"]),
                "high": max(float(item["high"]) for item in items),
                "low": min(float(item["low"]) for item in items),
                "close": float(items[-1]["close"]),
                "volume": sum(float(item["volume"]) for item in items),
                "amount": sum(float(item.get("amount", 0.0)) for item in items),
                "is_closed": True,
            }
        )
    result = pd.DataFrame(rows, columns=STANDARD_COLUMNS)
    result.attrs.update(frame.attrs)
    result.attrs["timeframe"] = f"{target_minutes}m"
    return result


class MarketDataClient:
    def __init__(self, config: dict[str, Any]):
        data_config = config.get("market_data", config.get("data_source", {}))
        self.provider = str(data_config.get("provider", "auto")).lower()
        self.adjust = str(data_config.get("adjust", "none")).strip().lower() or "none"
        self.min_listing_trade_days = int(data_config.get("min_listing_trade_days", 120))
        self.minute_timestamp = str(data_config.get("minute_timestamp", "end"))
        self.cache_dir = Path(data_config.get("cache_dir", "./cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir = self.cache_dir / "daily_history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.use_cache = bool(data_config.get("use_cache", True))
        self.request_interval = float(data_config.get("request_interval_seconds", 0.35))
        token = data_config.get("tushare_token") or os.getenv("TUSHARE_TOKEN", "")
        self.tushare_token = "" if "你的" in str(token) else str(token)
        self._last_request = 0.0
        self._akshare = None
        self._tushare = None

    @staticmethod
    def _http_json(url: str, params: dict[str, Any]) -> Any:
        request = Request(
            f"{url}?{urlencode(params)}",
            headers={
                "User-Agent": "Mozilla/5.0 quant-signal-monitor/1.0",
                "Referer": "https://quote.eastmoney.com/",
            },
        )
        with urlopen(request, timeout=15) as response:
            body = response.read(20 * 1024 * 1024 + 1)
        if len(body) > 20 * 1024 * 1024:
            raise RuntimeError("行情响应体超过20MB")
        return json.loads(body.decode("utf-8"))

    def _throttle(self) -> None:
        remaining = self.request_interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request = time.monotonic()

    def _get_akshare(self):
        if self._akshare is None:
            try:
                import akshare as ak
            except ImportError as exc:
                raise RuntimeError("AkShare 行情需要安装 akshare: pip install akshare") from exc
            self._akshare = ak
        return self._akshare

    def _get_tushare(self):
        if not self.tushare_token:
            raise RuntimeError("未配置 Tushare Token")
        if self._tushare is None:
            try:
                import tushare as ts
            except ImportError as exc:
                raise RuntimeError("请先安装 tushare: pip install tushare") from exc
            ts.set_token(self.tushare_token)
            self._tushare = ts.pro_api()
        return self._tushare

    def _fetch_sina_stock_snapshot(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, 100):
            batch = self._http_json(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
                {
                    "page": page,
                    "num": 100,
                    "sort": "symbol",
                    "asc": 1,
                    "node": "hs_a",
                    "symbol": "",
                },
            )
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.pkl"

    def _cached(self, key: str, ttl_seconds: int) -> pd.DataFrame | None:
        if not self.use_cache:
            return None
        path = self._cache_path(key)
        if not path.exists() or time.time() - path.stat().st_mtime > ttl_seconds:
            return None
        try:
            with path.open("rb") as handle:
                return pickle.load(handle)
        except Exception as exc:
            logger.warning("读取行情缓存失败: %s", exc)
            return None

    def _save_cache(self, key: str, value: pd.DataFrame) -> None:
        if not self.use_cache:
            return
        try:
            with self._cache_path(key).open("wb") as handle:
                pickle.dump(value, handle)
        except Exception as exc:
            logger.warning("写入行情缓存失败: %s", exc)

    def _daily_history_path(self, symbol: str) -> Path:
        return self.history_dir / f"{normalize_symbol(symbol)}_{self.adjust or 'none'}.pkl"

    def load_daily_history(self, symbol: str) -> pd.DataFrame:
        path = self._daily_history_path(symbol)
        if not path.exists():
            return pd.DataFrame(columns=STANDARD_COLUMNS)
        try:
            with path.open("rb") as handle:
                history = pickle.load(handle)
            if not isinstance(history, pd.DataFrame):
                raise ValueError("缓存内容不是 DataFrame")
            return history
        except Exception as exc:
            logger.warning("读取 %s 日线历史失败: %s", symbol, exc)
            return pd.DataFrame(columns=STANDARD_COLUMNS)

    def daily_history_is_usable(
        self,
        symbol: str,
        expected_trade_date: date,
        min_bars: int = 120,
    ) -> bool:
        history = self.load_daily_history(symbol)
        required = {"datetime", "open", "high", "low", "close"}
        if history.empty or not required.issubset(history.columns):
            return False
        if "is_closed" in history.columns:
            history = history[history["is_closed"].fillna(False).astype(bool)]
        prices = history[["open", "high", "low", "close"]].apply(
            pd.to_numeric,
            errors="coerce",
        )
        valid = prices.notna().all(axis=1) & prices.gt(0).all(axis=1)
        valid &= prices["high"] >= prices[["open", "close"]].max(axis=1)
        valid &= prices["low"] <= prices[["open", "close"]].min(axis=1)
        history = history[valid]
        if len(history) < min_bars:
            return False
        timestamps = pd.to_datetime(history["datetime"], errors="coerce").dropna()
        return not timestamps.empty and timestamps.iloc[-1].date() >= expected_trade_date

    def save_daily_history(self, symbol: str, frame: pd.DataFrame) -> None:
        path = self._daily_history_path(symbol)
        with path.open("wb") as handle:
            pickle.dump(frame, handle)

    def _latest_expected_trade_date(self) -> date:
        current_time = now_shanghai()
        today = current_time.date()
        trade_dates = self.get_trade_dates()
        eligible = [item for item in trade_dates if item <= today]
        if eligible:
            if current_time.time() < clock_time(15, 0) and today in eligible:
                eligible.remove(today)
            return max(eligible) if eligible else today
        current = today if current_time.time() >= clock_time(15, 0) else today - timedelta(days=1)
        while current.weekday() >= 5:
            current -= timedelta(days=1)
        return current

    def latest_expected_trade_date(self) -> date:
        return self._latest_expected_trade_date()

    def _listing_cutoff_date(self) -> date:
        expected = self._latest_expected_trade_date()
        if self.min_listing_trade_days <= 0:
            return expected
        trade_dates = sorted(item for item in self.get_trade_dates() if item <= expected)
        if len(trade_dates) >= self.min_listing_trade_days:
            return trade_dates[-self.min_listing_trade_days]
        fallback_days = int(self.min_listing_trade_days * 7 / 5) + 30
        return expected - timedelta(days=fallback_days)

    def refresh_daily_histories_from_snapshot(self) -> int:
        """Append today's batch snapshot to already bootstrapped daily histories."""
        current_time = now_shanghai()
        if current_time.time() < clock_time(15, 0):
            return 0
        if self.adjust not in ("", "none"):
            logger.info("复权模式下不把未复权批量快照写入历史，将按股票获取调整后日线")
            return 0
        self._throttle()
        if self.provider == "akshare":
            raw = self._get_akshare().stock_zh_a_spot_em()
            aliases = {
                "代码": "code",
                "名称": "name",
                "今开": "open",
                "最高": "high",
                "最低": "low",
                "最新价": "close",
                "成交量": "volume",
                "成交额": "amount",
            }
            snapshot = raw.rename(columns=aliases)
            snapshot_source = "akshare_snapshot"
        else:
            try:
                payload = self._http_json(
                    "https://82.push2.eastmoney.com/api/qt/clist/get",
                    {
                        "pn": 1,
                        "pz": 6000,
                        "po": 1,
                        "np": 1,
                        "fltt": 2,
                        "invt": 2,
                        "fid": "f3",
                        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                        "fields": "f12,f14,f2,f17,f15,f16,f5,f6",
                    },
                )
                items = (payload.get("data") or {}).get("diff") or []
                snapshot = pd.DataFrame(
                    [
                        {
                            "code": item.get("f12"),
                            "name": item.get("f14", ""),
                            "open": item.get("f17"),
                            "high": item.get("f15"),
                            "low": item.get("f16"),
                            "close": item.get("f2"),
                            "volume": item.get("f5"),
                            "amount": item.get("f6"),
                        }
                        for item in items
                    ]
                )
                snapshot_source = "eastmoney_snapshot"
            except Exception as exc:
                logger.warning("东方财富快照失败，降级新浪财经: %s", exc)
                items = self._fetch_sina_stock_snapshot()
                snapshot = pd.DataFrame(
                    [
                        {
                            "code": item.get("code"),
                            "name": item.get("name", ""),
                            "open": item.get("open"),
                            "high": item.get("high"),
                            "low": item.get("low"),
                            "close": item.get("trade"),
                            "volume": item.get("volume"),
                            "amount": item.get("amount"),
                        }
                        for item in items
                        if not str(item.get("symbol", "")).startswith("bj")
                    ]
                )
                snapshot_source = "sina_snapshot"
        required = {"code", "open", "high", "low", "close", "volume", "amount"}
        if not required.issubset(snapshot.columns):
            raise RuntimeError(f"全市场快照字段变化: 缺少 {sorted(required.difference(snapshot.columns))}")
        snapshot["code"] = snapshot["code"].map(normalize_symbol)
        updated = 0
        trade_date = pd.Timestamp(self._latest_expected_trade_date())
        for _, row in snapshot.iterrows():
            symbol = row["code"]
            path = self._daily_history_path(symbol)
            if not path.exists():
                continue
            numeric = pd.to_numeric(
                pd.Series([row["open"], row["high"], row["low"], row["close"], row["volume"], row["amount"]]),
                errors="coerce",
            )
            if numeric.iloc[:4].isna().any():
                continue
            open_price, high_price, low_price, close_price = map(float, numeric.iloc[:4])
            if min(open_price, high_price, low_price, close_price) <= 0:
                continue
            if high_price < max(open_price, close_price) or low_price > min(open_price, close_price):
                continue
            if high_price < low_price:
                continue
            if numeric.iloc[4:].notna().any() and (numeric.iloc[4:].dropna() < 0).any():
                continue
            history = self.load_daily_history(symbol)
            latest = pd.DataFrame(
                [
                    {
                        "datetime": trade_date,
                        "open": open_price,
                        "high": high_price,
                        "low": low_price,
                        "close": close_price,
                        "volume": 0.0 if pd.isna(numeric.iloc[4]) else float(numeric.iloc[4]),
                        "amount": 0.0 if pd.isna(numeric.iloc[5]) else float(numeric.iloc[5]),
                        "is_closed": True,
                    }
                ]
            )
            combined = pd.concat([history, latest], ignore_index=True)
            combined = combined.drop_duplicates("datetime", keep="last").sort_values("datetime")
            combined.attrs.update(
                {
                    "source": snapshot_source,
                    "timeframe": "1d",
                    "adjust": self.adjust,
                    "timezone": "Asia/Shanghai",
                    "timestamp_semantics": "end",
                }
            )
            self.save_daily_history(symbol, combined.reset_index(drop=True))
            updated += 1
        return updated

    def get_stock_list(self) -> pd.DataFrame:
        cache_key = f"stock_list_v3_{self.min_listing_trade_days}"
        cached = self._cached(cache_key, 24 * 3600)
        if cached is not None:
            return cached
        if self.provider == "tushare":
            try:
                pro = self._get_tushare()
                self._throttle()
                raw = pro.stock_basic(
                    exchange="",
                    list_status="L",
                    fields="ts_code,symbol,name,market,list_date",
                )
                result = raw.rename(columns={"symbol": "code"})
            except Exception as exc:
                if self.provider == "tushare":
                    raise
                logger.warning("Tushare 股票列表失败，降级 AkShare: %s", exc)
                result = self._get_akshare().stock_info_a_code_name().rename(
                    columns={"code": "code", "name": "name"}
                )
        elif self.provider == "akshare":
            self._throttle()
            result = self._get_akshare().stock_info_a_code_name().rename(
                columns={"code": "code", "name": "name"}
            )
        else:
            self._throttle()
            try:
                payload = self._http_json(
                    "https://82.push2.eastmoney.com/api/qt/clist/get",
                    {
                        "pn": 1,
                        "pz": 6000,
                        "po": 1,
                        "np": 1,
                        "fltt": 2,
                        "invt": 2,
                        "fid": "f3",
                        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                        "fields": "f12,f14,f26",
                    },
                )
                items = (payload.get("data") or {}).get("diff") or []
                result = pd.DataFrame(
                    [
                        {
                            "code": item.get("f12"),
                            "name": item.get("f14", ""),
                            "list_date": item.get("f26"),
                        }
                        for item in items
                    ]
                )
            except Exception as exc:
                logger.warning("东方财富股票列表失败，降级新浪财经: %s", exc)
                items = self._fetch_sina_stock_snapshot()
                result = pd.DataFrame(
                    [
                        {"code": item.get("code"), "name": item.get("name", "")}
                        for item in items
                        if not str(item.get("symbol", "")).startswith("bj")
                    ]
                )
        result["code"] = result["code"].map(normalize_symbol)
        result = result[~result["name"].astype(str).str.contains("ST|退", case=False, na=False)]
        if "list_date" in result.columns and self.min_listing_trade_days > 0:
            raw_dates = result["list_date"].astype(str).str.replace(r"\.0$", "", regex=True)
            listing_dates = pd.to_datetime(raw_dates, format="%Y%m%d", errors="coerce")
            cutoff = self._listing_cutoff_date()
            result = result[listing_dates.isna() | (listing_dates.dt.date <= cutoff)].copy()
        result = result.drop_duplicates("code").reset_index(drop=True)
        self._save_cache(cache_key, result)
        return result

    def get_index_bars(self, symbol: str = "000001.SH", limit: int = 300) -> pd.DataFrame:
        """Fetch closed daily index bars for the market-regime gate."""
        key = f"index_bars|{str(symbol).upper()}|1d|{limit}"
        cached = self._cached(key, 4 * 3600)
        if cached is not None:
            return cached.tail(limit).reset_index(drop=True)

        self._throttle()
        payload = self._http_json(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {
                "secid": eastmoney_index_secid(symbol),
                "klt": "101",
                "fqt": 0,
                "beg": 0,
                "end": "20500101",
                "lmt": max(limit, 300),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            },
        )
        klines = (payload.get("data") or {}).get("klines") or []
        rows = []
        for line in klines:
            fields = str(line).split(",")
            if len(fields) < 7:
                continue
            rows.append(
                {
                    "datetime": fields[0],
                    "open": fields[1],
                    "close": fields[2],
                    "high": fields[3],
                    "low": fields[4],
                    "volume": fields[5],
                    "amount": fields[6],
                }
            )
        frame = standardize_bars(pd.DataFrame(rows), "1d", "eastmoney-index", adjust="none")
        frame = frame[frame["is_closed"]].tail(limit).reset_index(drop=True)
        self._save_cache(key, frame)
        return frame

    def _fetch_akshare(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        ak = self._get_akshare()
        code = normalize_symbol(symbol)
        end = now_shanghai()
        self._throttle()
        if timeframe == "1d":
            start = (end - timedelta(days=max(limit * 2, 400))).strftime("%Y%m%d")
            raw = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start,
                end_date=end.strftime("%Y%m%d"),
                adjust=self.adjust,
            )
        else:
            period = str(TIMEFRAME_MINUTES[timeframe])
            lookback_days = 7 if period == "1" else max(30, int(limit * int(period) / 240 * 3))
            raw = ak.stock_zh_a_hist_min_em(
                symbol=code,
                start_date=(end - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S"),
                end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
                period=period,
                adjust=self.adjust,
            )
        return standardize_bars(
            raw,
            timeframe=timeframe,
            source="akshare",
            adjust=self.adjust,
            minute_timestamp=self.minute_timestamp,
        )

    def _fetch_eastmoney(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        klt = "101" if timeframe == "1d" else str(TIMEFRAME_MINUTES[timeframe])
        fqt = {"": 0, "none": 0, "qfq": 1, "hfq": 2}.get(self.adjust, 1)
        self._throttle()
        payload = self._http_json(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {
                "secid": eastmoney_secid(symbol),
                "klt": klt,
                "fqt": fqt,
                "beg": 0,
                "end": "20500101",
                "lmt": max(limit, 300),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            },
        )
        data = payload.get("data") or {}
        klines = data.get("klines") or []
        rows = []
        for line in klines:
            fields = str(line).split(",")
            if len(fields) < 7:
                continue
            rows.append(
                {
                    "datetime": fields[0],
                    "open": fields[1],
                    "close": fields[2],
                    "high": fields[3],
                    "low": fields[4],
                    "volume": fields[5],
                    "amount": fields[6],
                }
            )
        return standardize_bars(
            pd.DataFrame(rows),
            timeframe=timeframe,
            source="eastmoney",
            adjust=self.adjust,
            minute_timestamp="end",
        )

    def _fetch_tencent(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        stock = tencent_symbol(symbol)
        self._throttle()
        if timeframe == "1d":
            if self.adjust in ("", "none"):
                endpoint = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
                payload = self._http_json(
                    endpoint,
                    {"param": f"{stock},day,,,{max(limit, 300)}"},
                )
                data = (payload.get("data") or {}).get(stock) or {}
                items = data.get("day") or []
            else:
                adjust_prefix = "hfq" if self.adjust == "hfq" else "qfq"
                endpoint = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                payload = self._http_json(
                    endpoint,
                    {"param": f"{stock},day,,,{max(limit, 300)},{adjust_prefix}"},
                )
                data = (payload.get("data") or {}).get(stock) or {}
                items = data.get(f"{adjust_prefix}day") or data.get("day") or []
        else:
            if self.adjust not in ("", "none"):
                raise RuntimeError(
                    "腾讯分钟线不提供可靠复权；请把 market_data.adjust 设为 none，"
                    "或切换支持该口径的行情源"
                )
            period = f"m{TIMEFRAME_MINUTES[timeframe]}"
            endpoint = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
            payload = self._http_json(
                endpoint,
                {"param": f"{stock},{period},,{max(limit, 300)}"},
            )
            data = (payload.get("data") or {}).get(stock) or {}
            items = data.get(period) or []

        rows = []
        for fields in items:
            if len(fields) < 6:
                continue
            rows.append(
                {
                    "datetime": fields[0],
                    "open": fields[1],
                    "close": fields[2],
                    "high": fields[3],
                    "low": fields[4],
                    "volume": fields[5],
                    "amount": 0.0,
                }
            )
        return standardize_bars(
            pd.DataFrame(rows),
            timeframe=timeframe,
            source="tencent",
            adjust=self.adjust,
            minute_timestamp="end",
        )

    def _fetch_tushare_daily(self, symbol: str, limit: int) -> pd.DataFrame:
        pro = self._get_tushare()
        end = now_shanghai()
        start = end - timedelta(days=max(limit * 2, 400))
        self._throttle()
        raw = pro.daily(
            ts_code=tushare_symbol(symbol),
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        frame = standardize_bars(raw, "1d", "tushare", adjust="none")
        if self.adjust not in ("", "none"):
            raise RuntimeError("Tushare 当前适配未合并复权因子，禁止与 qfq 周期混用")
        return frame

    def get_bars(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        if timeframe not in {*TIMEFRAME_MINUTES, "1d"}:
            raise ValueError(f"不支持周期: {timeframe}")
        if timeframe == "120m":
            source = self.get_bars(symbol, "60m", limit=max(limit * 2, 80))
            return resample_session_bars(source, 120, source_minutes=60).tail(limit).reset_index(drop=True)

        if timeframe == "1d":
            history = self.load_daily_history(symbol)
            if not history.empty and len(history) >= min(limit, 120):
                last_date = pd.Timestamp(history["datetime"].iloc[-1]).date()
                if last_date >= self._latest_expected_trade_date():
                    return history[history["is_closed"]].tail(limit).reset_index(drop=True)

        ttl = 45 if timeframe != "1d" else 4 * 3600
        key = f"bars|{normalize_symbol(symbol)}|{timeframe}|{limit}|{self.adjust}"
        cached = self._cached(key, ttl)
        if cached is not None:
            return cached.tail(limit).reset_index(drop=True)

        if timeframe == "1d" and self.provider == "tushare":
            frame = self._fetch_tushare_daily(symbol, limit)
        elif self.provider == "akshare":
            frame = self._fetch_akshare(symbol, timeframe, limit)
        elif self.provider == "eastmoney":
            frame = self._fetch_eastmoney(symbol, timeframe, limit)
        else:
            frame = self._fetch_tencent(symbol, timeframe, limit)
        frame = frame[frame["is_closed"]].tail(limit).reset_index(drop=True)
        if frame.attrs.get("adjust") != self.adjust and self.adjust:
            raise RuntimeError(f"{symbol} {timeframe} 复权口径不一致")
        self._save_cache(key, frame)
        if timeframe == "1d" and not frame.empty:
            history = self.load_daily_history(symbol)
            combined = pd.concat([history, frame], ignore_index=True)
            combined = combined.drop_duplicates("datetime", keep="last").sort_values("datetime")
            combined.attrs.update(frame.attrs)
            self.save_daily_history(symbol, combined.reset_index(drop=True))
        return frame

    def get_multi_timeframe_bars(
        self,
        symbol: str,
        timeframes: list[str],
        limit: int = 300,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
        result: dict[str, pd.DataFrame] = {}
        errors: dict[str, str] = {}
        minute_frame: pd.DataFrame | None = None

        def prepare(
            frame: pd.DataFrame,
            source_mode: str,
            direct_error: str | None = None,
        ) -> pd.DataFrame:
            prepared = frame.tail(limit).reset_index(drop=True)
            prepared.attrs.update(frame.attrs)
            history_complete = len(prepared) >= limit
            warnings: list[str] = []
            if direct_error:
                warnings.append(f"直接获取失败: {direct_error}")
            if not history_complete:
                warnings.append(f"行情历史不足: 仅获取 {len(prepared)}/{limit} 根")
            prepared.attrs.update(
                requested_bars=limit,
                history_complete=history_complete,
                source_mode=source_mode,
                direct_error=direct_error,
                source_warning="; ".join(warnings) or None,
            )
            return prepared

        if "1m" in timeframes:
            try:
                minute_frame = self.get_bars(symbol, "1m", limit=limit)
                result["1m"] = prepare(minute_frame, "direct")
            except Exception as exc:
                errors["1m"] = str(exc)

        for timeframe in timeframes:
            if timeframe in result:
                continue
            try:
                if timeframe == "1d":
                    direct = self.get_bars(symbol, timeframe, limit=limit)
                    result[timeframe] = prepare(direct, "direct")
                    continue
                direct = self.get_bars(symbol, timeframe, limit=limit)
                if direct.empty:
                    raise RuntimeError(f"{timeframe} 行情为空")
                result[timeframe] = prepare(direct, "direct")
            except Exception as direct_exc:
                if timeframe == "1d":
                    errors[timeframe] = str(direct_exc)
                    continue
                try:
                    minutes = TIMEFRAME_MINUTES[timeframe]
                    # A 1m frame may already have been loaded for the requested
                    # 1m report.  Reuse it only when it is long enough to make
                    # the fallback period analyzable; otherwise fetch a larger
                    # window before resampling.  Forty derived bars is the
                    # minimum needed by MACD (26 + 9 + a small confirmation
                    # buffer), while the configured limit remains the target.
                    required_derived_bars = min(limit, 40)
                    required_minutes = max(limit, required_derived_bars * minutes)
                    if minute_frame is None or len(minute_frame) < required_minutes:
                        minute_frame = self.get_bars(
                            symbol,
                            "1m",
                            limit=required_minutes,
                        )
                    derived = resample_session_bars(minute_frame, minutes, source_minutes=1)
                    if len(derived) < min(40, limit):
                        raise RuntimeError(f"重采样后仅 {len(derived)} 根")
                    result[timeframe] = prepare(
                        derived,
                        "resampled_1m_fallback",
                        direct_error=str(direct_exc),
                    )
                except Exception as fallback_exc:
                    errors[timeframe] = f"直接获取失败: {direct_exc}; 1m重采样失败: {fallback_exc}"
        return result, errors

    def get_trade_dates(self) -> set[date]:
        cached = self._cached("trade_calendar", 24 * 3600)
        if cached is not None:
            return set(pd.to_datetime(cached["trade_date"]).dt.date)
        try:
            self._throttle()
            if self.provider == "akshare":
                raw = self._get_akshare().tool_trade_date_hist_sina()
                column = "trade_date" if "trade_date" in raw.columns else raw.columns[0]
                result = pd.DataFrame({"trade_date": pd.to_datetime(raw[column])})
            else:
                payload = self._http_json(
                    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                    {"param": "sh000001,day,,,1500,qfq"},
                )
                data = (payload.get("data") or {}).get("sh000001") or {}
                items = data.get("qfqday") or data.get("day") or []
                result = pd.DataFrame(
                    {"trade_date": pd.to_datetime([item[0] for item in items if item])}
                )
            self._save_cache("trade_calendar", result)
            return set(result["trade_date"].dt.date)
        except Exception as exc:
            logger.warning("交易日历获取失败，降级到工作日: %s", exc)
            return set()
