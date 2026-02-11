import backtrader as bt
import backtrader.indicators as btind
import backtrader.feeds as btfeeds
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime

# ==================== 技术指标定义 ====================

class MACD_Area(bt.Indicator):
    """
    自定义MACD面积指标，用于检测背离
    """
    lines = ('macd_area', 'hist_area')
    params = (('fast', 12), ('slow', 26), ('signal', 9))
    
    def __init__(self):
        self.macd = btind.MACD(self.data, period_me1=self.p.fast, 
                              period_me2=self.p.slow, period_signal=self.p.signal)
        # MACD柱状图面积（累加）
        self.l.macd_area = btind.SumN(self.macd.macd, period=5)
        # 信号线面积（累加）
        self.l.hist_area = btind.SumN(self.macd.signal, period=5)

class AnnualTrend(bt.Indicator):
    """
    年线趋势指标
    """
    lines = ('ma250', 'ma250_trend')
    params = (('period', 250),)
    
    def __init__(self):
        self.l.ma250 = btind.SMA(self.data.close, period=self.p.period)
        # 年线趋势：1向上，0平，-1向下
        self.l.ma250_trend = btind.Cmp(self.l.ma250, self.l.ma250(-1))

class VolumePattern(bt.Indicator):
    """
    量能模式识别
    """
    lines = ('volume_ratio', 'volume_status')
    params = (('short_period', 5), ('long_period', 20))
    
    def __init__(self):
        short_avg = btind.SMA(self.data.volume, period=self.p.short_period)
        long_avg = btind.SMA(self.data.volume, period=self.p.long_period)
        self.l.volume_ratio = short_avg / long_avg
        # 量能状态：1放量，0平量，-1缩量
        self.l.volume_status = btind.Cmp(short_avg, long_avg)

# ==================== 策略核心类 ====================

