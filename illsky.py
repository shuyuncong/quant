import backtrader as bt
import pandas as pd
import numpy as np
from backtrader.indicators import EMA, MACD, ROC

class TripleStrategy(bt.Strategy):
    params = (
        ('debug', True),
        ('printops', True),
        ('ma_period', 250),    # 年线周期
        ('position_percent', 0.25),  # 单只基础仓位比例
        ('max_position_percent', 0.4),  # 单只最大仓位比例
        ('vol_threshold', 0.05),  # 成交量阈值
        ('roe_threshold', 0.1),   # ROE阈值
    )

    def __init__(self):
        # 基本面数据 (假设数据feed中包含这些字段)
        self.roe = self.datas[0].roe
        self.pe = self.datas[0].pe
        self.debt_ratio = self.datas[0].debt_ratio
        self.turnover = self.datas[0].turnover  # 换手率
        
        # 技术指标
        self.ma250 = bt.indicators.SMA(self.data.close, period=self.p.ma_period)
        self.macd = bt.indicators.MACD(self.data.close)
        self.volume_sma = bt.indicators.SMA(self.data.volume, period=30)
        
        # MACD柱子面积计算 (简化版)
        self.macd_hist = self.macd.macd - self.macd.signal
        self.macd_area = bt.indicators.SMA(self.macd_hist, period=5)
        
        # 状态跟踪
        self.t_position = 0  # T仓位数
        self.t_cost = 0      # T仓成本
        self.base_position = 0  # 基础仓位数
        self.position_count = 0  # 持仓标的数
        
    def next(self):
        if self.p.debug and self.p.printops:
            print(f"Date: {self.datetime.date()}, Close: {self.data.close[0]}, Volume: {self.data.volume[0]}")
        
        # 0. 基本仓位管理 (最多4只股票)
        if self.position_count >= 4:
            return
            
        # 1. 三条腿选股逻辑
        if self.should_buy():
            self.open_base_position()
            
        # 2. 交易信号执行
        self.execute_signals()
        
        # 3. 资金风控
        self.risk_control()
        
    def should_buy(self):
        """三条腿选股逻辑"""
        # 基本面过滤
        if not self.fundamental_filter():
            return False
            
        # 技术面过滤
        if not self.technical_filter():
            return False
            
        # 量价关系过滤
        if not self.volume_filter():
            return False
            
        return True
        
    def fundamental_filter(self):
        """基本面过滤"""
        # ROE > 10%
        if self.roe[0] < self.p.roe_threshold:
            return False
            
        # PE合理 (<30倍)
        if self.pe[0] > 30:
            return False
            
        # 避开三高一低
        if self.debt_ratio[0] > 0.7:  # 高负债
            return False
            
        if self.data.cash_flow[0] < self.data.net_profit[0]:  # 现金流<净利润
            return False
            
        return True
        
    def technical_filter(self):
        """技术面过滤"""
        # 年线向上
        if self.ma250[0] < self.ma250[-1]:
            return False
            
        # MACD底背离检测 (简化版)
        if self.data.close[0] < self.data.close[-5] and self.macd_area[0] > self.macd_area[-5]:
            return True
            
        # 平台突破
        if self.data.close[0] > max(self.data.high.get(size=20)):
            return True
            
        return False
        
    def volume_filter(self):
        """量价关系过滤"""
        # 低位放量滞涨 (成交量放大但价格变化不大)
        vol_ratio = self.data.volume[0] / self.volume_sma[0]
        price_change = abs(self.data.close[0]/self.data.close[-1] - 1)
        
        if vol_ratio > 1.5 and price_change < 0.05:
            return True
            
        # 换手率在合理区间
        if 0.01 < self.turnover[0] < 0.2:
            return True
            
        return False
        
    def open_base_position(self):
        """开基础仓位"""
        size = self.calc_base_position_size()
        self.buy(size=size)
        self.base_position = size
        self.position_count += 1
        if self.p.debug:
            print(f"Open Base Position: {size} shares at {self.data.close[0]}")
        
    def calc_base_position_size(self):
        """计算基础仓位数"""
        value = self.broker.getvalue()
        cash = self.broker.get_cash()
        max_size = int((value * self.p.position_percent) / self.data.close[0])
        return min(max_size, int(cash / self.data.close[0]))
        
    def execute_signals(self):
        """交易信号执行"""
        # 牛市正T策略
        if self.is_bull_market():
            self.execute_bull_t_strategy()
        # 熊市策略
        elif self.is_bear_market():
            self.execute_bear_strategy()
        # 震荡市策略
        else:
            self.execute_swing_strategy()
            
    def is_bull_market(self):
        """判断是否牛市"""
        return self.ma250[0] > self.ma250[-20]  # 年线上扬
        
    def is_bear_market(self):
        """判断是否熊市"""
        return self.ma250[0] < self.ma250[-20]  # 年线下行
        
    def execute_bull_t_strategy(self):
        """牛市正T策略"""
        # 底背离加仓T
        if self.is_bottom_divergence():
            t_size = self.calc_t_size()
            self.buy(size=t_size)
            self.t_position += t_size
            self.t_cost = self.data.close[0]
            if self.p.debug:
                print(f"Bull T-Buy: {t_size} shares at {self.data.close[0]}")
                
        # 顶背离减仓T
        elif self.is_top_divergence():
            sell_size = min(self.t_position, self.base_position // 2)
            self.sell(size=sell_size)
            self.t_position -= sell_size
            if self.p.debug:
                print(f"Bull T-Sell: {sell_size} shares at {self.data.close[0]}")
                
        # 急跌加仓 (单日跌幅>5%)
        if (self.data.close[0] / self.data.open[0] - 1) < -0.05:
            t_size = self.calc_t_size()
            self.buy(size=t_size)
            self.t_position += t_size
            self.t_cost = self.data.close[0]
            if self.p.debug:
                print(f"Bull Dip-Buy: {t_size} shares at {self.data.close[0]}")
            
    def execute_bear_strategy(self):
        """熊市策略"""
        # 顶背离减仓
        if self.is_top_divergence():
            # 清仓退出
            if self.position.size > 0:
                self.sell(size=self.position.size)
                if self.p.debug:
                    print(f"Bear Market Sell All at {self.data.close[0]}")
                    
    def execute_swing_strategy(self):
        """震荡市策略"""
        # 30分钟底背离
        if self.is_30min_bottom():
            t_size = self.calc_t_size()
            self.buy(size=t_size)
            self.t_position += t_size
            self.t_cost = self.data.close[0]
            if self.p.debug:
                print(f"Swing T-Buy: {t_size} shares at {self.data.close[0]}")
                
        # 30分钟顶背离
        elif self.is_30min_top():
            sell_size = min(self.t_position, self.base_position // 2)
            self.sell(size=sell_size)
            self.t_position -= sell_size
            if self.p.debug:
                print(f"Swing T-Sell: {sell_size} shares at {self.data.close[0]}")
                
    def calc_t_size(self):
        """计算T仓位数"""
        cash = self.broker.get_cash()
        max_t_size = int((self.broker.getvalue() * 0.15) / self.data.close[0])
        return min(int(cash / self.data.close[0]), max_t_size)
        
    def risk_control(self):
        """资金风控"""
        # 止损逻辑
        if self.position.size > 0:
            cost_price = self.position.price
            loss_percent = (cost_price - self.data.close[0]) / cost_price
            
            # 个股亏损>8%
            if loss_percent > 0.08:
                self.sell(size=self.position.size)
                if self.p.debug:
                    print(f"Stop Loss: Sold All at {self.data.close[0]}")
                
            # 盈利保护 (>30%)
            profit_percent = (self.data.close[0] - cost_price) / cost_price
            if profit_percent > 0.3:
                sell_size = int(self.position.size * 0.5)
                self.sell(size=sell_size)
                if self.p.debug:
                    print(f"Profit Protection: Sold 50% at {self.data.close[0]}")

    # 技术信号检测函数 --------------------------------------------
    
    def is_bottom_divergence(self):
        """MACD底背离检测 (简化版)"""
        if len(self.data) < 10:
            return False
            
        price_low = self.data.close[0] < self.data.close[-5]
        macd_higher = self.macd_area[0] > self.macd_area[-5]
        return price_low and macd_higher
        
    def is_top_divergence(self):
        """MACD顶背离检测 (简化版)"""
        if len(self.data) < 10:
            return False
            
        price_high = self.data.close[0] > self.data.close[-5]
        macd_lower = self.macd_area[0] < self.macd_area[-5]
        return price_high and macd_lower
        
    def is_30min_bottom(self):
        """30分钟底背离检测 (简化版)"""
        # 需要用到较小周期数据，这里简化为短周期MACD
        short_macd = bt.indicators.MACD(self.data.close, period_me1=12, period_me2=26, period_signal=9)
        macd_hist = short_macd.macd - short_macd.signal
        
        if len(self.data) < 10:
            return False
            
        price_low = self.data.close[0] < self.data.close[-3]
        macd_higher = macd_hist[0] > macd_hist[-3]
        return price_low and macd_higher
        
    def is_30min_top(self):
        """30分钟顶背离检测 (简化版)"""
        short_macd = bt.indicators.MACD(self.data.close, period_me1=12, period_me2=26, period_signal=9)
        macd_hist = short_macd.macd - short_macd.signal
        
        if len(self.data) < 10:
            return False
            
        price_high = self.data.close[0] > self.data.close[-3]
        macd_lower = macd_hist[0] < macd_hist[-3]
        return price_high and macd_lower

    # 仓位管理函数 --------------------------------------------
        
    def notify_trade(self, trade):
        """交易通知"""
        if trade.isclosed:
            # T仓位清算时重置
            if trade.size == self.t_position:
                self.t_position = 0
                self.t_cost = 0
                
            # 基础仓位减少
            elif trade.size == self.base_position:
                self.base_position = 0
                self.position_count -= 1
                
    # 回测分析函数 --------------------------------------------
    
    def stop(self):
        """回测结束分析"""
        self.cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='mysharpe')
        self.cerebro.addanalyzer(bt.analyzers.DrawDown, _name='mydrawdown')
        self.cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='mytrade')
        
        # 打印结果
        print('='*50)
        print(f"最终资产价值: {self.broker.getvalue():.2f}")
        print('='*50)
        
        # 获取分析结果
        trade_analysis = self.analyzers.mytrade.get_analysis()
        sharpe_ratio = self.analyzers.mysharpe.get_analysis()
        drawdown = self.analyzers.mydrawdown.get_analysis()
        
        # 打印关键指标
        print(f"夏普比率: {sharpe_ratio['sharperatio']:.2f}")
        print(f"最大回撤: {drawdown.max.drawdown:.2%}")
        print(f"总交易次数: {trade_analysis.total.closed}")
        print(f"胜率: {trade_analysis.won.total / trade_analysis.total.closed:.2%}")
        print(f"盈亏比: {trade_analysis.won.pnl.total / abs(trade_analysis.lost.pnl.total):.2f}")

