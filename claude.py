import backtrader as bt
import backtrader.indicators as btind
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime
import yfinance as yf

# 自定义指标：筹码分布指标
class ChipDistribution(bt.Indicator):
    lines = ('low_concentration', 'high_concentration')
    params = (('period', 20),)
    
    def __init__(self):
        self.addminperiod(self.params.period)
        self.volume = self.data.volume
        self.close = self.data.close
        self.high = self.data.high
        self.low = self.data.low
        
    def next(self):
        # 简化的筹码分布计算
        vol_sum = sum(self.volume.get(size=self.params.period))
        low_price_vol = sum([self.volume[i] for i in range(-self.params.period, 0) 
                           if self.close[i] < self.close[0] * 0.95])
        high_price_vol = sum([self.volume[i] for i in range(-self.params.period, 0) 
                            if self.close[i] > self.close[0] * 1.05])
        
        self.lines.low_concentration[0] = low_price_vol / vol_sum if vol_sum > 0 else 0
        self.lines.high_concentration[0] = high_price_vol / vol_sum if vol_sum > 0 else 0

# 自定义指标：量价关系指标
class VolumePrice(bt.Indicator):
    lines = ('vol_price_signal',)
    params = (('period', 5),)
    
    def __init__(self):
        self.addminperiod(self.params.period + 1)
        self.volume = self.data.volume
        self.close = self.data.close
        
    def next(self):
        # 计算价格变化和成交量变化
        price_change = self.close[0] - self.close[-self.params.period]
        vol_avg_prev = sum(self.volume.get(size=self.params.period, ago=1)) / self.params.period
        vol_avg_curr = sum(self.volume.get(size=self.params.period)) / self.params.period
        vol_change = vol_avg_curr - vol_avg_prev
        
        # 量价关系信号
        # 1: 放量上涨, 2: 缩量上涨, 3: 放量下跌, 4: 缩量下跌
        if price_change > 0 and vol_change > 0:
            self.lines.vol_price_signal[0] = 1  # 放量上涨
        elif price_change > 0 and vol_change <= 0:
            self.lines.vol_price_signal[0] = 2  # 缩量上涨
        elif price_change <= 0 and vol_change > 0:
            self.lines.vol_price_signal[0] = 3  # 放量下跌
        else:
            self.lines.vol_price_signal[0] = 4  # 缩量下跌

