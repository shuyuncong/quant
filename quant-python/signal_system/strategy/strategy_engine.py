"""
策略引擎模块
整合基本面筛选、技术面分析、信号生成
"""

import pandas as pd
import numpy as np
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class StrategyEngine:
    """策略引擎"""

    def __init__(self, config, data_fetcher, technical_indicators):
        self.config = config
        self.data_fetcher = data_fetcher
        self.technical = technical_indicators

    def filter_by_fundamental(self, stock_list):
        """
        基本面筛选 - 三高一低过滤

        Args:
            stock_list: DataFrame, 股票列表

        Returns:
            list: 通过筛选的股票代码列表
        """
        logger.info(f"开始基本面筛选，候选股票: {len(stock_list)}")

        fundamental_config = self.config['strategy']['fundamental']
        passed_stocks = []

        for _, stock in stock_list.iterrows():
            ts_code = stock['ts_code']

            try:
                financial = self.data_fetcher.get_financial_data(ts_code)
                if financial is None:
                    continue

                daily_basic = self.data_fetcher.get_daily_basic(ts_code)
                if daily_basic is None:
                    continue

                roe = financial.get('roe', 0)
                debt_ratio = financial.get('debt_to_assets', 100)
                pe = daily_basic.get('pe', 999)
                market_cap = daily_basic.get('total_mv', 0) / 10000

                if roe < fundamental_config['min_roe']:
                    continue

                if debt_ratio > fundamental_config['max_debt_ratio']:
                    continue

                if pe > fundamental_config['max_pe'] or pe < 0:
                    continue

                if market_cap < fundamental_config['min_market_cap']:
                    continue

                if market_cap > fundamental_config['max_market_cap']:
                    continue

                passed_stocks.append({
                    'ts_code': ts_code,
                    'name': stock['name'],
                    'roe': roe,
                    'debt_ratio': debt_ratio,
                    'pe': pe,
                    'market_cap': market_cap
                })

            except Exception as e:
                logger.warning(f"筛选 {ts_code} 失败: {e}")
                continue

        logger.info(f"基本面筛选完成，通过: {len(passed_stocks)}")
        return passed_stocks

    def filter_by_volume(self, stock_codes):
        """
        成交量筛选

        Args:
            stock_codes: list, 股票代码列表

        Returns:
            list: 通过筛选的股票代码
        """
        logger.info(f"开始成交量筛选，候选股票: {len(stock_codes)}")

        volume_config = self.config['strategy']['volume']
        passed_stocks = []

        for stock_info in stock_codes:
            ts_code = stock_info['ts_code']

            try:
                df = self.data_fetcher.get_daily_data(ts_code, period=30)
                if df.empty or len(df) < 20:
                    continue

                if 'turnover_rate' in df.columns:
                    avg_turnover = df['turnover_rate'].tail(30).mean()
                else:
                    daily_basic = self.data_fetcher.get_daily_basic(ts_code)
                    if daily_basic is None:
                        continue
                    avg_turnover = daily_basic.get('turnover_rate', 0)

                if avg_turnover < volume_config['min_turnover_rate']:
                    continue

                if avg_turnover > volume_config['max_turnover_rate']:
                    continue

                stock_info['avg_turnover'] = avg_turnover
                passed_stocks.append(stock_info)

            except Exception as e:
                logger.warning(f"成交量筛选 {ts_code} 失败: {e}")
                continue

        logger.info(f"成交量筛选完成，通过: {len(passed_stocks)}")
        return passed_stocks

    def analyze_technical(self, stock_codes):
        """
        技术面分析

        Args:
            stock_codes: list, 股票信息列表

        Returns:
            list: 带技术分析结果的股票列表
        """
        logger.info(f"开始技术面分析，候选股票: {len(stock_codes)}")

        tech_config = self.config['strategy']['technical']
        analyzed_stocks = []

        for stock_info in stock_codes:
            ts_code = stock_info['ts_code']

            try:
                df = self.data_fetcher.get_daily_data(ts_code, period=300)
                if df.empty or len(df) < tech_config['ma_period']:
                    continue

                tech_result = self.technical.analyze_stock_technical(
                    df,
                    ma_period=tech_config['ma_period'],
                    macd_fast=tech_config['macd_fast'],
                    macd_slow=tech_config['macd_slow'],
                    macd_signal=tech_config['macd_signal']
                )

                if tech_result is None:
                    continue

                stock_info.update(tech_result)
                analyzed_stocks.append(stock_info)

            except Exception as e:
                logger.warning(f"技术分析 {ts_code} 失败: {e}")
                continue

        logger.info(f"技术面分析完成: {len(analyzed_stocks)}")
        return analyzed_stocks

    def judge_market_status(self):
        """
        判断市场环境

        Returns:
            str: 'bull' (牛市), 'sideways' (震荡), 'bear' (熊市)
        """
        try:
            df = self.data_fetcher.get_index_daily('000001.SH', period=300)
            if df.empty or len(df) < 250:
                return 'sideways'

            tech_result = self.technical.analyze_stock_technical(df, ma_period=250)
            if tech_result is None:
                return 'sideways'

            ma_slope = tech_result['ma250_slope']
            is_above_ma = tech_result['is_above_ma250']
            macd = tech_result['macd']

            if ma_slope > 0 and is_above_ma and macd > 0:
                return 'bull'
            elif ma_slope < 0 and not is_above_ma and macd < 0:
                return 'bear'
            else:
                return 'sideways'

        except Exception as e:
            logger.error(f"判断市场环境失败: {e}")
            return 'sideways'

    def generate_buy_signals(self, analyzed_stocks, market_status):
        """
        生成买入信号

        Args:
            analyzed_stocks: list, 技术分析后的股票列表
            market_status: str, 市场状态

        Returns:
            list: 买入信号列表
        """
        logger.info(f"生成买入信号，市场状态: {market_status}")

        tech_config = self.config['strategy']['technical']
        buy_signals = []

        for stock in analyzed_stocks:
            signals = []
            score = 0

            if stock['ma250_slope'] > 0:
                signals.append('年线向上')
                score += 3

            if stock['near_ma250'] and stock['is_above_ma250']:
                signals.append('回调至年线附近')
                score += 2

            if stock['divergence'] == 'bullish':
                signals.append('底背离')
                score += 3

            if stock['macd_golden_cross']:
                signals.append('MACD金叉')
                score += 2

            if stock['volume_ratio'] > 1.5:
                signals.append('放量')
                score += 1

            if market_status == 'bull' and score >= 5:
                buy_signals.append({
                    'ts_code': stock['ts_code'],
                    'name': stock['name'],
                    'price': stock['current_price'],
                    'signals': signals,
                    'score': score,
                    'roe': stock.get('roe', 0),
                    'pe': stock.get('pe', 0),
                    'market_cap': stock.get('market_cap', 0),
                    'reason': '战略买入点'
                })
            elif market_status == 'sideways' and score >= 6:
                buy_signals.append({
                    'ts_code': stock['ts_code'],
                    'name': stock['name'],
                    'price': stock['current_price'],
                    'signals': signals,
                    'score': score,
                    'roe': stock.get('roe', 0),
                    'pe': stock.get('pe', 0),
                    'market_cap': stock.get('market_cap', 0),
                    'reason': '震荡市买入'
                })

        buy_signals.sort(key=lambda x: x['score'], reverse=True)
        logger.info(f"生成买入信号: {len(buy_signals)} 个")

        return buy_signals[:10]

    def check_positions_for_sell(self, positions):
        """
        检查持仓是否需要卖出

        Args:
            positions: list, 持仓列表 [{ts_code, buy_price, buy_date}]

        Returns:
            list: 卖出信号列表
        """
        logger.info(f"检查持仓卖出信号，持仓数: {len(positions)}")

        risk_config = self.config['risk_control']
        sell_signals = []

        for pos in positions:
            ts_code = pos['ts_code']

            try:
                df = self.data_fetcher.get_daily_data(ts_code, period=100)
                if df.empty:
                    continue

                tech_result = self.technical.analyze_stock_technical(df)
                if tech_result is None:
                    continue

                current_price = tech_result['current_price']
                buy_price = pos['buy_price']
                profit_pct = (current_price - buy_price) / buy_price

                reasons = []

                if profit_pct < -risk_config['stop_loss']:
                    reasons.append(f'止损 ({profit_pct*100:.2f}%)')

                if profit_pct > risk_config['stop_profit']:
                    reasons.append(f'止盈 ({profit_pct*100:.2f}%)')

                if tech_result['divergence'] == 'bearish':
                    reasons.append('顶背离')

                if not tech_result['is_above_ma250'] and tech_result['ma250_slope'] < 0:
                    reasons.append('破年线')

                if reasons:
                    sell_signals.append({
                        'ts_code': ts_code,
                        'name': pos.get('name', ''),
                        'buy_price': buy_price,
                        'current_price': current_price,
                        'profit_pct': profit_pct,
                        'reasons': reasons
                    })

            except Exception as e:
                logger.warning(f"检查 {ts_code} 卖出信号失败: {e}")
                continue

        logger.info(f"生成卖出信号: {len(sell_signals)} 个")
        return sell_signals

    def run_daily_scan(self, positions=None):
        """
        执行每日扫描

        Args:
            positions: list, 当前持仓

        Returns:
            dict: 扫描结果
        """
        logger.info("=" * 50)
        logger.info("开始每日扫描")
        logger.info("=" * 50)

        market_status = self.judge_market_status()
        logger.info(f"市场状态: {market_status}")

        stock_list = self.data_fetcher.get_stock_list()
        if stock_list.empty:
            logger.error("获取股票列表失败")
            return None

        fundamental_passed = self.filter_by_fundamental(stock_list)

        volume_passed = self.filter_by_volume(fundamental_passed)

        analyzed_stocks = self.analyze_technical(volume_passed)

        buy_signals = self.generate_buy_signals(analyzed_stocks, market_status)

        sell_signals = []
        if positions:
            sell_signals = self.check_positions_for_sell(positions)

        result = {
            'scan_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'market_status': market_status,
            'buy_signals': buy_signals,
            'sell_signals': sell_signals,
            'stats': {
                'total_stocks': len(stock_list),
                'fundamental_passed': len(fundamental_passed),
                'volume_passed': len(volume_passed),
                'technical_analyzed': len(analyzed_stocks),
                'buy_signals_count': len(buy_signals),
                'sell_signals_count': len(sell_signals)
            }
        }

        logger.info("=" * 50)
        logger.info("每日扫描完成")
        logger.info("=" * 50)

        return result
