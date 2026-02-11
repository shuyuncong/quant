import backtrader as bt
import pandas as pd
import matplotlib.pyplot as plt

# =======================
# 1. 选股模块
# =======================
def stock_filter(df):
    """
    根据三条腿原则：基本面 + 成交量 + 技术面
    df: 包含财务指标和行情数据的 DataFrame
    返回布尔值（是否通过筛选）
    """
    # 基本面条件
    if df['roe'] < 0.1:  # ROE < 10%
        return False
    if df['pe'] > 30:    # PE过高
        return False
    if df['debt_ratio'] > 0.5:  # 负债率过高
        return False

    # 成交量条件
    if df['turnover'].mean() < 0.01 or df['turnover'].mean() > 0.2:
        return False

    # 技术面（均线趋势）
    if df['close'].iloc[-1] < df['ma250'].iloc[-1]:
        return False

    return True


# =======================
# 2. 策略模块（信号 + 仓位 + 风控）
# =======================
class QuantStrategy(bt.Strategy):
    params = dict(
        stop_loss=0.08,      # 动态止损8%
        take_profit=0.3,     # 止盈30%
        trail=True,          # 是否使用移动止盈
    )

    def __init__(self):
        # 技术指标
        self.ma250 = bt.indicators.SimpleMovingAverage(self.data.close, period=250)
        self.macd = bt.indicators.MACD(self.data.close)
        self.rsi = bt.indicators.RSI(self.data.close, period=14)

        # 资金管理
        self.order = None
        self.buyprice = None

    def next(self):
        if self.order:  # 有挂单时不重复操作
            return

        # 买入信号（战略买点）
        if not self.position:
            if self.data.close[0] > self.ma250[0] and self.macd.macd[0] > self.macd.signal[0]:
                size = int(self.broker.getcash() * 0.25 / self.data.close[0])  # 每次25%仓位
                self.order = self.buy(size=size)
                self.buyprice = self.data.close[0]

        else:
            # 卖出条件：止盈 / 止损 / 顶背离
            if self.data.close[0] < self.buyprice * (1 - self.p.stop_loss):
                self.order = self.close()  # 止损
            elif self.data.close[0] > self.buyprice * (1 + self.p.take_profit):
                self.order = self.close()  # 止盈
            elif self.macd.macd[0] < self.macd.signal[0]:
                self.order = self.close()  # 顶背离信号


# =======================
# 3. 回测函数
# =======================
def run_backtest(datafile):
    # 数据读取
    df = pd.read_csv(datafile, parse_dates=True, index_col=0)
    df['ma250'] = df['close'].rolling(250).mean()
    data = bt.feeds.PandasData(dataname=df)

    # 回测引擎
    cerebro = bt.Cerebro()
    cerebro.adddata(data)
    cerebro.addstrategy(QuantStrategy)
    cerebro.addsizer(bt.sizers.PercentSizer, percents=25)  # 默认单票25%仓位
    cerebro.broker.set_cash(1000000)   # 初始资金
    cerebro.broker.setcommission(commission=0.001)  # 千分之一手续费

    # 分析工具
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade')

    results = cerebro.run()
    strat = results[0]

    # 输出绩效指标
    print('夏普比率:', strat.analyzers.sharpe.get_analysis())
    print('最大回撤:', strat.analyzers.drawdown.get_analysis())
    print('交易统计:', strat.analyzers.trade.get_analysis())

    # 绘制图表
    cerebro.plot(style='candlestick')

    return strat


# =======================
# 4. 执行回测
# =======================
if __name__ == '__main__':
    run_backtest("your_stock_data.csv")  # 需替换为实际股票数据文件
