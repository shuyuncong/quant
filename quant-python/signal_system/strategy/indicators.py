"""
技术指标计算模块
实现 MACD、均线、背离检测等核心指标
"""

import pandas as pd
import numpy as np
import talib
from scipy.signal import find_peaks
import logging

logger = logging.getLogger(__name__)


class TechnicalIndicators:
    """技术指标计算器"""

    @staticmethod
    def calculate_ma(data, period=250):
        """
        计算移动平均线

        Args:
            data: Series, 价格数据
            period: int, 周期

        Returns:
            Series: 移动平均线
        """
        if len(data) < period:
            return pd.Series([np.nan] * len(data), index=data.index)

        return talib.SMA(data.values, timeperiod=period)

    @staticmethod
    def calculate_macd(data, fast=12, slow=26, signal=9):
        """
        计算 MACD 指标

        Args:
            data: Series, 价格数据
            fast: int, 快线周期
            slow: int, 慢线周期
            signal: int, 信号线周期

        Returns:
            tuple: (macd, signal, hist)
        """
        if len(data) < slow:
            return None, None, None

        macd, signal_line, hist = talib.MACD(
            data.values,
            fastperiod=fast,
            slowperiod=slow,
            signalperiod=signal
        )

        return macd, signal_line, hist

    @staticmethod
    def calculate_rsi(data, period=14):
        """
        计算 RSI 指标

        Args:
            data: Series, 价格数据
            period: int, 周期

        Returns:
            Series: RSI 值
        """
        if len(data) < period:
            return pd.Series([np.nan] * len(data), index=data.index)

        return talib.RSI(data.values, timeperiod=period)

    @staticmethod
    def detect_divergence(price, indicator, lookback=60, min_distance=10):
        """
        检测背离信号

        Args:
            price: Series, 价格数据
            indicator: Series, 指标数据 (如 MACD hist)
            lookback: int, 回看周期
            min_distance: int, 峰值最小间隔

        Returns:
            str: 'bullish' (底背离), 'bearish' (顶背离), 'none' (无背离)
        """
        if len(price) < lookback or len(indicator) < lookback:
            return 'none'

        recent_price = price.iloc[-lookback:].values
        recent_indicator = indicator.iloc[-lookback:].values

        price_peaks, _ = find_peaks(recent_price, distance=min_distance)
        price_troughs, _ = find_peaks(-recent_price, distance=min_distance)

        indicator_peaks, _ = find_peaks(recent_indicator, distance=min_distance)
        indicator_troughs, _ = find_peaks(-recent_indicator, distance=min_distance)

        if len(price_troughs) >= 2 and len(indicator_troughs) >= 2:
            last_price_trough = price_troughs[-1]
            prev_price_trough = price_troughs[-2]
            last_ind_trough = indicator_troughs[-1]
            prev_ind_trough = indicator_troughs[-2]

            if (recent_price[last_price_trough] < recent_price[prev_price_trough] and
                recent_indicator[last_ind_trough] > recent_indicator[prev_ind_trough]):
                return 'bullish'

        if len(price_peaks) >= 2 and len(indicator_peaks) >= 2:
            last_price_peak = price_peaks[-1]
            prev_price_peak = price_peaks[-2]
            last_ind_peak = indicator_peaks[-1]
            prev_ind_peak = indicator_peaks[-2]

            if (recent_price[last_price_peak] > recent_price[prev_price_peak] and
                recent_indicator[last_ind_peak] < recent_indicator[prev_ind_peak]):
                return 'bearish'

        return 'none'

    @staticmethod
    def calculate_ma_slope(ma_data, period=20):
        """
        计算均线斜率

        Args:
            ma_data: Series, 均线数据
            period: int, 计算斜率的周期

        Returns:
            float: 斜率值
        """
        if len(ma_data) < period:
            return 0

        recent_ma = ma_data.iloc[-period:].values
        x = np.arange(len(recent_ma))
        slope = np.polyfit(x, recent_ma, 1)[0]

        return slope

    @staticmethod
    def is_near_ma(price, ma, threshold=0.05):
        """
        判断价格是否接近均线

        Args:
            price: float, 当前价格
            ma: float, 均线值
            threshold: float, 阈值比例

        Returns:
            bool: 是否接近
        """
        if ma == 0 or np.isnan(ma):
            return False

        distance = abs(price - ma) / ma
        return distance < threshold

    @staticmethod
    def calculate_volume_ratio(volume, avg_volume):
        """
        计算量比

        Args:
            volume: float, 当前成交量
            avg_volume: float, 平均成交量

        Returns:
            float: 量比
        """
        if avg_volume == 0 or np.isnan(avg_volume):
            return 1.0

        return volume / avg_volume

    @staticmethod
    def analyze_stock_technical(df, ma_period=250, macd_fast=12, macd_slow=26, macd_signal=9):
        """
        综合分析股票技术指标

        Args:
            df: DataFrame, 包含 OHLCV 数据
            ma_period: int, 均线周期
            macd_fast: int, MACD 快线
            macd_slow: int, MACD 慢线
            macd_signal: int, MACD 信号线

        Returns:
            dict: 技术分析结果
        """
        if df.empty or len(df) < ma_period:
            return None

        try:
            close = df['close']
            volume = df['volume'] if 'volume' in df.columns else df['vol']

            ma = TechnicalIndicators.calculate_ma(close, ma_period)
            macd, signal_line, hist = TechnicalIndicators.calculate_macd(
                close, macd_fast, macd_slow, macd_signal
            )

            if ma is None or macd is None:
                return None

            current_price = close.iloc[-1]
            current_ma = ma[-1]
            current_macd = macd[-1]
            current_hist = hist[-1]
            current_volume = volume.iloc[-1]

            ma_slope = TechnicalIndicators.calculate_ma_slope(pd.Series(ma), period=20)

            near_ma = TechnicalIndicators.is_near_ma(current_price, current_ma)

            divergence = TechnicalIndicators.detect_divergence(
                close,
                pd.Series(hist),
                lookback=60
            )

            avg_volume = volume.iloc[-30:].mean()
            volume_ratio = TechnicalIndicators.calculate_volume_ratio(
                current_volume,
                avg_volume
            )

            return {
                'current_price': current_price,
                'ma250': current_ma,
                'ma250_slope': ma_slope,
                'distance_to_ma250': abs(current_price - current_ma) / current_ma if current_ma > 0 else 0,
                'near_ma250': near_ma,
                'macd': current_macd,
                'macd_signal': signal_line[-1],
                'macd_hist': current_hist,
                'divergence': divergence,
                'volume_ratio': volume_ratio,
                'is_above_ma250': current_price > current_ma,
                'macd_golden_cross': current_macd > signal_line[-1] and macd[-2] <= signal_line[-2],
                'macd_death_cross': current_macd < signal_line[-1] and macd[-2] >= signal_line[-2]
            }

        except Exception as e:
            logger.error(f"技术分析失败: {e}")
            return None