# 主策略类
class ThreeLegStrategy(bt.Strategy):
    params = (
        ('ma250', 250),  # 年线
        ('macd_fast', 12),
        ('macd_slow', 26),
        ('macd_signal', 9),
        ('rsi_period', 14),
        ('max_position_pct', 0.4),  # 单个标的最大仓位
        ('stop_loss_pct', 0.08),    # 止损比例
        ('take_profit_pct', 0.3),   # 止盈比例
        ('market_type', 'uptrend'),  # 市场类型: uptrend, downtrend, sideways
    )
    
    def __init__(self):
        # 技术指标
        self.ma250 = bt.indicators.SimpleMovingAverage(self.data.close, period=self.params.ma250)
        self.macd = bt.indicators.MACD(
            self.data.close, 
            period_me1=self.params.macd_fast,
            period_me2=self.params.macd_slow,
            period_signal=self.params.macd_signal
        )
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)
        
        # 自定义指标
        self.volume_price = VolumePrice(self.data)
        self.chip_dist = ChipDistribution(self.data)
        
        # 交易状态变量
        self.order = None
        self.buy_price = 0
        self.stop_loss = 0
        self.take_profit = 0
        self.position_size = 0
        
        # 背离检测变量
        self.price_highs = []
        self.price_lows = []
        self.macd_highs = []
        self.macd_lows = []
        
        # 交易统计
        self.trades = 0
        self.wins = 0
        self.losses = 0
        self.profit_sum = 0
        self.loss_sum = 0
        
        # 3日量比
        self.volume_ratio = bt.indicators.PeriodN(self.data.volume, period=3) / bt.indicators.SMA(self.data.volume, period=10)
        
    def log(self, txt, dt=None):
        """日志函数"""
        dt = dt or self.datas[0].datetime.date(0)
        print(f'{dt.isoformat()}, {txt}')
        
    def notify_order(self, order):
        """订单状态通知"""
        if order.status in [order.Submitted, order.Accepted]:
            return
            
        if order.status in [order.Completed]:
            if order.isbuy():
                self.log(f'买入执行: 价格={order.executed.price:.2f}, 成本={order.executed.value:.2f}, 手续费={order.executed.comm:.2f}')
                self.buy_price = order.executed.price
                self.stop_loss = self.buy_price * (1 - self.params.stop_loss_pct)
                self.take_profit = self.buy_price * (1 + self.params.take_profit_pct)
            else:
                self.log(f'卖出执行: 价格={order.executed.price:.2f}, 成本={order.executed.value:.2f}, 手续费={order.executed.comm:.2f}')
                
                # 计算盈亏
                if order.executed.price > self.buy_price:
                    self.wins += 1
                    self.profit_sum += (order.executed.price - self.buy_price) / self.buy_price
                else:
                    self.losses += 1
                    self.loss_sum += (self.buy_price - order.executed.price) / self.buy_price
                    
                self.trades += 1
                
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('订单取消/保证金不足/拒绝')
            
        self.order = None
        
    def detect_divergence(self):
        """检测顶底背离"""
        # 简化的背离检测逻辑
        if len(self.price_highs) < 2 or len(self.macd_highs) < 2:
            return None
            
        # 顶背离: 股价创新高但MACD未创新高
        if self.price_highs[-1] > self.price_highs[-2] and self.macd_highs[-1] < self.macd_highs[-2]:
            return "top_divergence"
            
        # 底背离: 股价创新低但MACD未创新低
        if self.price_lows[-1] < self.price_lows[-2] and self.macd_lows[-1] > self.macd_lows[-2]:
            return "bottom_divergence"
            
        return None
        
    def update_divergence_data(self):
        """更新用于背离检测的数据"""
        # 简化的局部高低点检测
        if len(self.data) < 5:
            return
            
        # 检测局部高点
        if (self.data.close[-2] > self.data.close[-3] and 
            self.data.close[-2] > self.data.close[-1] and
            self.data.close[-2] > self.data.close[-4] and
            self.data.close[-2] > self.data.close[-5]):
            self.price_highs.append(self.data.close[-2])
            self.macd_highs.append(self.macd.macd[-2])
            
        # 检测局部低点
        if (self.data.close[-2] < self.data.close[-3] and 
            self.data.close[-2] < self.data.close[-1] and
            self.data.close[-2] < self.data.close[-4] and
            self.data.close[-2] < self.data.close[-5]):
            self.price_lows.append(self.data.close[-2])
            self.macd_lows.append(self.macd.macd[-2])
            
        # 保持列表长度适中
        if len(self.price_highs) > 10:
            self.price_highs.pop(0)
            self.macd_highs.pop(0)
            
        if len(self.price_lows) > 10:
            self.price_lows.pop(0)
            self.macd_lows.pop(0)
            
    def next(self):
        """主策略逻辑"""
        # 更新背离检测数据
        self.update_divergence_data()
        
        # 如果有未完成订单，等待
        if self.order:
            return
            
        # 市场趋势判断
        market_trend = self.determine_market_trend()
        
        # 根据不同市场环境选择不同策略
        if market_trend == 'uptrend':
            self.uptrend_strategy()
        elif market_trend == 'downtrend':
            self.downtrend_strategy()
        else:  # sideways
            self.sideways_strategy()
            
        # 风控逻辑
        self.risk_management()
        
    def determine_market_trend(self):
        """确定市场趋势"""
        # 如果参数中指定了市场类型，直接使用
        if hasattr(self.params, 'market_type'):
            return self.params.market_type
            
        # 否则基于技术指标判断
        close = self.data.close[0]
        
        # 年线向上为上涨趋势
        if close > self.ma250[0] and self.ma250[0] > self.ma250[-20]:
            return 'uptrend'
        # 年线向下为下跌趋势
        elif close < self.ma250[0] and self.ma250[0] < self.ma250[-20]:
            return 'downtrend'
        # 其他情况为震荡市场
        else:
            return 'sideways'
            
    def uptrend_strategy(self):
        """上涨趋势策略"""
        # 检测信号
        divergence = self.detect_divergence()
        macd_crossover = self.macd.macd[0] > self.macd.signal[0] and self.macd.macd[-1] <= self.macd.signal[-1]
        macd_crossunder = self.macd.macd[0] < self.macd.signal[0] and self.macd.macd[-1] >= self.macd.signal[-1]
        
        # 买入信号
        if not self.position:
            # 底背离 + MACD金叉 + 年线向上 + 回调至年线附近
            if (divergence == "bottom_divergence" and macd_crossover and 
                self.data.close[0] > self.ma250[0] * 0.95 and self.data.close[0] < self.ma250[0] * 1.05):
                self.buy_signal()
            # 低位放量滞涨 + 筹码在低位集中
            elif (self.volume_price.vol_price_signal[0] == 3 and 
                  self.chip_dist.low_concentration[0] > 0.6 and 
                  self.volume_ratio[0] > 1.5):
                self.buy_signal()
                
        # 卖出信号
        else:
            # 顶背离 + 高位放量滞涨
            if (divergence == "top_divergence" and 
                (self.volume_price.vol_price_signal[0] == 1 or self.volume_price.vol_price_signal[0] == 3) and
                self.chip_dist.high_concentration[0] > 0.6):
                self.sell_signal()
            # 连续大涨超过30%
            elif self.data.close[0] > self.buy_price * 1.3:
                self.sell_signal(partial=True)  # 部分减仓
                
    def downtrend_strategy(self):
        """下跌趋势策略"""
        # 检测信号
        divergence = self.detect_divergence()
        
        # 在下跌趋势中主要是做反向T，减少仓位
        if self.position:
            # 顶背离出现，减仓
            if divergence == "top_divergence":
                self.sell_signal(partial=True)
            # 放量下跌，减仓
            elif self.volume_price.vol_price_signal[0] == 3:
                self.sell_signal(partial=True)
            # 年线破位且深跌超过20%，全部卖出
            elif (self.data.close[0] < self.ma250[0] and 
                  self.data.close[0] < self.data.close[-20] * 0.8):
                self.sell_signal()
                
        # 在下跌趋势中谨慎买入
        else:
            # 只有在底背离非常明显且缩量止跌时考虑小仓位买入
            if (divergence == "bottom_divergence" and 
                self.volume_price.vol_price_signal[0] == 4 and
                self.rsi[0] < 30):  # 超卖
                self.buy_signal(small_position=True)
                
    def sideways_strategy(self):
        """震荡市场策略"""
        # 检测信号
        divergence = self.detect_divergence()
        
        # 买入信号
        if not self.position:
            # 30分钟底背离 + 缩量止跌 (这里用日线模拟)
            if divergence == "bottom_divergence" and self.volume_price.vol_price_signal[0] == 4:
                self.buy_signal(medium_position=True)
            # 年线附近反弹
            elif (self.data.close[0] > self.ma250[0] * 0.95 and 
                  self.data.close[0] < self.ma250[0] * 1.05 and
                  self.data.close[0] > self.data.close[-1]):
                self.buy_signal(medium_position=True)
                
        # 卖出信号
        else:
            # 30分钟顶背离 + 放量滞涨
            if (divergence == "top_divergence" and 
                (self.volume_price.vol_price_signal[0] == 1 or self.volume_price.vol_price_signal[0] == 3)):
                self.sell_signal()
            # 高位放量
            elif self.volume_price.vol_price_signal[0] == 1 and self.rsi[0] > 70:
                self.sell_signal()
                
    def buy_signal(self, small_position=False, medium_position=False):
        """买入信号处理"""
        # 计算仓位大小
        cash = self.broker.getcash()
        value = self.broker.getvalue()
        
        if small_position:
            size_pct = self.params.max_position_pct * 0.5  # 小仓位
        elif medium_position:
            size_pct = self.params.max_position_pct * 0.7  # 中等仓位
        else:
            size_pct = self.params.max_position_pct  # 标准仓位
            
        # 计算可用资金比例
        available_pct = size_pct * (cash / value)
        
        # 计算买入股数
        price = self.data.close[0]
        size = int((value * available_pct) / price)
        
        if size > 0:
            self.log(f'买入信号: 价格={price:.2f}, 数量={size}')
            self.order = self.buy(size=size)
            self.position_size = size
            
    def sell_signal(self, partial=False):
        """卖出信号处理"""
        if not self.position:
            return
            
        if partial:
            # 部分减仓，卖出30%持仓
            size = int(self.position.size * 0.3)
        else:
            # 全部卖出
            size = self.position.size
            
        if size > 0:
            self.log(f'卖出信号: 价格={self.data.close[0]:.2f}, 数量={size}')
            self.order = self.sell(size=size)
            
    def risk_management(self):
        """风险管理"""
        if not self.position:
            return
            
        # 止损逻辑
        if self.data.close[0] < self.stop_loss:
            self.log(f'触发止损: 价格={self.data.close[0]:.2f}, 止损价={self.stop_loss:.2f}')
            self.order = self.sell(size=self.position.size)
            
        # 年线破位且无底背离结构止损
        if (self.data.close[0] < self.ma250[0] and 
            self.detect_divergence() != "bottom_divergence" and
            self.data.close[0] < self.data.close[-20] * 0.8):  # 深跌超过20%
            self.log(f'趋势破位止损: 价格={self.data.close[0]:.2f}')
            self.order = self.sell(size=self.position.size)
            
        # 止盈逻辑 - 移动止损
        if self.data.close[0] > self.buy_price * 1.15:  # 盈利超过15%
            new_stop = self.buy_price  # 移动止损到成本线
            if new_stop > self.stop_loss:
                self.stop_loss = new_stop
                self.log(f'移动止损到成本线: {self.stop_loss:.2f}')
                
    def stop(self):
        """策略结束时的统计"""
        self.log('策略执行结束')
        self.log(f'总交易次数: {self.trades}')
        
        if self.trades > 0:
            win_rate = self.wins / self.trades
            self.log(f'胜率: {win_rate:.2%}')
            
            if self.wins > 0 and self.losses > 0:
                avg_profit = self.profit_sum / self.wins
                avg_loss = self.loss_sum / self.losses
                profit_loss_ratio = avg_profit / avg_loss if avg_loss > 0 else float('inf')
                self.log(f'盈亏比: {profit_loss_ratio:.2f}')