class TripleLegStrategy(bt.Strategy):
    params = (
        # 基本面参数
        ('min_roe', 10),
        ('max_pe', 20),
        ('max_debt_ratio', 50),
        # 技术面参数
        ('macd_fast', 12),
        ('macd_slow', 26),
        ('macd_signal', 9),
        ('rsi_period', 14),
        # 风控参数
        ('stop_loss', 0.08),
        ('take_profit', 0.20),
        ('max_position_size', 0.25),
        ('max_portfolio_risk', 0.02),
    )
    
    def __init__(self):
        # 技术指标
        self.annual_trend = AnnualTrend(self.data)
        self.macd = btind.MACD(self.data, 
                              period_me1=self.p.macd_fast,
                              period_me2=self.p.macd_slow,
                              period_signal=self.p.macd_signal)
        self.macd_area = MACD_Area(self.data)
        self.rsi = btind.RSI(self.data, period=self.p.rsi_period)
        self.volume_pattern = VolumePattern(self.data)
        
        # 交易状态跟踪
        self.order = None
        self.buy_price = None
        self.bar_executed = 0
        
        # 仓位管理
        self.positions_count = 0
        self.max_positions = 4
        self.cash_reserve = 0.25  # 25%现金储备
        
    def next(self):
        # 跳过最初的数据积累期
        if len(self) < 250:
            return
            
        # 检查是否有挂单
        if self.order:
            return
            
        # 获取当前数据
        data = self.data
        position = self.getposition(data)
        
        # 计算当前市值
        portfolio_value = self.broker.getvalue()
        cash = self.broker.getcash()
        
        # 风控检查
        if self.check_risk_control():
            return
            
        # 选股条件检查（三条腿原则）
        if not self.check_stock_selection():
            if position:
                self.close(data)
            return
            
        # 生成交易信号
        signal = self.generate_signal()
        
        # 执行交易
        self.execute_trade(signal, position, portfolio_value, cash)
        
    def check_risk_control(self):
        """风险控制检查"""
        # 单日回撤超过2%，停止开新仓
        if self.stats.drawdown.drawdown[-1] > 0.02:
            return True
            
        # 总回撤超过20%，清仓
        if self.stats.drawdown.drawdown[-1] > 0.20:
            for data in self.datas:
                self.close(data)
            return True
            
        return False
        
    def check_stock_selection(self):
        """三条腿选股原则检查"""
        # 技术面检查
        if self.annual_trend.ma250_trend[0] <= 0:  # 年线不向上
            return False
            
        if self.data.close[0] < self.annual_trend.ma250[0]:  # 股价低于年线
            return False
            
        # 量能检查（简化版）
        if self.volume_pattern.volume_ratio[0] < 1.0:  # 近期量能不足
            return False
            
        # 这里可以添加更多基本面检查（需要外部数据）
        # 如ROE、PE、负债率等
        
        return True
        
    def generate_signal(self):
        """生成交易信号"""
        data = self.data
        
        # 趋势判断
        trend = self.determine_market_trend()
        
        # 根据不同趋势生成信号
        if trend == "uptrend":
            return self.uptrend_signal()
        elif trend == "downtrend":
            return self.downtrend_signal()
        else:  # sideways
            return self.sideways_signal()
            
    def determine_market_trend(self):
        """判断市场趋势"""
        # 年线趋势为主
        if self.annual_trend.ma250_trend[0] > 0 and self.data.close[0] > self.annual_trend.ma250[0]:
            return "uptrend"
        elif self.annual_trend.ma250_trend[0] < 0 and self.data.close[0] < self.annual_trend.ma250[0]:
            return "downtrend"
        else:
            return "sideways"
            
    def uptrend_signal(self):
        """上涨趋势信号"""
        # 战略买入点：年线附近 + 底背离
        near_annual = abs(self.data.close[0] - self.annual_trend.ma250[0]) / self.annual_trend.ma250[0] < 0.05
        
        # 简化版底背离检测
        price_low = min(self.data.close.get(size=10))
        macd_low = min(self.macd.macd.get(size=10))
        divergence = price_low == self.data.close[0] and macd_low != self.macd.macd[0]
        
        if near_annual and divergence and self.rsi[0] < 40:
            return "BUY"
            
        # 顶背离卖出信号
        price_high = max(self.data.close.get(size=10))
        macd_high = max(self.macd.macd.get(size=10))
        top_divergence = price_high == self.data.close[0] and macd_high != self.macd.macd[0]
        
        if top_divergence and self.rsi[0] > 70:
            return "SELL"
            
        return "HOLD"
        
    def downtrend_signal(self):
        """下跌趋势信号"""
        # 反弹减仓
        if self.rsi[0] > 60 and self.volume_pattern.volume_ratio[0] > 1.5:
            return "SELL"
            
        return "HOLD"
        
    def sideways_signal(self):
        """震荡市信号"""
        # 波段操作
        if self.rsi[0] < 30:
            return "BUY"
        elif self.rsi[0] > 70:
            return "SELL"
            
        return "HOLD"
        
    def execute_trade(self, signal, position, portfolio_value, cash):
        """执行交易"""
        data = self.data
        
        # 计算可用资金
        available_cash = cash * (1 - self.cash_reserve)
        
        if signal == "BUY" and not position:
            # 计算仓位大小
            size = self.calculate_position_size(portfolio_value, available_cash)
            
            # 执行买入
            if size > 0:
                self.order = self.buy(data=data, size=size)
                self.buy_price = data.close[0]
                
        elif signal == "SELL" and position:
            # 执行卖出
            self.order = self.sell(data=data, size=position.size)
            
    def calculate_position_size(self, portfolio_value, available_cash):
        """计算仓位大小"""
        # 单只股票最大仓位
        max_position_value = portfolio_value * self.p.max_position_size
        
        # 计算可买股数
        price = self.data.close[0]
        size = min(
            int(max_position_value / price),
            int(available_cash / price)
        )
        
        return size
        
    def notify_order(self, order):
        """订单状态通知"""
        if order.status in [order.Completed]:
            if order.isbuy():
                self.log(f'BUY EXECUTED, Price: {order.executed.price:.2f}, Cost: {order.executed.value:.2f}, Comm: {order.executed.comm:.2f}')
            elif order.issell():
                self.log(f'SELL EXECUTED, Price: {order.executed.price:.2f}, Cost: {order.executed.value:.2f}, Comm: {order.executed.comm:.2f}')
                
            self.bar_executed = len(self)
            
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('Order Canceled/Margin/Rejected')
            
        self.order = None
        
    def notify_trade(self, trade):
        """交易结果通知"""
        if not trade.isclosed:
            return
            
        self.log(f'OPERATION PROFIT, GROSS: {trade.pnl:.2f}, NET: {trade.pnlcomm:.2f}')
        
    def log(self, txt, dt=None):
        """日志记录"""
        dt = dt or self.datas[0].datetime.date(0)
        print(f'{dt.isoformat()}, {txt}')

