import backtrader as bt
import backtrader.analyzers as btanalyzers
import backtrader.indicators as btind
import pandas as pd
import numpy as np
from datetime import datetime
import matplotlib.pyplot as plt  # For potential custom plots if needed

# Assuming we have a data loader function; in practice, use YahooFinance or CSV
# For demo, we'll simulate data or assume pandas DataFrames for stocks

class PandasData(bt.feeds.PandasData):
    params = (
        ('datetime', None),
        ('open', 'Open'),
        ('high', 'High'),
        ('low', 'Low'),
        ('close', 'Close'),
        ('volume', 'Volume'),
        ('openinterest', None),
    )

# Select stocks function (modular, outside strategy)
def select_stocks(stock_pool, roe_data, pe_data, debt_data, market_cap_data):
    """
    Select stocks based on three legs: Basic, Volume/Quantity, Technical (simplified for backtest)
    - Basic: ROE >10%, PE <=20, Debt <50%
    - Market Cap: 50-500B
    - Returns top 3 stocks meeting criteria (for demo)
    """
    selected = []
    for stock in stock_pool:
        if (roe_data.get(stock, 0) > 10 and
            pe_data.get(stock, float('inf')) <= 20 and
            debt_data.get(stock, 100) < 50 and
            50 <= market_cap_data.get(stock, 0) <= 500):
            selected.append(stock)
        if len(selected) >= 3:  # Limit to 3 as per principle
            break
    return selected

# Strategy Class
class TeaTalkStrategy(bt.Strategy):
    params = (
        ('year_ma_period', 250),  # Year line
        ('macd_period_me1', 12),
        ('macd_period_me2', 26),
        ('macd_period_signal', 9),
        ('turnover_window', 30),  # For avg turnover
        ('max_pos_per_stock', 0.4),  # 40% max per stock
        ('mobile_funds', 0.25),  # 25% mobile
        ('add_increment', 0.05),  # 5% add
        ('stop_loss_pct', 0.08),  # 8% stop loss
        ('profit_take_pct', 0.20),  # 20% profit take
        ('big_drop', 0.20),  # 20% deep drop for trend break
    )

    def __init__(self):
        self.stocks = {d._name: d for d in self.datas}  # Multi-data
        self.year_ma = {}
        self.macd = {}
        self.prev_macd_hist = {}  # For divergence detection (simplified area as height for demo)
        self.turnover = {}  # Simplified turnover as volume / avg_volume
        self.orders = {}
        self.base_pos = 0.25  # 25% base per stock
        self.mobile_used = {stock: 0 for stock in self.stocks}

        for stock, data in self.stocks.items():
            self.year_ma[stock] = btind.SMA(data.close, period=self.p.year_ma_period)
            self.macd[stock] = btind.MACD(data.close,
                                          period_me1=self.p.macd_period_me1,
                                          period_me2=self.p.macd_period_me2,
                                          period_signal=self.p.macd_period_signal)
            self.prev_macd_hist[stock] = None
            self.turnover[stock] = btind.AverageTrueRange(data, period=self.p.turnover_window)  # Proxy for volume activity
            self.orders[stock] = None

    def next(self):
        total_value = self.broker.getvalue()
        for stock, data in self.stocks.items():
            if not self.position:  # Skip if no position, but assume we enter on signal
                continue

            close = data.close[0]
            volume = data.volume[0]
            year_ma = self.year_ma[stock][0]
            macd_hist = self.macd[stock].macd[0] - self.macd[stock].signal[0]  # Hist proxy
            prev_hist = self.prev_macd_hist[stock]

            # Update prev hist
            self.prev_macd_hist[stock] = macd_hist

            # Quantity: Simplified turnover rate (volume / close as proxy, assume shares=1 for demo)
            avg_turnover = np.mean(data.volume.get(size=self.p.turnover_window)) / close if close else 0

            # Detect trend
            is_uptrend = close > year_ma and year_ma > year_ma[-1]  # Upward year line
            is_downtrend = close < year_ma and year_ma < year_ma[-1]

            # Divergence (simplified: hist shrink + price extreme)
            bottom_div = (macd_hist < 0 and abs(macd_hist) < abs(prev_hist or 0) and close < data.close[-1]) if prev_hist else False
            top_div = (macd_hist > 0 and abs(macd_hist) < abs(prev_hist or 0) and close > data.close[-1]) if prev_hist else False

            # Volume price relations (simplified)
            vol_up = volume > data.volume[-1] and close > data.close[-1]
            vol_down = volume > data.volume[-1] and close < data.close[-1]
            shrink_up = volume < data.volume[-1] and close > data.close[-1]
            shrink_down = volume < data.volume[-1] and close < data.close[-1]

            # Position size
            current_pos = self.getposition(data).size
            target_base = total_value * self.base_pos / close
            mobile_max = total_value * self.p.mobile_funds / close
            current_mobile = self.mobile_used[stock]

            # Signals based on market
            if is_uptrend:
                # Uptrend strategy
                if bottom_div or (shrink_down and avg_turnover > 0.01):  # Buy on bottom div or shrink down
                    # Add mobile in 5% increments
                    add_size = total_value * self.p.add_increment / close
                    if current_mobile + add_size <= mobile_max:
                        self.buy(data=data, size=add_size)
                        self.mobile_used[stock] += add_size
                if top_div or shrink_up:
                    # Sell mobile
                    if current_mobile > 0:
                        self.sell(data=data, size=current_mobile)
                        self.mobile_used[stock] = 0

            elif is_downtrend:
                # Downtrend
                if top_div:
                    # Reduce base if needed (reverse T)
                    reduce_size = min(current_pos * 0.5, target_base * 0.5)  # Half base
                    self.sell(data=data, size=reduce_size)
                if bottom_div:
                    # Buy back if reduced
                    self.buy(data=data, size=reduce_size)  # Assume prev reduce

            else:  # Oscillation
                if bottom_div or shrink_down:
                    add_size = total_value * self.p.add_increment / close
                    if current_pos + add_size <= total_value * 0.5 / close:  # <=50%
                        self.buy(data=data, size=add_size)
                if top_div or shrink_up:
                    self.sell(data=data, size=current_mobile or (current_pos * 0.5))

            # Risk control
            entry_price = self.getposition(data).price
            if current_pos > 0:
                if (close - entry_price) / entry_price < -self.p.stop_loss_pct:
                    self.close(data=data)  # Stop loss
                if (close - entry_price) / entry_price > self.p.profit_take_pct:
                    self.sell(data=data, size=current_pos * 0.5)  # Partial profit
                if close < year_ma * (1 - self.p.big_drop):
                    self.close(data=data)  # Trend break