# 回测函数
def run_backtest(data, strategy_params=None):
    """运行回测"""
    cerebro = bt.Cerebro()
    
    # 添加数据
    cerebro.adddata(data)
    
    # 添加策略
    if strategy_params:
        cerebro.addstrategy(ThreeLegStrategy, **strategy_params)
    else:
        cerebro.addstrategy(ThreeLegStrategy)
    
    # 设置初始资金
    cerebro.broker.setcash(1000000.0)
    
    # 设置手续费
    cerebro.broker.setcommission(commission=0.0003)  # 万三手续费
    
    # 设置滑点
    cerebro.broker.set_slippage_perc(0.001)  # 0.1%滑点
    
    # 添加分析器
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade')
    
    # 运行回测
    results = cerebro.run()
    
    # 输出分析结果
    strat = results[0]
    
    print(f"夏普比率: {strat.analyzers.sharpe.get_analysis()['sharperatio']:.3f}")
    print(f"最大回撤: {strat.analyzers.drawdown.get_analysis()['max']['drawdown']:.2%}")
    print(f"年化收益率: {strat.analyzers.returns.get_analysis()['ravg'] * 252:.2%}")
    
    trade_analysis = strat.analyzers.trade.get_analysis()
    if trade_analysis['total']['total'] > 0:
        win_rate = trade_analysis['won']['total'] / trade_analysis['total']['total']
        print(f"胜率: {win_rate:.2%}")
        
        if trade_analysis['won']['total'] > 0 and trade_analysis['lost']['total'] > 0:
            avg_win = trade_analysis['won']['pnl']['average']
            avg_loss = abs(trade_analysis['lost']['pnl']['average'])
            profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else float('inf')
            print(f"盈亏比: {profit_loss_ratio:.2f}")
    
    # 绘制结果
    cerebro.plot(style='candle', barup='red', bardown='green', 
                 volup='red', voldown='green', 
                 plotdist=0.5, subplot=True)
    
    return results


# 获取数据示例
def get_stock_data(symbol, start_date, end_date):
    """获取股票数据"""
    # 使用yfinance获取数据
    df = yf.download(symbol, start=start_date, end=end_date)
    
    # 转换为backtrader数据格式
    data = bt.feeds.PandasData(
        dataname=df,
        datetime=None,  # 使用索引作为日期
        open=0,         # df中的第0列是开盘价
        high=1,         # df中的第1列是最高价
        low=2,          # df中的第2列是最低价
        close=3,        # df中的第3列是收盘价
        volume=4,       # df中的第4列是成交量
        openinterest=-1 # 不使用未平仓合约数
    )
    
    return data


# 主函数
if __name__ == "__main__":
    # 获取股票数据
    data = get_stock_data('AAPL', '2018-01-01', '2023-01-01')
    
    # 设置策略参数
    strategy_params = {
        'market_type': 'uptrend',  # 可选: uptrend, downtrend, sideways
        'max_position_pct': 0.4,
        'stop_loss_pct': 0.08,
        'take_profit_pct': 0.3
    }
    
    # 运行回测
    results = run_backtest(data, strategy_params)