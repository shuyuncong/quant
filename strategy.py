
import backtrader as bt
import datetime
import pandas as pd
import numpy as np
import math

# region 1. 选股模块 (Placeholder)
def select_stocks():
    """
    ### 选股模块说明 ###
    这是一个功能占位符。在真实的量化工作流中，选股是回测前至关重要的一步。
    您会基于以下“三条腿”原则，通过API（如Tushare, Baostock）或数据库查询来筛选股票池：

    1.  **基本面筛选**:
        -   排除: 负债率 > 50%, 应收账款 > 货币资金, 财务费用 > 净利润, 经营现金流净额 < 净利润。
        -   优选: ROE > 10%, PE <= 20, 市值50-500亿, 股本 < 50亿股。
    2.  **成交量筛选**:
        -   近30日平均换手率在1%-5%之间，确保流动性适中。
    3.  **技术面初筛**:
        -   股价处于年线（250日均线）之上，确认基本的多头趋势。

    执行该函数后，会得到一个股票代码列表，然后将这些股票的数据逐一加载到 Backtrader 中进行回测。
    """
    print("执行选股模块，筛选符合基本面、成交量和技术面初步条件的股票...")
    # 示例股票池 (在实际应用中由筛选逻辑产生)
    stock_list = ['stock1.csv', 'stock2.csv']
    print(f"选股完成，将对以下股票进行回测: {stock_list}")
    return stock_list
# endregion

# region 2. 仓位管理模块
class CustomSizer(bt.Sizer):
    """
    ### 仓位管理模块 ###
    根据市场行情动态调整仓位。
    - 上涨行情 (收盘价 > 年线): 70% 仓位
    - 震荡行情 (收盘价 ≈ 年线): 50% 仓位
    - 下跌行情 (收盘价 < 年线): 30% 仓位 (或更低，这里设为30%)
    """
    params = (
        ('uptrend_stake', 0.7),
        ('sidetrend_stake', 0.5),
        ('downtrend_stake', 0.3),
        ('sma_period', 250), # 用于判断趋势的均线
    )

    def __init__(self):
        self.sma = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.sma_period)

    def _getsizing(self, comminfo, cash, data, isbuy):
        if isbuy:
            position = self.broker.getposition(data)
            if position:
                # 如果已有仓位，则不再买入（简化逻辑，避免重复加仓）
                return 0

            close = data.close[0]
            sma250 = self.sma[0]
            
            # 判断趋势
            if close > sma250 * 1.05: # 股价高于年线5%以上，视为上涨趋势
                target_percent = self.p.uptrend_stake
            elif close < sma250 * 0.95: # 股价低于年线5%以下，视为下跌趋势
                target_percent = self.p.downtrend_stake
            else: # 股价在年线附近震荡
                target_percent = self.p.sidetrend_stake
            
            # 根据目标百分比计算大小
            size = (cash * target_percent) / close
            return size
        else:
            # 卖出时，卖出所有持仓
            return self.broker.getposition(data).size

# endregion