# 指标绘制类
class StrategyPlotter(bt.observer.Observer):
    lines = ('macd', 'signal', 'ma250',)
    
    plotinfo = dict(plot=True, subplot=True)
    
    def __init__(self):
        self.macd = self._owner.macd.macd
        self.signal = self._owner.macd.signal
        self.ma250 = self._owner.ma250
        
    def next(self):
        self.lines.macd[0] = self.macd[0]
        self.lines.signal[0] = self.signal[0]
        self.lines.ma250[0] = self.ma250[0]

# 回测运行函数
def run_backtest(data):
    cerebro = bt.Cerebro()
    cerebro.adddata(data)
    cerebro.addstrategy(TripleStrategy)
    
    # 添加分析器
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade')
    
    # 设置初始资金
    cerebro.broker.setcash(100000.0)
    
    # 添加自定义指标绘图
    cerebro.addobserver(StrategyPlotter)
    
    # 运行回测
    results = cerebro.run()
    
    # 绘制图表
    cerebro.plot(style='candlestick', volume=True)
    
    return results

# 数据加载函数 (示例)
def load_data():
    # 实际应用中需替换为真实数据源
    data = bt.feeds.PandasData(
        dataname=pd.read_csv('your_data.csv', parse_dates=True, index_col=0),
        roe=6,   # ROE字段索引
        pe=7,    # PE字段索引
        debt_ratio=8,  # 负债率字段索引
        turnover=9,    # 换手率字段索引
        openinterest=-1
    )
    return data

# 主程序入口
if __name__ == '__main__':
    data = load_data()
    results = run_backtest(data)