# ==================== 回测设置 ====================

def run_backtest():
    # 创建Cerebro引擎
    cerebro = bt.Cerebro()
    
    # 设置初始资金
    cerebro.broker.setcash(100000.0)
    
    # 设置交易手续费
    cerebro.broker.setcommission(commission=0.001)  # 0.1%
    
    # 添加策略
    cerebro.addstrategy(TripleLegStrategy)
    
    # 获取数据（这里以苹果股票为例）
    data = bt.feeds.YahooFinanceData(
        dataname='AAPL',
        fromdate=datetime(2018, 1, 1),
        todate=datetime(2023, 12, 31),
        buffered=True
    )
    
    # 添加数据
    cerebro.adddata(data)
    
    # 添加分析器
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    
    # 运行回测
    print('Starting Portfolio Value: %.2f' % cerebro.broker.getvalue())
    results = cerebro.run()
    print('Final Portfolio Value: %.2f' % cerebro.broker.getvalue())
    
    # 打印分析结果
    strat = results[0]
    print('Sharpe Ratio:', strat.analyzers.sharpe.get_analysis())
    print('DrawDown:', strat.analyzers.drawdown.get_analysis())
    print('Returns:', strat.analyzers.returns.get_analysis())
    print('Trade Analysis:', strat.analyzers.trades.get_analysis())
    
    # 绘制图表
    cerebro.plot(style='candlestick')

# ==================== 多股票回测框架 ====================

class MultiStockBacktest:
    """多股票回测框架"""
    def __init__(self, stock_list, start_date, end_date):
        self.stock_list = stock_list
        self.start_date = start_date
        self.end_date = end_date
        self.results = {}
        
    def run(self):
        """运行多股票回测"""
        for stock in self.stock_list:
            print(f"Backtesting {stock}...")
            
            cerebro = bt.Cerebro()
            cerebro.broker.setcash(100000.0)
            cerebro.broker.setcommission(commission=0.001)
            
            # 添加策略
            cerebro.addstrategy(TripleLegStrategy)
            
            # 添加数据
            data = bt.feeds.YahooFinanceData(
                dataname=stock,
                fromdate=self.start_date,
                todate=self.end_date,
                buffered=True
            )
            cerebro.adddata(data)
            
            # 添加分析器
            cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
            cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
            cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
            cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
            
            # 运行回测
            result = cerebro.run()
            self.results[stock] = result[0]
            
    def generate_report(self):
        """生成回测报告"""
        report_data = []
        
        for stock, result in self.results.items():
            sharpe = result.analyzers.sharpe.get_analysis()
            drawdown = result.analyzers.drawdown.get_analysis()
            returns = result.analyzers.returns.get_analysis()
            trades = result.analyzers.trades.get_analysis()
            
            report_data.append({
                'Stock': stock,
                'Sharpe': sharpe.get('sharperatio', 0),
                'Max Drawdown': drawdown.get('max', {}).get('drawdown', 0),
                'Total Return': returns.get('rtot', 0),
                'Total Trades': trades.get('total', {}).get('total', 0),
                'Win Rate': trades.get('won', {}).get('total', 0) / trades.get('total', {}).get('total', 1) if trades.get('total', {}).get('total', 0) > 0 else 0
            })
            
        df = pd.DataFrame(report_data)
        return df

# ==================== 主程序 ====================

if __name__ == '__main__':
    # 单股票回测
    run_backtest()
    
    # 多股票回测示例
    # stock_list = ['AAPL', 'MSFT', 'GOOGL', 'AMZN']
    # backtester = MultiStockBacktest(stock_list, datetime(2018, 1, 1), datetime(2023, 12, 31))
    # backtester.run()
    # report = backtester.generate_report()
    # print(report)