# region 3. 交易策略核心框架
class CoreStrategy(bt.Strategy):
    """
    ### 交易策略核心框架 ###
    集成了选股、信号生成、仓位和风控的逻辑。
    """
    params = (
        # 指标参数
        ('macd_fast', 12),
        ('macd_slow', 26),
        ('macd_signal', 9),
        ('sma_period', 250),
        ('rsi_period', 14),
        ('vol_avg_period', 30),
        # 风控参数
        ('stop_loss_pct', 0.08), # 个股止损幅度
        ('profit_take_pct', 0.30), # 波段止盈幅度
    )

    def __init__(self):
        # --- 指标定义 ---
        self.sma250 = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.sma_period)
        self.macd = bt.indicators.MACD(self.data.close,
                                      period_me1=self.p.macd_fast,
                                      period_me2=self.p.macd_slow,
                                      period_signal=self.p.macd_signal)
        # MACD柱状图 (Histogram)
        self.macd_hist = self.macd.macd - self.macd.signal
        self.rsi = bt.indicators.RSI(self.data.close, period=self.p.rsi_period)
        self.vol_avg = bt.indicators.SimpleMovingAverage(self.data.volume, period=self.p.vol_avg_period)
        
        # 追踪订单状态
        self.order = None
        self.buy_price = 0

    def log(self, txt, dt=None):
        dt = dt or self.datas[0].datetime.date(0)
        print(f'{dt.isoformat()} | {txt}')

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status in [order.Completed]:
            if order.isbuy():
                self.log(f'BUY EXECUTED, Price: {order.executed.price:.2f}, Cost: {order.executed.value:.2f}, Comm: {order.executed.comm:.2f}')
                self.buy_price = order.executed.price
            elif order.issell():
                self.log(f'SELL EXECUTED, Price: {order.executed.price:.2f}, Cost: {order.executed.value:.2f}, Comm: {order.executed.comm:.2f}')
            self.bar_executed = len(self)
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('Order Canceled/Margin/Rejected')

        self.order = None

    def notify_trade(self, trade):
        if not trade.isclosed:
            return
        self.log(f'TRADE PROFIT, GROSS: {trade.pnl:.2f}, NET: {trade.pnlcomm:.2f}')

    def next(self):
        if self.order: # 如果有挂单，则不进行新的交易
            return 

        # --- 市场趋势判断 ---
        is_uptrend = self.data.close[0] > self.sma250[0]
        is_downtrend = self.data.close[0] < self.sma250[0]

        # --- 信号生成 ---
        # 底背离简化检测: 股价创近期新低，但MACD柱状图的低点抬高
        price_new_low = self.data.low[0] < min(self.data.low.get(ago=-1, size=10))
        macd_hist_higher_low = self.macd_hist[0] > self.macd_hist[-1] and self.macd_hist[-1] < 0

        # 顶背离简化检测: 股价创近期新高，但MACD柱状图的高点降低
        price_new_high = self.data.high[0] > max(self.data.high.get(ago=-1, size=10))
        macd_hist_lower_high = self.macd_hist[0] < self.macd_hist[-1] and self.macd_hist[-1] > 0

        # 成交量信号
        volume_burst = self.data.volume[0] > self.vol_avg[0] * 1.5

        # --- 交易决策 ---
        if not self.position: # 如果没有持仓，则寻找买入机会
            # **上涨趋势策略买点**
            if is_uptrend:
                # 1. 回调至年线附近站稳 + 底背离信号
                is_near_sma250 = abs(self.data.close[0] - self.sma250[0]) / self.sma250[0] < 0.05
                if is_near_sma250 and macd_hist_higher_low and volume_burst:
                    self.log(f'BUY SIGNAL: Uptrend, pullback to SMA250 with divergence. Price: {self.data.close[0]:.2f}')
                    self.order = self.buy()
                    # 设置止损单
                    stop_price = self.data.close[0] * (1 - self.p.stop_loss_pct)
                    self.sell(exectype=bt.Order.Stop, price=stop_price)

                # 2. 低位平台放量突破
                # (此逻辑较复杂，需要形态识别，此处简化为MACD金叉)
                if self.macd.macd[0] > self.macd.signal[0] and self.macd.macd[-1] < self.macd.signal[-1] and volume_burst:
                    self.log(f'BUY SIGNAL: Uptrend, MACD crossover with volume. Price: {self.data.close[0]:.2f}')
                    self.order = self.buy()
                    stop_price = self.data.close[0] * (1 - self.p.stop_loss_pct)
                    self.sell(exectype=bt.Order.Stop, price=stop_price)

        else: # 如果有持仓，则寻找卖出机会
            # **卖出信号**
            # 1. 顶背离信号
            if price_new_high and macd_hist_lower_high:
                self.log(f'SELL SIGNAL: Top divergence detected. Price: {self.data.close[0]:.2f}')
                self.order = self.close()

            # 2. 盈利保护
            if self.data.close[0] >= self.buy_price * (1 + self.p.profit_take_pct):
                self.log(f'SELL SIGNAL: Profit target reached. Price: {self.data.close[0]:.2f}')
                self.order = self.close()

            # 3. 跌破年线（作为趋势反转的强烈信号）
            if self.data.close[0] < self.sma250[0] and self.data.close[-1] > self.sma250[-1]:
                self.log(f'SELL SIGNAL: Price broke below SMA250. Price: {self.data.close[0]:.2f}')
                self.order = self.close()
# endregion

