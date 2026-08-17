"""Run a real-data acceptance flow with public market data."""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SIGNAL_SYSTEM_ROOT = ROOT / "signal_system"
for candidate in (str(ROOT), str(SIGNAL_SYSTEM_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

for proxy_key in (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
):
    os.environ.pop(proxy_key, None)

from backtest.engine.bt_engine import BacktestEngine
from backtest.strategies.chan_zero_axis_bt import ChanZeroAxisBacktestStrategy
from backtest.strategies.trend_following_bt import TrendFollowingBacktestStrategy
from core.indicators.divergence import DivergenceDetector
from strategy.strategy_engine import StrategyEngine
from utils.helpers import load_config


STOCK_POOL = [
    {
        "ts_code": "000333.SZ",
        "symbol": "000333",
        "name": "Midea Group",
        "roe": 24.0,
        "debt_to_assets": 63.0,
        "pe": 15.0,
    },
    {
        "ts_code": "600519.SH",
        "symbol": "600519",
        "name": "Kweichow Moutai",
        "roe": 35.0,
        "debt_to_assets": 19.0,
        "pe": 28.0,
    },
    {
        "ts_code": "600036.SH",
        "symbol": "600036",
        "name": "China Merchants Bank",
        "roe": 16.0,
        "debt_to_assets": 92.0,
        "pe": 8.0,
    },
    {
        "ts_code": "601318.SH",
        "symbol": "601318",
        "name": "Ping An",
        "roe": 11.0,
        "debt_to_assets": 88.0,
        "pe": 9.0,
    },
    {
        "ts_code": "600900.SH",
        "symbol": "600900",
        "name": "Yangtze Power",
        "roe": 13.0,
        "debt_to_assets": 64.0,
        "pe": 21.0,
    },
    {
        "ts_code": "600276.SH",
        "symbol": "600276",
        "name": "Hengrui Medicine",
        "roe": 15.0,
        "debt_to_assets": 13.0,
        "pe": 45.0,
    },
    {
        "ts_code": "600887.SH",
        "symbol": "600887",
        "name": "Yili",
        "roe": 20.0,
        "debt_to_assets": 58.0,
        "pe": 22.0,
    },
    {
        "ts_code": "601899.SH",
        "symbol": "601899",
        "name": "Zijin Mining",
        "roe": 16.0,
        "debt_to_assets": 59.0,
        "pe": 18.0,
    },
    {
        "ts_code": "600031.SH",
        "symbol": "600031",
        "name": "Sany Heavy",
        "roe": 15.0,
        "debt_to_assets": 58.0,
        "pe": 17.0,
    },
    {
        "ts_code": "600309.SH",
        "symbol": "600309",
        "name": "Wanhua Chemical",
        "roe": 18.0,
        "debt_to_assets": 61.0,
        "pe": 14.0,
    },
    {
        "ts_code": "600438.SH",
        "symbol": "600438",
        "name": "Tongwei",
        "roe": 9.0,
        "debt_to_assets": 67.0,
        "pe": 18.0,
    },
    {
        "ts_code": "600690.SH",
        "symbol": "600690",
        "name": "Haier Smart Home",
        "roe": 17.0,
        "debt_to_assets": 58.0,
        "pe": 14.0,
    },
    {
        "ts_code": "601012.SH",
        "symbol": "601012",
        "name": "LONGi",
        "roe": 8.0,
        "debt_to_assets": 57.0,
        "pe": 20.0,
    },
    {
        "ts_code": "601888.SH",
        "symbol": "601888",
        "name": "China Tourism Group Duty Free",
        "roe": 18.0,
        "debt_to_assets": 41.0,
        "pe": 24.0,
    },
    {
        "ts_code": "300750.SZ",
        "symbol": "300750",
        "name": "CATL",
        "roe": 18.0,
        "debt_to_assets": 67.0,
        "pe": 24.0,
    },
    {
        "ts_code": "002594.SZ",
        "symbol": "002594",
        "name": "BYD",
        "roe": 20.0,
        "debt_to_assets": 76.0,
        "pe": 22.0,
    },
    {
        "ts_code": "002475.SZ",
        "symbol": "002475",
        "name": "Luxshare",
        "roe": 16.0,
        "debt_to_assets": 63.0,
        "pe": 26.0,
    },
    {
        "ts_code": "000858.SZ",
        "symbol": "000858",
        "name": "Wuliangye",
        "roe": 24.0,
        "debt_to_assets": 28.0,
        "pe": 18.0,
    },
    {
        "ts_code": "002415.SZ",
        "symbol": "002415",
        "name": "Hikvision",
        "roe": 17.0,
        "debt_to_assets": 42.0,
        "pe": 20.0,
    },
    {
        "ts_code": "000651.SZ",
        "symbol": "000651",
        "name": "Gree Electric",
        "roe": 24.0,
        "debt_to_assets": 66.0,
        "pe": 9.0,
    },
    {
        "ts_code": "000725.SZ",
        "symbol": "000725",
        "name": "BOE A",
        "roe": 6.0,
        "debt_to_assets": 49.0,
        "pe": 24.0,
    },
    {
        "ts_code": "000063.SZ",
        "symbol": "000063",
        "name": "ZTE",
        "roe": 11.0,
        "debt_to_assets": 66.0,
        "pe": 19.0,
    },
    {
        "ts_code": "300760.SZ",
        "symbol": "300760",
        "name": "Mindray",
        "roe": 28.0,
        "debt_to_assets": 29.0,
        "pe": 28.0,
    },
    {
        "ts_code": "300059.SZ",
        "symbol": "300059",
        "name": "East Money",
        "roe": 12.0,
        "debt_to_assets": 71.0,
        "pe": 29.0,
    },
    {
        "ts_code": "603259.SH",
        "symbol": "603259",
        "name": "Wuxi AppTec",
        "roe": 14.0,
        "debt_to_assets": 39.0,
        "pe": 17.0,
    },
    {
        "ts_code": "603288.SH",
        "symbol": "603288",
        "name": "Foshan Haitian",
        "roe": 22.0,
        "debt_to_assets": 21.0,
        "pe": 31.0,
    },
    {
        "ts_code": "601328.SH",
        "symbol": "601328",
        "name": "Bank of Communications",
        "roe": 10.0,
        "debt_to_assets": 93.0,
        "pe": 6.0,
    },
    {
        "ts_code": "601668.SH",
        "symbol": "601668",
        "name": "China State Construction",
        "roe": 13.0,
        "debt_to_assets": 76.0,
        "pe": 5.0,
    },
    {
        "ts_code": "600809.SH",
        "symbol": "600809",
        "name": "Shanxi Fenjiu",
        "roe": 33.0,
        "debt_to_assets": 34.0,
        "pe": 22.0,
    },
]


class PandasTechnicalIndicators:
    """TA-Lib-free technical indicators for acceptance validation."""

    def __init__(self):
        self.divergence_detector = DivergenceDetector()

    @staticmethod
    def calculate_ma(data: pd.Series, period: int = 250) -> pd.Series:
        return data.rolling(period).mean()

    @staticmethod
    def calculate_macd(data: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
        if len(data) < slow:
            return None, None, None
        ema_fast = data.ewm(span=fast, adjust=False).mean()
        ema_slow = data.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        signal_line = macd.ewm(span=signal, adjust=False).mean()
        hist = macd - signal_line
        return macd, signal_line, hist

    @staticmethod
    def calculate_ma_slope(ma_data: pd.Series, period: int = 20) -> float:
        if len(ma_data.dropna()) < period:
            return 0.0
        recent = ma_data.dropna().iloc[-period:]
        x = pd.Series(range(len(recent)), dtype=float)
        return float(((x - x.mean()) * (recent - recent.mean())).sum() / ((x - x.mean()) ** 2).sum())

    @staticmethod
    def is_near_ma(price: float, ma: float, threshold: float = 0.05) -> bool:
        if not ma or pd.isna(ma):
            return False
        return abs(price - ma) / ma < threshold

    @staticmethod
    def calculate_volume_ratio(volume: float, avg_volume: float) -> float:
        if not avg_volume or pd.isna(avg_volume):
            return 1.0
        return float(volume / avg_volume)

    def detect_divergence(self, price: pd.Series, indicator: pd.Series, lookback: int = 60) -> str:
        if len(price) < lookback or len(indicator) < lookback:
            return "none"
        return self.divergence_detector.classify(price.iloc[-lookback:], indicator.iloc[-lookback:])

    def analyze_stock_technical(
        self,
        df: pd.DataFrame,
        ma_period: int = 250,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
    ) -> Optional[Dict]:
        if df.empty or len(df) < ma_period:
            return None

        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        ma = self.calculate_ma(close, ma_period)
        macd, signal_line, hist = self.calculate_macd(close, macd_fast, macd_slow, macd_signal)
        if macd is None or signal_line is None or hist is None:
            return None

        current_price = float(close.iloc[-1])
        current_ma = float(ma.iloc[-1])
        current_macd = float(macd.iloc[-1])
        current_signal = float(signal_line.iloc[-1])
        current_hist = float(hist.iloc[-1])
        current_volume = float(volume.iloc[-1])
        avg_volume = float(volume.iloc[-30:].mean())
        divergence = self.detect_divergence(close, hist, lookback=60)

        return {
            "current_price": current_price,
            "ma250": current_ma,
            "ma250_slope": self.calculate_ma_slope(ma, period=20),
            "distance_to_ma250": abs(current_price - current_ma) / current_ma if current_ma > 0 else 0.0,
            "near_ma250": self.is_near_ma(current_price, current_ma),
            "macd": current_macd,
            "macd_signal": current_signal,
            "macd_hist": current_hist,
            "divergence": divergence,
            "volume_ratio": self.calculate_volume_ratio(current_volume, avg_volume),
            "is_above_ma250": current_price > current_ma,
            "macd_golden_cross": current_macd > current_signal and float(macd.iloc[-2]) <= float(signal_line.iloc[-2]),
            "macd_death_cross": current_macd < current_signal and float(macd.iloc[-2]) >= float(signal_line.iloc[-2]),
        }


class AkshareAcceptanceFetcher:
    """Real-data fetcher backed by AkShare single-symbol endpoints."""

    def __init__(self, stock_pool: List[Dict], start_date: str, end_date: str):
        self.stock_pool = stock_pool
        self.start_date = start_date
        self.end_date = end_date
        self.stock_meta = {item["ts_code"]: item for item in stock_pool}
        self.hist_cache: Dict[str, pd.DataFrame] = {}
        self.info_cache: Dict[str, Dict] = {}
        self.index_cache: Optional[pd.DataFrame] = None

    def get_stock_list(self):
        return pd.DataFrame(
            [{"ts_code": item["ts_code"], "name": item["name"]} for item in self.stock_pool]
        )

    def get_financial_data(self, ts_code, period=None):
        del period
        meta = self.stock_meta[ts_code]
        return {
            "roe": meta["roe"],
            "debt_to_assets": meta["debt_to_assets"],
        }

    def get_daily_basic(self, ts_code, trade_date=None):
        del trade_date
        meta = self.stock_meta[ts_code]
        hist = self.get_daily_data(ts_code, period=300)
        latest = hist.iloc[-1]
        total_mv_wan = float(latest["close"]) * float(latest["outstanding_share"]) / 10000.0
        return {
            "turnover_rate": float(latest["turnover_rate"]),
            "pe": float(meta["pe"]),
            "total_mv": total_mv_wan,
            "volume_ratio": float(hist["volume"].iloc[-1] / hist["volume"].tail(30).mean()),
        }

    def get_latest_trade_date(self) -> str:
        """Return the latest date actually present in the acceptance market data."""
        index_data = self.get_index_daily(start_date=self.start_date, end_date=self.end_date)
        if index_data.empty:
            raise RuntimeError("Unable to determine latest trade date from index data")
        return pd.Timestamp(index_data["datetime"].iloc[-1]).strftime("%Y%m%d")

    def align_end_date_to_latest_trade_date(self) -> str:
        """Close every acceptance data request on the same completed trading day."""
        latest_trade_date = self.get_latest_trade_date()
        self.end_date = latest_trade_date
        return latest_trade_date

    def _get_latest_report_period(self) -> str:
        """Return the latest completed quarter end for selector compatibility."""
        end = pd.Timestamp(self.end_date)
        current_quarter_start = end.to_period("Q").start_time
        return (current_quarter_start - pd.Timedelta(days=1)).strftime("%Y%m%d")

    def get_daily_data(self, ts_code, start_date=None, end_date=None, period=300):
        del period
        if ts_code in self.hist_cache:
            return self.hist_cache[ts_code].copy()

        market_symbol = self._to_market_symbol(ts_code)
        frame = ak.stock_zh_a_daily(
            symbol=market_symbol,
            start_date=start_date or self.start_date,
            end_date=end_date or self.end_date,
            adjust="qfq",
        )
        result = frame.rename(
            columns={
                "date": "datetime",
                "open": "open",
                "close": "close",
                "high": "high",
                "low": "low",
                "volume": "volume",
                "amount": "amount",
                "turnover": "turnover_rate",
                "outstanding_share": "outstanding_share",
            }
        )
        result["datetime"] = pd.to_datetime(result["datetime"])
        result["turnover_rate"] = result["turnover_rate"].astype(float) * 100.0
        result["price_change_pct"] = result["close"].astype(float).pct_change().fillna(0.0)
        result = result.sort_values("datetime").reset_index(drop=True)
        result.attrs["ts_code"] = ts_code
        self.hist_cache[ts_code] = result
        return result.copy()

    def get_index_daily(self, ts_code="000001.SH", start_date=None, end_date=None, period=300):
        del period, ts_code
        if self.index_cache is None:
            frame = ak.stock_zh_index_daily(symbol="sh000001")
            result = frame.rename(
                columns={
                    "date": "datetime",
                    "open": "open",
                    "close": "close",
                    "high": "high",
                    "low": "low",
                    "volume": "volume",
                }
            )
            result["datetime"] = pd.to_datetime(result["datetime"])
            result["price_change_pct"] = result["close"].astype(float).pct_change().fillna(0.0)
            self.index_cache = result.sort_values("datetime").reset_index(drop=True)
        result = self.index_cache.copy()
        result = result[
            (result["datetime"] >= pd.to_datetime(start_date or self.start_date))
            & (result["datetime"] <= pd.to_datetime(end_date or self.end_date))
        ]
        return result.sort_values("datetime").reset_index(drop=True)

    def _get_stock_info(self, ts_code: str) -> Dict:
        if ts_code in self.info_cache:
            return self.info_cache[ts_code]

        symbol = self.stock_meta[ts_code]["symbol"]
        frame = ak.stock_individual_info_em(symbol=symbol)
        info = dict(zip(frame["item"], frame["value"]))
        self.info_cache[ts_code] = {
            "latest_price": float(info["最新"]),
            "total_market_value_wan": float(info["总市值"]) / 10000.0,
            "industry": info.get("行业", ""),
        }
        return self.info_cache[ts_code]

    @staticmethod
    def _to_market_symbol(ts_code: str) -> str:
        symbol, market = ts_code.split(".")
        return ("sh" if market == "SH" else "sz") + symbol


def build_acceptance_config() -> Dict:
    config_path = ROOT / "signal_system" / "config" / "config.yaml"
    config = load_config(str(config_path))
    config = deepcopy(config)
    config["strategy"]["fundamental"]["max_market_cap"] = 20000
    config["strategy"]["fundamental"]["max_pe"] = 80
    config["strategy"]["fundamental"]["max_debt_ratio"] = 80
    config["strategy"]["volume"]["min_turnover_rate"] = 0.2
    config["strategy"]["volume"]["max_turnover_rate"] = 8
    config["strategy"]["volume"]["volume_burst_ratio"] = 0.8
    config["selector"]["market_cap_max"] = 20000
    config["selector"]["pe_acceptable_max"] = 80
    config["selector"]["debt_ratio_max"] = 80
    config["selector"]["turnover_rate_min"] = 0.2
    config["selector"]["turnover_rate_max"] = 8
    config["selector"]["volume_ratio_min"] = 0.8
    config["selector"]["price_change_soft_min"] = -0.08
    config["selector"]["price_change_soft_max"] = 0.08
    config["notification"]["wechat"]["enabled"] = False
    config["notification"]["email"]["enabled"] = False
    return config


def choose_backtest_symbol(scan_result: Dict) -> str:
    buy_signals = scan_result.get("buy_signals", [])
    if buy_signals:
        return buy_signals[0]["ts_code"]

    selected = scan_result.get("candidate_pool", [])
    if selected:
        data = selected[0].get("data", {})
        if data.get("ts_code"):
            return data["ts_code"]

    return STOCK_POOL[0]["ts_code"]


def ensure_output_dir() -> Path:
    date_tag = datetime.now().strftime("%Y%m%d")
    output_dir = ROOT / "backtest" / "acceptance" / "output" / date_tag
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def normalize_backtest_signals(raw_signals: list[dict], ts_code: str, strategy_name: str, regime: str) -> list[dict]:
    """Add the fields required by BacktestEngine while preserving strategy evidence."""
    return [
        {
            "datetime": signal["datetime"],
            "action": signal["action"],
            "position_pct": signal.get("position_pct", 0.95),
            "ts_code": ts_code,
            "signal_type": signal.get("signal_type", signal["action"]),
            "strategy_name": strategy_name,
            "regime": regime,
            "reason": signal.get("reason", ""),
        }
        for signal in raw_signals
    ]


def build_strategy_comparison(trend_summary: dict, chan_summary: dict) -> dict:
    """Compare win rates only when both strategies have completed trades."""
    completed_trades_comparable = (
        int(trend_summary.get("trade_count", 0)) > 0
        and int(chan_summary.get("trade_count", 0)) > 0
    )
    return {
        "trend_following": trend_summary,
        "chan_zero_axis": chan_summary,
        "comparison_status": (
            "comparable" if completed_trades_comparable else "insufficient_completed_trades"
        ),
        "completed_trade_win_rate_delta": (
            chan_summary.get("completed_trade_win_rate", chan_summary.get("win_rate", 0.0))
            - trend_summary.get("completed_trade_win_rate", trend_summary.get("win_rate", 0.0))
            if completed_trades_comparable
            else None
        ),
        "warning": "单一标的、固定区间的真实数据对比，只用于验收和研究，不能证明长期胜率提升。",
    }


def main() -> None:
    requested_end_date = datetime.now().strftime("%Y%m%d")
    start_date = "20240101"
    output_dir = ensure_output_dir()

    config = build_acceptance_config()
    fetcher = AkshareAcceptanceFetcher(
        STOCK_POOL,
        start_date=start_date,
        end_date=requested_end_date,
    )
    actual_end_date = fetcher.align_end_date_to_latest_trade_date()
    technical = PandasTechnicalIndicators()

    strategy_engine = StrategyEngine(
        config=config,
        data_fetcher=fetcher,
        technical_indicators=technical,
    )
    scan_result = strategy_engine.run_daily_scan(positions=[])

    scan_path = output_dir / "daily_scan_real_data.json"
    scan_path.write_text(json.dumps(scan_result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    backtest_symbol = choose_backtest_symbol(scan_result)
    price_data = fetcher.get_daily_data(backtest_symbol)
    bt_engine = BacktestEngine(config=config)
    trend_signals = normalize_backtest_signals(
        TrendFollowingBacktestStrategy(config).generate_signals(price_data),
        backtest_symbol,
        "trend_following",
        scan_result["market_status"],
    )
    chan_signals = normalize_backtest_signals(
        ChanZeroAxisBacktestStrategy(config).generate_signals(price_data),
        backtest_symbol,
        "chan_zero_axis",
        scan_result["market_status"],
    )
    backtest_result = bt_engine.run(
        price_data=price_data,
        signals=trend_signals,
        output_path=str(output_dir / "backtest_result.json"),
        strategy_name="trend_following",
        regime_scope=scan_result["market_status"],
        config_snapshot=config,
    )
    chan_backtest_result = bt_engine.run(
        price_data=price_data,
        signals=chan_signals,
        output_path=str(output_dir / "chan_zero_axis_backtest_result.json"),
        strategy_name="chan_zero_axis",
        regime_scope=scan_result["market_status"],
        config_snapshot=config,
    )

    parameter_scan = bt_engine.scan_parameters(
        price_data=price_data,
        strategy_cls=TrendFollowingBacktestStrategy,
        param_grid={
            "strategy.technical.ma_period": [120, 180, 250],
            "risk.stop_loss_pct": [0.06, 0.08],
        },
        base_config=config,
        strategy_name="trend_following",
        split_ratio=0.7,
        score_field="annual_return",
        regime_scope=scan_result["market_status"],
    )
    parameter_scan_path = output_dir / "parameter_scan_real_data.json"
    parameter_scan_path.write_text(
        json.dumps(parameter_scan, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    chan_parameter_scan = bt_engine.scan_parameters(
        price_data=price_data,
        strategy_cls=ChanZeroAxisBacktestStrategy,
        param_grid={
            "backtest.chan_zero_axis.min_confirmations": [1, 2, 3],
            "backtest.chan_zero_axis.cross_window_bars": [3, 5, 8],
        },
        base_config=config,
        strategy_name="chan_zero_axis",
        split_ratio=0.7,
        score_field="annual_return",
        regime_scope=scan_result["market_status"],
    )
    chan_parameter_scan_path = output_dir / "chan_zero_axis_parameter_scan_real_data.json"
    chan_parameter_scan_path.write_text(
        json.dumps(chan_parameter_scan, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    trend_summary = backtest_result["summary"]
    chan_summary = chan_backtest_result["summary"]
    strategy_comparison = build_strategy_comparison(trend_summary, chan_summary)

    acceptance_summary = {
        "run_date": datetime.now().isoformat(),
        "data_source": "akshare_public_endpoints",
        "data_period": {
            "start_date": start_date,
            "requested_end_date": requested_end_date,
            "actual_end_date": actual_end_date,
        },
        "stock_pool": [item["ts_code"] for item in STOCK_POOL],
        "market_status": scan_result["market_status"],
        "scan_stats": scan_result["stats"],
        "top_buy_signals": [
            {
                "ts_code": item["ts_code"],
                "signal_type": item.get("signal_type"),
                "score": item.get("score"),
                "reason": item.get("reason"),
            }
            for item in scan_result.get("buy_signals", [])[:3]
        ],
        "backtest_symbol": backtest_symbol,
        "backtest_summary": backtest_result["summary"],
        "chan_zero_axis_summary": chan_backtest_result["summary"],
        "strategy_comparison": strategy_comparison,
        "parameter_scan_best_params": parameter_scan.get("best_params", {}),
        "parameter_scan_best_comparison": parameter_scan.get("best_comparison", {}),
        "chan_zero_axis_parameter_scan_best_params": chan_parameter_scan.get("best_params", {}),
        "chan_zero_axis_parameter_scan_best_comparison": chan_parameter_scan.get("best_comparison", {}),
        "artifacts": {
            "daily_scan": str(scan_path),
            "backtest_result": str(output_dir / "backtest_result.json"),
            "backtest_report": backtest_result.get("output_files", {}).get("report_json"),
            "parameter_scan": str(parameter_scan_path),
            "chan_zero_axis_backtest_result": str(output_dir / "chan_zero_axis_backtest_result.json"),
            "chan_zero_axis_backtest_report": chan_backtest_result.get("output_files", {}).get("report_json"),
            "chan_zero_axis_parameter_scan": str(chan_parameter_scan_path),
        },
        "limitations": [
            "Daily and index prices are real public market data.",
            "The stock universe is a fixed acceptance watchlist, not the full market.",
            "Fundamental fields are acceptance inputs for the watchlist and are not fetched from Tushare.",
            "The strategy comparison is not a claim of improved win rate; expand symbols and periods, use rolling out-of-sample validation, and stress-test cost and exit assumptions before relying on it.",
        ],
    }
    summary_path = output_dir / "acceptance_summary.json"
    summary_path.write_text(
        json.dumps(acceptance_summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print(json.dumps(acceptance_summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
