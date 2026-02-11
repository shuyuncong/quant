import pandas as pd
import numpy as np
import tushare as ts
import talib as ta
from datetime import datetime, timedelta
import time
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 初始化Tushare
ts.set_token('你的Tushare Token')
pro = ts.pro_api()

class QualityMomentumStrategy:
    def __init__(self, initial_capital=500000, start_date='20220101', end_date=None):
        self.initial_capital = initial_capital  # 初始资金
        self.capital = initial_capital  # 当前资金
        self.start_date = start_date
        self.end_date = end_date if end_date else datetime.now().strftime('%Y%m%d')
        self.portfolio = {}  # 当前持仓 {股票代码: {'数量': 数量, '成本价': 成本价, '买入日期': 日期}}
        self.trade_history = []  # 交易历史
        self.daily_net_value = []  # 每日净值
        self.stock_pool = []  # 股票池
        
        # 策略参数
        self.max_stock_num = 20  # 最大持股数量
        self.single_position_limit = 0.1  # 单只股票最大仓位
        self.stop_loss = -0.12  # 止损线
        self.stop_profit = 0.3  # 止盈线
        self.holding_period_limit = 60  # 最长持有天数
        
        # 初始化
        self.initialize()
    
    def initialize(self):
        """初始化策略"""
        logger.info("初始化策略...")
        self.create_stock_pool()
        logger.info(f"股票池初始化完成，共有{len(self.stock_pool)}只股票")
    
    def create_stock_pool(self):
        """创建股票池"""
        # 获取所有A股股票
        all_stocks = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry,list_date,market')
        
        filtered_stocks = []
        for _, stock in all_stocks.iterrows():
            # 排除ST股票
            if 'ST' in stock['name'] or '*' in stock['name']:
                continue
            
            # 排除上市不足2年的股票
            list_date = datetime.strptime(stock['list_date'], '%Y%m%d')
            if (datetime.now() - list_date).days < 500:
                continue
            
            # 获取日均成交额
            try:
                daily_data = pro.daily(ts_code=stock['ts_code'], start_date=(datetime.now() - timedelta(days=30)).strftime('%Y%m%d'))
                if daily_data.empty:
                    continue
                avg_amount = daily_data['amount'].mean()
                
                # 排除流动性差的股票（日均成交额<1000万）
                if avg_amount < 10000000:
                    continue
                
                filtered_stocks.append(stock['ts_code'])
            except Exception as e:
                logger.error(f"获取{stock['ts_code']}数据出错: {e}")
                continue
        
        self.stock_pool = filtered_stocks
    
    def get_financial_data(self, stock_code):
        """获取财务数据"""
        try:
            # 获取最近的财务指标
            financial = pro.fina_indicator(ts_code=stock_code, period=self.get_latest_report_date())
            if financial.empty:
                return None
            
            # 获取前一年同期财务指标（用于同比分析）
            last_year = pro.fina_indicator(
                ts_code=stock_code, 
                period=self.get_last_year_same_period()
            )
            
            return {
                'current': financial.iloc[0],
                'last_year': None if last_year.empty else last_year.iloc[0]
            }
        except Exception as e:
            logger.error(f"获取{stock_code}财务数据出错: {e}")
            return None
    
    def get_latest_report_date(self):
        """获取最新的财报日期"""
        now = datetime.now()
        year = now.year
        month = now.month
        
        if month < 4:  # 1-3月，使用上一年三季报
            return f"{year-1}0930"
        elif month < 8:  # 4-7月，使用上一年年报
            return f"{year-1}1231"
        elif month < 11:  # 8-10月，使用当年半年报
            return f"{year}0630"
        else:  # 11-12月，使用当年三季报
            return f"{year}0930"
    
    def get_last_year_same_period(self):
        """获取去年同期的财报日期"""
        current_period = self.get_latest_report_date()
        year = int(current_period[:4])
        return f"{year-1}{current_period[4:]}"
    
    def get_industry_data(self, industry):
        """获取行业数据"""
        try:
            # 获取同行业股票
            stocks = pro.stock_basic(exchange='', list_status='L', fields='ts_code,industry')
            industry_stocks = stocks[stocks['industry'] == industry]['ts_code'].tolist()
            
            # 获取行业财务数据
            industry_data = []
            for stock in industry_stocks:
                financial = self.get_financial_data(stock)
                if financial and financial['current'] is not None:
                    industry_data.append(financial['current'])
            
            return pd.DataFrame(industry_data)
        except Exception as e:
            logger.error(f"获取{industry}行业数据出错: {e}")
            return pd.DataFrame()
    
    def quality_score(self, stock_code):
        """计算基本面质量得分"""
        financial = self.get_financial_data(stock_code)
        if not financial or financial['current'] is None:
            return 0
        
        current = financial['current']
        score = 0
        
        # 获取股票行业
        stock_info = pro.stock_basic(ts_code=stock_code, fields='industry')
        industry = stock_info.iloc[0]['industry'] if not stock_info.empty else None
        
        # ROE评分
        if pd.notna(current['roe']):
            if current['roe'] > 15:
                score += 3
            elif current['roe'] > 10:
                score += 2
            elif current['roe'] > 8:
                score += 1
        
        # 毛利率评分
        if pd.notna(current['grossprofit_margin']):
            # 获取行业毛利率中位数
            industry_data = self.get_industry_data(industry)
            if not industry_data.empty and 'grossprofit_margin' in industry_data.columns:
                industry_gpm_median = industry_data['grossprofit_margin'].median()
                if current['grossprofit_margin'] > industry_gpm_median * 1.2:
                    score += 2
                elif current['grossprofit_margin'] > industry_gpm_median:
                    score += 1
        
        # 现金流评分
        if pd.notna(current['ocf_to_profit']):
            if current['ocf_to_profit'] > 1:
                score += 2
            elif current['ocf_to_profit'] > 0.8:
                score += 1
        
        # 负债率评分
        if pd.notna(current['debt_to_assets']):
            if industry in ['银行', '保险']:
                if current['debt_to_assets'] < 80:
                    score += 1
            else:
                if current['debt_to_assets'] < 40:
                    score += 2
                elif current['debt_to_assets'] < 60:
                    score += 1
        
        # 研发投入评分（如果有）
        if pd.notna(current['rd_exp']) and pd.notna(current['revenue']):
            rd_to_revenue = current['rd_exp'] / current['revenue']
            industry_data = self.get_industry_data(industry)
            if not industry_data.empty and 'rd_exp' in industry_data.columns and 'revenue' in industry_data.columns:
                industry_rd_ratio = (industry_data['rd_exp'] / industry_data['revenue']).median()
                if rd_to_revenue > industry_rd_ratio * 1.2:
                    score += 2
                elif rd_to_revenue > industry_rd_ratio:
                    score += 1
        
        # 业绩增长评分
        if financial['last_year'] is not None:
            last_year = financial['last_year']
            if pd.notna(current['profit_dedt']) and pd.notna(last_year['profit_dedt']) and last_year['profit_dedt'] > 0:
                profit_growth = (current['profit_dedt'] - last_year['profit_dedt']) / last_year['profit_dedt']
                if profit_growth > 0.3:
                    score += 2
                elif profit_growth > 0.1:
                    score += 1
        
        return score
    
    def valuation_score(self, stock_code):
        """计算估值得分"""
        try:
            # 获取当前估值数据
            daily_basic = pro.daily_basic(ts_code=stock_code, fields='ts_code,pe,pe_ttm,pb,ps_ttm')
            if daily_basic.empty:
                return 0
            
            current = daily_basic.iloc[0]
            score = 0
            
            # 获取股票行业
            stock_info = pro.stock_basic(ts_code=stock_code, fields='industry')
            industry = stock_info.iloc[0]['industry'] if not stock_info.empty else None
            
            # PE评分
            if pd.notna(current['pe_ttm']) and current['pe_ttm'] > 0:
                # 获取行业PE中位数
                industry_stocks = pro.stock_basic(industry=industry, fields='ts_code')
                industry_pe = []
                for _, row in industry_stocks.iterrows():
                    try:
                        pe_data = pro.daily_basic(ts_code=row['ts_code'], fields='pe_ttm').iloc[0]['pe_ttm']
                        if pd.notna(pe_data) and pe_data > 0:
                            industry_pe.append(pe_data)
                    except:
                        continue
                
                if industry_pe:
                    industry_pe_median = np.median(industry_pe)
                    if current['pe_ttm'] < industry_pe_median * 0.7:
                        score += 3
                    elif current['pe_ttm'] < industry_pe_median:
                        score += 2
            
            # PB评分
            if pd.notna(current['pb']) and current['pb'] > 0:
                # 获取历史PB数据
                hist_pb = pro.daily_basic(ts_code=stock_code, start_date=(datetime.now() - timedelta(days=365*5)).strftime('%Y%m%d'), fields='pb')
                if not hist_pb.empty:
                    historical_pb_avg = hist_pb['pb'].mean()
                    if current['pb'] < historical_pb_avg * 0.6:
                        score += 2
                    elif current['pb'] < historical_pb_avg * 0.8:
                        score += 1
            
            # PS评分
            if pd.notna(current['ps_ttm']) and current['ps_ttm'] > 0:
                # 获取行业PS中位数
                industry_stocks = pro.stock_basic(industry=industry, fields='ts_code')
                industry_ps = []
                for _, row in industry_stocks.iterrows():
                    try:
                        ps_data = pro.daily_basic(ts_code=row['ts_code'], fields='ps_ttm').iloc[0]['ps_ttm']
                        if pd.notna(ps_data) and ps_data > 0:
                            industry_ps.append(ps_data)
                    except:
                        continue
                
                if industry_ps:
                    industry_ps_median = np.median(industry_ps)
                    if current['ps_ttm'] < industry_ps_median * 0.7:
                        score += 2
                    elif current['ps_ttm'] < industry_ps_median:
                        score += 1
            
            # PEG评分（需要计算）
            financial = self.get_financial_data(stock_code)
            if financial and financial['last_year'] is not None and pd.notna(current['pe_ttm']):
                current_fin = financial['current']
                last_year_fin = financial['last_year']
                if pd.notna(current_fin['profit_dedt']) and pd.notna(last_year_fin['profit_dedt']) and last_year_fin['profit_dedt'] > 0:
                    profit_growth = (current_fin['profit_dedt'] - last_year_fin['profit_dedt']) / last_year_fin['profit_dedt'] * 100
                    if profit_growth > 0:
                        peg = current['pe_ttm'] / profit_growth
                        if 0 < peg < 1:
                            score += 3
                        elif peg < 1.5:
                            score += 2
            
            return score
        except Exception as e:
            logger.error(f"计算{stock_code}估值得分出错: {e}")
            return 0
    
    def momentum_score(self, stock_code):
        """计算动量得分"""
        try:
            # 获取股票历史价格数据
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=180)).strftime('%Y%m%d')
            df = pro.daily(ts_code=stock_code, start_date=start_date, end_date=end_date)
            if df.empty:
                return 0
            
            # 按日期升序排序
            df = df.sort_values('trade_date')
            
            # 计算技术指标
            close_prices = df['close'].values
            
            score = 0
            
            # RSI评分
            rsi = ta.RSI(close_prices, timeperiod=14)
            latest_rsi = rsi[-1]
            
            if 20 < latest_rsi < 30:
                score += 3  # 超卖但不极端
            elif 30 < latest_rsi < 40:
                score += 2
            elif latest_rsi <= 20:
                score += 1  # 极端超卖可能是风险信号
            
            # MACD评分
            macd, signal, hist = ta.MACD(close_prices, fastperiod=12, slowperiod=26, signalperiod=9)
            if hist[-1] > hist[-2] > hist[-3] and hist[-1] < 0:  # 柱状图向上但仍为负，即将金叉
                score += 2
            elif hist[-1] > 0 and hist[-2] < 0:  # 刚刚金叉
                score += 3
            
            # 布林带评分
            upper, middle, lower = ta.BBANDS(close_prices, timeperiod=20)
            if close_prices[-1] < lower[-1]:
                score += 2
            elif close_prices[-1] < middle[-1]:
                score += 1
            
            # 均线评分
            ma20 = ta.MA(close_prices, timeperiod=20)
            ma60 = ta.MA(close_prices, timeperiod=60)
            
            # 股价跌破20日均线后企稳
            if (close_prices[-3] < ma20[-3] and 
                close_prices[-2] < ma20[-2] and 
                close_prices[-1] > close_prices[-2] and
                close_prices[-1] < ma20[-1] * 1.03):  # 股价低于MA20但有企稳迹象
                score += 2
            
            # 20日均线上穿60日均线
            if ma20[-1] > ma60[-1] and ma20[-2] < ma60[-2]:
                score += 2
            
            return score
        except Exception as e:
            logger.error(f"计算{stock_code}动量得分出错: {e}")
            return 0
    
    def volume_pattern_score(self, stock_code):
        """计算成交量模式得分"""
        try:
            # 获取股票历史价格和成交量数据
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=60)).strftime('%Y%m%d')
            df = pro.daily(ts_code=stock_code, start_date=start_date, end_date=end_date)
            if df.empty:
                return 0
            
            # 按日期升序排序
            df = df.sort_values('trade_date')
            
            # 计算20日均量
            df['vol_ma20'] = ta.MA(df['vol'].values, timeperiod=20)
            
            # 计算涨跌幅
            df['pct_chg'] = df['close'].pct_change() * 100
            
            score = 0
            
            # 最近5天的数据
            recent_data = df.tail(5)
            
            # 低位放量滞涨信号（买入信号）
            for _, day in recent_data.iterrows():
                if (pd.notna(day['vol']) and pd.notna(day['vol_ma20']) and 
                    day['vol'] > day['vol_ma20'] * 1.5 and  # 成交量放大50%
                    pd.notna(day['pct_chg']) and day['pct_chg'] < 2 and  # 涨幅小于2%
                    self.is_price_low(stock_code, df)):  # 股价处于低位
                    score += 3
                    break
            
            # 高位放量滞涨信号（卖出信号，这里为负分）
            for _, day in recent_data.iterrows():
                if (pd.notna(day['vol']) and pd.notna(day['vol_ma20']) and 
                    day['vol'] > day['vol_ma20'] * 2 and  # 成交量放大100%
                    pd.notna(day['pct_chg']) and day['pct_chg'] < 1 and  # 涨幅小于1%
                    self.is_price_high(stock_code, df)):  # 股价处于高位
                    score -= 3
                    break
            
            # 检查缩量十字星形态（整理信号）
            for i in range(1, len(recent_data)):
                prev = recent_data.iloc[i-1]
                curr = recent_data.iloc[i]
                if (pd.notna(curr['vol']) and pd.notna(prev['vol']) and 
                    curr['vol'] < prev['vol'] * 0.8 and  # 成交量缩小20%
                    abs(curr['pct_chg']) < 1 and  # 涨跌幅小于1%
                    abs(curr['close'] - curr['open']) / curr['open'] < 0.005):  # 十字星形态
                    score += 1
                    break
            
            return score
        except Exception as e:
            logger.error(f"计算{stock_code}成交量模式得分出错: {e}")
            return 0
    
    def is_price_low(self, stock_code, df=None):
        """判断股价是否处于低位"""
        try:
            if df is None:
                # 获取股票历史价格数据
                end_date = datetime.now().strftime('%Y%m%d')
                start_date = (datetime.now() - timedelta(days=180)).strftime('%Y%m%d')
                df = pro.daily(ts_code=stock_code, start_date=start_date, end_date=end_date)
                if df.empty:
                    return False
                # 按日期升序排序
                df = df.sort_values('trade_date')
            
            close_prices = df['close'].values
            
            # 计算技术指标
            ma120 = ta.MA(close_prices, timeperiod=120)
            upper, middle, lower = ta.BBANDS(close_prices, timeperiod=20)
            
            # 条件1: 股价处于近半年最低30%区间
            price_range = np.percentile(close_prices, [0, 30, 100])
            condition1 = close_prices[-1] <= price_range[1]
            
            # 条件2: 股价低于120日均线10%以上
            condition2 = close_prices[-1] < ma120[-1] * 0.9 if not np.isnan(ma120[-1]) else False
            
            # 条件3: 股价处于布林带下轨以下
            condition3 = close_prices[-1] < lower[-1] if not np.isnan(lower[-1]) else False
            
            # 满足任意两个条件即可
            return sum([condition1, condition2, condition3]) >= 2
        except Exception as e:
            logger.error(f"判断{stock_code}股价位置出错: {e}")
            return False
    
    def is_price_high(self, stock_code, df=None):
        """判断股价是否处于高位"""
        try:
            if df is None:
                # 获取股票历史价格数据
                end_date = datetime.now().strftime('%Y%m%d')
                start_date = (datetime.now() - timedelta(days=180)).strftime('%Y%m%d')
                df = pro.daily(ts_code=stock_code, start_date=start_date, end_date=end_date)
                if df.empty:
                    return False
                # 按日期升序排序
                df = df.sort_values('trade_date')
            
            close_prices = df['close'].values
            
            # 计算技术指标
            ma120 = ta.MA(close_prices, timeperiod=120)
            upper, middle, lower = ta.BBANDS(close_prices, timeperiod=20)
            
            # 条件1: 股价处于近半年最高30%区间
            price_range = np.percentile(close_prices, [0, 70, 100])
            condition1 = close_prices[-1] >= price_range[1]
            
            # 条件2: 股价高于120日均线20%以上
            condition2 = close_prices[-1] > ma120[-1] * 1.2 if not np.isnan(ma120[-1]) else False
            
            # 条件3: 股价处于布林带上轨以上
            condition3 = close_prices[-1] > upper[-1] if not np.isnan(upper[-1]) else False
            
            # 满足任意两个条件即可
            return sum([condition1, condition2, condition3]) >= 2
        except Exception as e:
            logger.error(f"判断{stock_code}股价位置出错: {e}")
            return False
    
    def select_stocks(self, top_n=20):
        """选股"""
        stock_scores = []
        
        for stock in self.stock_pool:
            try:
                # 计算各维度得分
                q_score = self.quality_score(stock)
                v_score = self.valuation_score(stock)
                m_score = self.momentum_score(stock)
                vol_score = self.volume_pattern_score(stock)
                
                # 综合得分，可根据市场环境调整权重
                total_score = q_score * 0.35 + v_score * 0.25 + m_score * 0.2 + vol_score * 0.2
                
                stock_scores.append((stock, total_score, q_score, v_score, m_score, vol_score))
            except Exception as e:
                logger.error(f"计算{stock}得分出错: {e}")
                continue
        
        # 按得分排序并选取前N只
        stock_scores.sort(key=lambda x: x[1], reverse=True)
        top_stocks = [item[0] for item in stock_scores[:top_n]]
        
        # 记录详细得分
        logger.info("Top stocks with scores:")
        for stock, total, q, v, m, vol in stock_scores[:top_n]:
            logger.info(f"{stock}: Total={total:.2f}, Quality={q}, Value={v}, Momentum={m}, Volume={vol}")
        
        return top_stocks
    
    def should_buy(self, stock):
        """判断是否应该买入"""
        # 计算各维度得分
        q_score = self.quality_score(stock)
        v_score = self.valuation_score(stock)
        m_score = self.momentum_score(stock)
        vol_score = self.volume_pattern_score(stock)
        
        # 买入条件
        if (q_score > 7 and  # 基本面得分高
            v_score > 5 and  # 估值得分高
            m_score > 5 and  # 动量指标显示超卖
            vol_score > 2):  # 低位放量滞涨特征明显
            return True
        
        return False
    
    def should_sell(self, stock, holding_days):
        """判断是否应该卖出"""
        try:
            # 获取当前价格
            latest_daily = pro.daily(ts_code=stock, limit=1)
            if latest_daily.empty:
                return False
            current_price = latest_daily.iloc[0]['close']
            
            # 获取买入价格和持有天数
            buy_price = self.portfolio[stock]['成本价']
            
            # 计算收益率
            return_rate = (current_price - buy_price) / buy_price * 100
            
            # 成交量模式评分
            vol_score = self.volume_pattern_score(stock)
            
            # 卖出条件
            if vol_score < -2:  # 高位放量滞涨
                logger.info(f"{stock} 高位放量滞涨，建议卖出")
                return True
            
            if holding_days > self.holding_period_limit and return_rate < 5:  # 长期持有但收益不佳
                logger.info(f"{stock} 持有{holding_days}天，收益率仅{return_rate:.2f}%，建议卖出")
                return True
            
            if return_rate > self.stop_profit:  # 止盈
                logger.info(f"{stock} 收益率达{return_rate:.2f}%，触发止盈")
                return True
            
            if return_rate < self.stop_loss:  # 止损
                logger.info(f"{stock} 收益率为{return_rate:.2f}%，触发止损")
                return True
            
            # 检查季报是否恶化
            financial = self.get_financial_data(stock)
            if financial and financial['last_year'] is not None:
                current_roe = financial['current']['roe'] if pd.notna(financial['current']['roe']) else 0
                previous_roe = financial['last_year']['roe'] if pd.notna(financial['last_year']['roe']) else 0
                
                if previous_roe > 0 and (current_roe - previous_roe) / previous_roe < -0.3:
                    logger.info(f"{stock} ROE同比下降超过30%，建议卖出")
                    return True
            
            return False
        except Exception as e:
            logger.error(f"判断{stock}是否卖出出错: {e}")
            return False
    
    def calculate_position(self, stock, total_capital):
        """计算仓位"""
        # 计算各维度得分
        q_score = self.quality_score(stock)
        v_score = self.valuation_score(stock)
        m_score = self.momentum_score(stock)
        vol_score = self.volume_pattern_score(stock)
        
        # 总分
        total_score = q_score + v_score + m_score + vol_score
        
        # 基础仓位：每只股票5%
        base_position = 0.05
        
        # 根据得分调整仓位
        if total_score > 15:
            position = base_position * 1.5
        elif total_score > 12:
            position = base_position * 1.2
        else:
            position = base_position
        
        # 控制单只股票最大仓位不超过10%
        position = min(position, self.single_position_limit)
        
        return position * total_capital
    
    def buy_stock(self, stock, amount):
        """买入股票"""
        try:
            # 获取当前价格
            latest_daily = pro.daily(ts_code=stock, limit=1)
            if latest_daily.empty:
                logger.error(f"无法获取{stock}当前价格")
                return False
            
            current_price = latest_daily.iloc[0]['close']
            trade_date = latest_daily.iloc[0]['trade_date']
            
            # 计算可买数量（考虑整手100股）
            shares = int(amount / current_price / 100) * 100
            if shares == 0:
                logger.warning(f"资金不足，无法买入{stock}")
                return False
            
            actual_amount = shares * current_price
            
            # 检查资金是否足够
            if actual_amount > self.capital:
                logger.warning(f"资金不足，无法买入{stock}")
                return False
            
            # 更新资金和持仓
            self.capital -= actual_amount
            
            if stock in self.portfolio:
                # 已有持仓，计算新的平均成本
                old_shares = self.portfolio[stock]['数量']
                old_cost = self.portfolio[stock]['成本价'] * old_shares
                new_cost = old_cost + actual_amount
                new_shares = old_shares + shares
                new_avg_cost = new_cost / new_shares
                
                self.portfolio[stock] = {
                    '数量': new_shares,
                    '成本价': new_avg_cost,
                    '买入日期': trade_date
                }
            else:
                # 新建持仓
                self.portfolio[stock] = {
                    '数量': shares,
                    '成本价': current_price,
                    '买入日期': trade_date
                }
            
            # 记录交易
            self.trade_history.append({
                '日期': trade_date,
                '股票': stock,
                '操作': '买入',
                '价格': current_price,
                '数量': shares,
                '金额': actual_amount
            })
            
            logger.info(f"买入 {stock} {shares}股，价格 {current_price}，金额 {actual_amount}")
            return True
        except Exception as e:
            logger.error(f"买入{stock}出错: {e}")
            return False
    
    def sell_stock(self, stock):
        """卖出股票"""
        try:
            if stock not in self.portfolio:
                logger.warning(f"{stock}不在持仓中")
                return False
            
            # 获取当前价格
            latest_daily = pro.daily(ts_code=stock, limit=1)
            if latest_daily.empty:
                logger.error(f"无法获取{stock}当前价格")
                return False
            
            current_price = latest_daily.iloc[0]['close']
            trade_date = latest_daily.iloc[0]['trade_date']
            
            # 计算卖出金额
            shares = self.portfolio[stock]['数量']
            amount = shares * current_price
            
            # 更新资金和持仓
            self.capital += amount
            cost_price = self.portfolio[stock]['成本价']
            profit = (current_price - cost_price) * shares
            
            # 记录交易
            self.trade_history.append({
                '日期': trade_date,
                '股票': stock,
                '操作': '卖出',
                '价格': current_price,
                '数量': shares,
                '金额': amount,
                '盈亏': profit
            })
            
            # 删除持仓
            del self.portfolio[stock]
            
            logger.info(f"卖出 {stock} {shares}股，价格 {current_price}，金额 {amount}，盈亏 {profit}")
            return True
        except Exception as e:
            logger.error(f"卖出{stock}出错: {e}")
            return False
    
    def get_holding_days(self, stock):
        """获取持有天数"""
        if stock not in self.portfolio:
            return 0
        
        buy_date = self.portfolio[stock]['买入日期']
        buy_date = datetime.strptime(buy_date, '%Y%m%d')
        current_date = datetime.now()
        
        return (current_date - buy_date).days
    
    def rebalance(self):
        """调仓"""
        logger.info("开始调仓...")
        
        # 计算当前总资产
        total_assets = self.calculate_total_assets()
        
        # 获取推荐股票列表
        recommended_stocks = self.select_stocks(self.max_stock_num)
        
        # 卖出不在推荐列表中的股票
        for stock in list(self.portfolio.keys()):
            if stock not in recommended_stocks:
                holding_days = self.get_holding_days(stock)
                if self.should_sell(stock, holding_days):
                    self.sell_stock(stock)
        
        # 检查所有持仓的止盈止损条件
        for stock in list(self.portfolio.keys()):
            holding_days = self.get_holding_days(stock)
            if self.should_sell(stock, holding_days):
                self.sell_stock(stock)
        
        # 买入新推荐的股票
        for stock in recommended_stocks:
            if stock not in self.portfolio and self.should_buy(stock):
                position = self.calculate_position(stock, total_assets)
                self.buy_stock(stock, position)
        
        # 更新净值
        self.update_net_value()
        
        logger.info(f"调仓完成，当前持仓数量: {len(self.portfolio)}，可用资金: {self.capital}")
    
    def calculate_total_assets(self):
        """计算当前总资产"""
        total = self.capital
        
        for stock, info in self.portfolio.items():
            try:
                # 获取当前价格
                latest_daily = pro.daily(ts_code=stock, limit=1)
                if not latest_daily.empty:
                    current_price = latest_daily.iloc[0]['close']
                    shares = info['数量']
                    total += current_price * shares
            except Exception as e:
                logger.error(f"计算{stock}市值出错: {e}")
        
        return total
    
    def update_net_value(self):
        """更新净值"""
        total_assets = self.calculate_total_assets()
        net_value = total_assets / self.initial_capital
        current_date = datetime.now().strftime('%Y%m%d')
        
        self.daily_net_value.append({
            '日期': current_date,
            '净值': net_value,
            '总资产': total_assets
        })
        
        logger.info(f"当前净值: {net_value:.4f}, 总资产: {total_assets:.2f}")
    
    def is_first_trading_day_of_month(self):
        """判断是否为每月第一个交易日"""
        today = datetime.now()
        first_day = datetime(today.year, today.month, 1)
        
        # 获取当月第一个交易日
        cal = pro.trade_cal(start_date=first_day.strftime('%Y%m%d'), end_date=today.strftime('%Y%m%d'))
        if cal.empty:
            return False
        
        first_trading_day = cal[cal['is_open'] == 1].iloc[0]['cal_date']
        return today.strftime('%Y%m%d') == first_trading_day
    
    def run_daily(self):
        """每日运行"""
        # 更新净值
        self.update_net_value()
        
        # 检查是否需要调仓
        if self.is_first_trading_day_of_month():
            self.rebalance()
        else:
            # 每日检查止盈止损条件
            for stock in list(self.portfolio.keys()):
                holding_days = self.get_holding_days(stock)
                if self.should_sell(stock, holding_days):
                    self.sell_stock(stock)
    
    def run_backtest(self):
        """回测"""
        logger.info("开始回测...")
        
        # 设置回测起止日期
        start_date = datetime.strptime(self.start_date, '%Y%m%d')
        end_date = datetime.strptime(self.end_date, '%Y%m%d')
        
        # 获取交易日历
        calendar = pro.trade_cal(start_date=self.start_date, end_date=self.end_date)
        trading_days = calendar[calendar['is_open'] == 1]['cal_date'].tolist()
        
        current_month = None
        
        for day in trading_days:
            # 模拟时间前进
            current_date = datetime.strptime(day, '%Y%m%d')
            
            # 每月第一个交易日调仓
            if current_date.month != current_month:
                self.rebalance()
                current_month = current_date.month
            else:
                # 每日检查止盈止损条件
                for stock in list(self.portfolio.keys()):
                    holding_days = self.get_holding_days(stock)
                    if self.should_sell(stock, holding_days):
                        self.sell_stock(stock)
            
            # 更新净值
            self.update_net_value()
        
        # 计算回测指标
        self.calculate_backtest_metrics()
    
    def calculate_backtest_metrics(self):
        """计算回测指标"""
        if not self.daily_net_value:
            logger.warning("没有净值数据，无法计算回测指标")
            return
        
        # 提取净值序列
        net_values = [item['净值'] for item in self.daily_net_value]
        
        # 计算年化收益率
        days = len(net_values)
        annual_return = (net_values[-1] / net_values[0]) ** (365 / days) - 1
        
        # 计算最大回撤
        max_drawdown = 0
        peak = net_values[0]
        
        for value in net_values:
            if value > peak:
                peak = value
            drawdown = (peak - value) / peak
            max_drawdown = max(max_drawdown, drawdown)
        
        # 计算夏普比率
        returns = [net_values[i] / net_values[i-1] - 1 for i in range(1, len(net_values))]
        avg_return = np.mean(returns)
        std_return = np.std(returns)
        risk_free_rate = 0.03 / 365  # 假设年化无风险利率为3%
        sharpe_ratio = (avg_return - risk_free_rate) / std_return * np.sqrt(252) if std_return > 0 else 0
        
        # 计算胜率
        wins = sum(1 for trade in self.trade_history if trade.get('操作') == '卖出' and trade.get('盈亏', 0) > 0)
        total_trades = sum(1 for trade in self.trade_history if trade.get('操作') == '卖出')
        win_rate = wins / total_trades if total_trades > 0 else 0
        
        # 计算盈亏比
        profits = sum(trade.get('盈亏', 0) for trade in self.trade_history if trade.get('操作') == '卖出' and trade.get('盈亏', 0) > 0)
        losses = sum(abs(trade.get('盈亏', 0)) for trade in self.trade_history if trade.get('操作') == '卖出' and trade.get('盈亏', 0) < 0)
        profit_loss_ratio = (profits / wins) / (losses / (total_trades - wins)) if wins > 0 and total_trades - wins > 0 else 0
        
        # 计算换手率
        turnover = sum(trade.get('金额', 0) for trade in self.trade_history) / (2 * self.initial_capital) / (days / 252)
        
        # 输出回测指标
        metrics = {
            "年化收益率": annual_return * 100,
            "最大回撤": max_drawdown * 100,
            "夏普比率": sharpe_ratio,
            "胜率": win_rate * 100,
            "盈亏比": profit_loss_ratio,
            "换手率": turnover * 100
        }
        
        logger.info("回测指标:")
        for key, value in metrics.items():
            logger.info(f"{key}: {value:.2f}%")
        
        return metrics
    
    def plot_performance(self):
        """绘制回测业绩图表"""
        try:
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            
            # 提取日期和净值
            dates = [datetime.strptime(item['日期'], '%Y%m%d') for item in self.daily_net_value]
            net_values = [item['净值'] for item in self.daily_net_value]
            
            # 创建图表
            plt.figure(figsize=(12, 8))
            
            # 绘制净值曲线
            plt.subplot(2, 1, 1)
            plt.plot(dates, net_values, 'b-', linewidth=2)
            plt.title('策略净值曲线')
            plt.ylabel('净值')
            plt.grid(True)
            plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            plt.gcf().autofmt_xdate()
            
            # 计算回撤
            drawdowns = []
            peak = net_values[0]
            for value in net_values:
                if value > peak:
                    peak = value
                drawdown = (peak - value) / peak
                drawdowns.append(drawdown)
            
            # 绘制回撤曲线
            plt.subplot(2, 1, 2)
            plt.plot(dates, drawdowns, 'r-', linewidth=2)
            plt.title('回撤曲线')
            plt.ylabel('回撤')
            plt.grid(True)
            plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            plt.gcf().autofmt_xdate()
            
            plt.tight_layout()
            plt.savefig('strategy_performance.png')
            plt.close()
            
            logger.info("业绩图表已保存为 strategy_performance.png")
        except Exception as e:
            logger.error(f"绘制业绩图表出错: {e}")
    
    def save_results(self):
        """保存回测结果"""
        try:
            # 保存净值数据
            net_value_df = pd.DataFrame(self.daily_net_value)
            net_value_df.to_csv('net_value.csv', index=False)
            
            # 保存交易记录
            trade_df = pd.DataFrame(self.trade_history)
            trade_df.to_csv('trade_history.csv', index=False)
            
            # 保存当前持仓
            portfolio_data = []
            for stock, info in self.portfolio.items():
                portfolio_data.append({
                    '股票代码': stock,
                    '持有数量': info['数量'],
                    '成本价': info['成本价'],
                    '买入日期': info['买入日期'],
                    '持有天数': self.get_holding_days(stock)
                })
            portfolio_df = pd.DataFrame(portfolio_data)
            portfolio_df.to_csv('current_portfolio.csv', index=False)
            
            logger.info("回测结果已保存到CSV文件")
        except Exception as e:
            logger.error(f"保存回测结果出错: {e}")