# region 4. 模拟数据生成
def generate_dummy_data(file_path, days=500):
    """
    生成模拟数据，包含一个清晰的上涨趋势和回调，以便策略可以被触发。
    """
    dates = pd.to_datetime([datetime.date.today() - datetime.timedelta(days=i) for i in range(days)])
    dates = dates.sort_values()
    
    # 创建一个基础正弦波趋势
    price = 100 + np.sin(np.arange(days) / 50) * 20 + np.arange(days) * 0.2
    # 增加一些噪声
    price += np.random.randn(days) * 2
    
    # 确保价格不为负
    price = np.maximum(1, price)
    
    # 生成其他OHLC数据
    op = price - np.random.uniform(0, 2, size=days)
    op = np.maximum(1, op)
    high = np.maximum(price, op) + np.random.uniform(0, 2, size=days)
    low = np.minimum(price, op) - np.random.uniform(0, 2, size=days)
    low = np.maximum(1, low)

    # 成交量
    volume = 1000000 + np.random.randint(-200000, 200000, size=days)
    
    df = pd.DataFrame({
        'datetime': dates,
        'open': op,
        'high': high,
        'low': low,
        'close': price,
        'volume': volume
    })
    df.to_csv(file_path, index=False)
    return file_path
# endregion

# region 5. 策略回测与评估
if __name__ == '__main__':
    cerebro = bt.Cerebro()

    # --- 添加策略和仓位管理器 ---
    cerebro.addstrategy(CoreStrategy)
    # cerebro.addsizer(CustomSizer) # 使用自定义的动态仓位管理器
    cerebro.addsizer(bt.sizers.PercentSizer, percents=40) # 或者使用固定的40%仓位

    # --- 准备数据 ---
    # 在真实场景中，你会调用 select_stocks() 并加载真实数据
    # 此处我们生成一个模拟数据文件用于演示
    dummy_data_file = generate_dummy_data('dummy_stock_data.csv', days=750)
    
    data = bt.feeds.GenericCSVData(
        dataname=dummy_data_file,
        dtformat=('%Y-%m-%d'),
        datetime=0, open=1, high=2, low=3, close=4, volume=5,
        openinterest=-1
    )
    cerebro.adddata(data)

    # --- 设置初始资金和手续费 ---
    cerebro.broker.setcash(250000.0)
    cerebro.broker.setcommission(commission=0.0001) # 0.1% 的手续费

    # --- 添加分析器 ---
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', timeframe=bt.TimeFrame.Days, compression=252)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')

    # --- 运行回测 ---
    print('Starting Portfolio Value: %.2f' % cerebro.broker.getvalue())
    results = cerebro.run()
    strat = results[0]
    
    # --- 打印回测关键指标 ---
    print('\n--- Backtest Results ---')
    final_value = cerebro.broker.getvalue()
    print('Final Portfolio Value: %.2f' % final_value)
    print('Total Return: %.2f%%' % ((final_value / 100000.0 - 1) * 100))

    # 1. 胜率 (Win Rate)
    trade_analysis = strat.analyzers.trades.get_analysis()
    if 'total' in trade_analysis and trade_analysis.total.total > 0:
        won_trades = trade_analysis.won.total if 'won' in trade_analysis else 0
        win_rate = (won_trades / trade_analysis.total.total) * 100
        print(f"Win Rate: {win_rate:.2f}%")
    else:
        print("Win Rate: No trades executed.")

    # 2. 盈亏比 (Profit/Loss Ratio)
    if 'won' in trade_analysis and trade_analysis.won.total > 0 and 'lost' in trade_analysis and trade_analysis.lost.total > 0:
        avg_win = trade_analysis.won.pnl.average
        avg_loss = trade_analysis.lost.pnl.average
        # Avoid division by zero if average loss is somehow 0
        if avg_loss != 0:
            pnl_ratio = abs(avg_win / avg_loss)
            print(f"Profit/Loss Ratio: {pnl_ratio:.2f}:1")
        else:
            print("Profit/Loss Ratio: Cannot be calculated (average loss is zero).")
    else:
        print("Profit/Loss Ratio: Not enough trades to calculate.")

    # 3. 最大回撤 (Max Drawdown)
    drawdown_analysis = strat.analyzers.drawdown.get_analysis()
    max_drawdown = drawdown_analysis.max.drawdown
    print(f"Max Drawdown: {max_drawdown:.2f}%")

    # 4. 年化收益率 (Annualized Return)
    returns_analysis = strat.analyzers.returns.get_analysis()
    annual_return = returns_analysis['rnorm100']
    print(f"Annualized Return: {annual_return:.2f}%")
    
    print('\n--- End of Backtest ---')

    # --- 绘制图表 ---
    cerebro.plot(style='candlestick', barup='red', bardown='green')