# Backtest function
def run_backtest(stock_pool, start_date, end_date, initial_cash=100000):
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(initial_cash)

    # Mock data; in practice, load real data
    # Assume df_dict = {stock: pd.DataFrame with OHLCV, index=datetime}
    df_dict = {}  # Placeholder: load your data here
    for stock in stock_pool:
        # Simulate data for demo
        dates = pd.date_range(start=start_date, end=end_date, freq='B')
        df = pd.DataFrame(index=dates)
        df['Open'] = np.random.uniform(90, 110, len(dates)).cumsum() + 100
        df['High'] = df['Open'] + np.random.uniform(0, 5, len(dates))
        df['Low'] = df['Open'] - np.random.uniform(0, 5, len(dates))
        df['Close'] = (df['High'] + df['Low']) / 2
        df['Volume'] = np.random.uniform(1000, 10000, len(dates))
        data = PandasData(dataname=df, name=stock)
        cerebro.adddata(data)
        df_dict[stock] = df

    # Mock fundamentals
    roe_data = {s: np.random.uniform(5, 15) for s in stock_pool}
    pe_data = {s: np.random.uniform(10, 25) for s in stock_pool}
    debt_data = {s: np.random.uniform(30, 60) for s in stock_pool}
    mcap_data = {s: np.random.uniform(40, 600) for s in stock_pool}

    selected_stocks = select_stocks(stock_pool, roe_data, pe_data, debt_data, mcap_data)
    print(f"Selected Stocks: {selected_stocks}")

    # Add strategy
    cerebro.addstrategy(TeaTalkStrategy)

    # Analyzers
    cerebro.addanalyzer(btanalyzers.PyFolio, _name='pyfolio')
    cerebro.addanalyzer(btanalyzers.TradeAnalyzer, _name='trades')
    cerebro.addanalyzer(btanalyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(btanalyzers.AnnualReturn, _name='annualreturn')

    # Run
    results = cerebro.run()
    strat = results[0]

    # Metrics
    trades = strat.analyzers.trades.get_analysis()
    win_rate = trades.won.total / trades.total.closed * 100 if trades.total.closed > 0 else 0
    pnl_ratio = trades.pnl.won.average / abs(trades.pnl.lost.average) if trades.pnl.lost.average != 0 else 0
    max_drawdown = strat.analyzers.drawdown.get_analysis().max.drawdown
    annual_ret = list(strat.analyzers.annualreturn.get_analysis().values())[0] * 100 if strat.analyzers.annualreturn.get_analysis() else 0

    print(f"Win Rate: {win_rate:.2f}%")
    print(f"PnL Ratio: {pnl_ratio:.2f}")
    print(f"Max Drawdown: {max_drawdown:.2f}%")
    print(f"Annual Return: {annual_ret:.2f}%")

    # Plot
    cerebro.plot(style='candlestick')

# Example run
stock_pool = ['AAPL', 'GOOG', 'TSLA', 'MSFT', 'AMZN']
start_date = datetime(2020, 1, 1)
end_date = datetime(2023, 12, 31)
run_backtest(stock_pool, start_date, end_date)