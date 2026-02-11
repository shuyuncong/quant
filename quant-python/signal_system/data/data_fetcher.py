"""
数据获取模块
支持 Tushare 和 AkShare 数据源
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
import os
import pickle

logger = logging.getLogger(__name__)


class DataFetcher:
    """数据获取器"""

    def __init__(self, tushare_token=None, use_cache=True, cache_dir='./cache'):
        self.tushare_token = tushare_token
        self.use_cache = use_cache
        self.cache_dir = cache_dir

        if use_cache and not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        self.pro = None
        if tushare_token:
            try:
                import tushare as ts
                ts.set_token(tushare_token)
                self.pro = ts.pro_api()
                logger.info("Tushare API 初始化成功")
            except Exception as e:
                logger.error(f"Tushare API 初始化失败: {e}")

    def _get_cache_path(self, cache_key):
        """获取缓存文件路径"""
        return os.path.join(self.cache_dir, f"{cache_key}.pkl")

    def _load_cache(self, cache_key, max_age_hours=24):
        """加载缓存数据"""
        if not self.use_cache:
            return None

        cache_path = self._get_cache_path(cache_key)
        if not os.path.exists(cache_path):
            return None

        file_age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache_path))
        if file_age > timedelta(hours=max_age_hours):
            return None

        try:
            with open(cache_path, 'rb') as f:
                return pickle.load(f)
        except Exception as e:
            logger.warning(f"加载缓存失败: {e}")
            return None

    def _save_cache(self, cache_key, data):
        """保存缓存数据"""
        if not self.use_cache:
            return

        cache_path = self._get_cache_path(cache_key)
        try:
            with open(cache_path, 'wb') as f:
                pickle.dump(data, f)
        except Exception as e:
            logger.warning(f"保存缓存失败: {e}")

    def get_stock_list(self, exchange='', list_status='L'):
        """
        获取股票列表

        Args:
            exchange: 交易所代码 ('' 表示全部)
            list_status: 上市状态 (L=上市, D=退市, P=暂停上市)

        Returns:
            DataFrame: 股票列表
        """
        cache_key = f"stock_list_{exchange}_{list_status}"
        cached_data = self._load_cache(cache_key, max_age_hours=24)

        if cached_data is not None:
            logger.info(f"从缓存加载股票列表: {len(cached_data)} 只")
            return cached_data

        if self.pro is None:
            logger.error("Tushare API 未初始化")
            return pd.DataFrame()

        try:
            df = self.pro.stock_basic(
                exchange=exchange,
                list_status=list_status,
                fields='ts_code,symbol,name,area,industry,market,list_date'
            )

            df = df[~df['name'].str.contains('ST|退', na=False)]

            self._save_cache(cache_key, df)
            logger.info(f"获取股票列表成功: {len(df)} 只")
            return df

        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            return pd.DataFrame()

    def get_daily_data(self, ts_code, start_date=None, end_date=None, period=250):
        """
        获取日线数据

        Args:
            ts_code: 股票代码
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            period: 获取天数 (如果不指定日期)

        Returns:
            DataFrame: 日线数据
        """
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=period*2)).strftime('%Y%m%d')
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')

        cache_key = f"daily_{ts_code}_{start_date}_{end_date}"
        cached_data = self._load_cache(cache_key, max_age_hours=6)

        if cached_data is not None:
            return cached_data

        if self.pro is None:
            logger.error("Tushare API 未初始化")
            return pd.DataFrame()

        try:
            df = self.pro.daily(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date
            )

            if df.empty:
                return df

            df = df.sort_values('trade_date').reset_index(drop=True)

            self._save_cache(cache_key, df)
            return df

        except Exception as e:
            logger.error(f"获取 {ts_code} 日线数据失败: {e}")
            return pd.DataFrame()

    def get_financial_data(self, ts_code, period=None):
        """
        获取财务数据

        Args:
            ts_code: 股票代码
            period: 报告期 (YYYYMMDD), None 表示最新

        Returns:
            dict: 财务数据
        """
        if period is None:
            period = self._get_latest_report_period()

        cache_key = f"financial_{ts_code}_{period}"
        cached_data = self._load_cache(cache_key, max_age_hours=24*7)

        if cached_data is not None:
            return cached_data

        if self.pro is None:
            logger.error("Tushare API 未初始化")
            return None

        try:
            df = self.pro.fina_indicator(
                ts_code=ts_code,
                period=period,
                fields='ts_code,end_date,roe,debt_to_assets,pe,pb,current_ratio,quick_ratio'
            )

            if df.empty:
                return None

            result = df.iloc[0].to_dict()
            self._save_cache(cache_key, result)
            return result

        except Exception as e:
            logger.error(f"获取 {ts_code} 财务数据失败: {e}")
            return None

    def get_daily_basic(self, ts_code, trade_date=None):
        """
        获取每日指标数据 (市值、换手率等)

        Args:
            ts_code: 股票代码
            trade_date: 交易日期 (YYYYMMDD), None 表示最新

        Returns:
            dict: 每日指标数据
        """
        if trade_date is None:
            trade_date = self._get_latest_trade_date()

        cache_key = f"daily_basic_{ts_code}_{trade_date}"
        cached_data = self._load_cache(cache_key, max_age_hours=6)

        if cached_data is not None:
            return cached_data

        if self.pro is None:
            logger.error("Tushare API 未初始化")
            return None

        try:
            df = self.pro.daily_basic(
                ts_code=ts_code,
                trade_date=trade_date,
                fields='ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio,pe,pb,total_mv,circ_mv'
            )

            if df.empty:
                return None

            result = df.iloc[0].to_dict()
            self._save_cache(cache_key, result)
            return result

        except Exception as e:
            logger.error(f"获取 {ts_code} 每日指标失败: {e}")
            return None

    def get_index_daily(self, ts_code='000001.SH', start_date=None, end_date=None, period=250):
        """
        获取指数日线数据

        Args:
            ts_code: 指数代码 (默认上证指数)
            start_date: 开始日期
            end_date: 结束日期
            period: 获取天数

        Returns:
            DataFrame: 指数日线数据
        """
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=period*2)).strftime('%Y%m%d')
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')

        cache_key = f"index_daily_{ts_code}_{start_date}_{end_date}"
        cached_data = self._load_cache(cache_key, max_age_hours=6)

        if cached_data is not None:
            return cached_data

        if self.pro is None:
            logger.error("Tushare API 未初始化")
            return pd.DataFrame()

        try:
            df = self.pro.index_daily(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date
            )

            if df.empty:
                return df

            df = df.sort_values('trade_date').reset_index(drop=True)

            self._save_cache(cache_key, df)
            return df

        except Exception as e:
            logger.error(f"获取指数 {ts_code} 数据失败: {e}")
            return pd.DataFrame()

    def _get_latest_report_period(self):
        """获取最新财报期"""
        now = datetime.now()
        year = now.year
        month = now.month

        if month >= 10:
            return f"{year}0930"
        elif month >= 7:
            return f"{year}0630"
        elif month >= 4:
            return f"{year}0331"
        else:
            return f"{year-1}0930"

    def _get_latest_trade_date(self):
        """获取最新交易日"""
        if self.pro is None:
            return datetime.now().strftime('%Y%m%d')

        try:
            df = self.pro.trade_cal(
                exchange='SSE',
                start_date=(datetime.now() - timedelta(days=7)).strftime('%Y%m%d'),
                end_date=datetime.now().strftime('%Y%m%d'),
                is_open='1'
            )

            if df.empty:
                return datetime.now().strftime('%Y%m%d')

            return df.iloc[-1]['cal_date']

        except Exception as e:
            logger.warning(f"获取最新交易日失败: {e}")
            return datetime.now().strftime('%Y%m%d')
