import backtrader as bt
import pandas as pd
import numpy as np
import datetime
from backtrader.feeds import PandasData
import matplotlib.pyplot as plt
import talib

# =========================
# 1. 自定义数据类 - 支持基本面数据
# =========================
class FundamentalPandasData(PandasData):
    """
    扩展PandasData以支持基本面数据字段
    """
    lines = ('roe', 'pe', 'pb', 'debt_ratio', 'cash_flow', 'market_cap')
    
    # 加入到lines中，使其在策略中可访问
    params = (
        ('roe', -1),
        ('pe', -1),
        ('pb', -1),
        ('debt_ratio', -1),
        ('cash_flow', -1),
        ('market_cap', -1),
    )

# =========================
# 2. 选股模块实现
# =========================
def tea_talk_screening(data):
    """
    茶话三条腿选股原则实现
    :param data: 包含股票数据的DataFrame，需包含技术指标和基本面数据
    :return: 符合条件的股票列表
    """
    selected_stocks = []
    
    for stock, df in data.items():
        try:
            # 1. 技术面筛选
            # 年线向上
            df['sma250'] = df['close'].rolling(250).mean()
            tech_upward = df['sma250'].iloc[-1] > df['sma250'].iloc[-2] and df['sma250'].iloc[-2] > df['sma250'].iloc[-3]
            
            # MACD背离检测（简化版）
            df['macd'], df['macd_signal'], df['macd_hist'] = talib.MACD(df['close'], fastperiod=12, slowperiod=26, signalperiod=9)
            # 底背离：价格新低但MACD柱面积增大
            price_lower = df['close'].iloc[-1] < df['close'].iloc[-2] and df['close'].iloc[-2] < df['close'].iloc[-3]
            macd_higher = df['macd_hist'].iloc[-1] > df['macd_hist'].iloc[-2] and df['macd_hist'].iloc[-2] > df['macd_hist'].iloc[-3]
            tech_bottom_divergence = price_lower and macd_higher
            
            # 低位放量滞涨
            volume_ma = df['volume'].rolling(20).mean()
            volume_ratio = df['volume'].iloc[-1] / volume_ma.iloc[-1]
            price_stagnation = abs(df['close'].pct_change(5).iloc[-1]) < 0.05  # 5日涨幅小于5%
            tech_volume_signal = volume_ratio > 1.5 and price_stagnation
            
            # 2. 基本面筛选
            # 三高一低防雷标准
            high_debt = df['debt_ratio'].iloc[-1] > 0.5  # 负债率>50%
            high_receivables = df['receivables'].iloc[-1] > df['cash'].iloc[-1]  # 应收账款>现金
            high_fin_exp = df['fin_exp'].iloc[-1] > df['net_profit'].iloc[-1]  # 财务费用>净利润
            low_cash_flow = df['cash_flow'].iloc[-1] < 0.5 * df['net_profit'].iloc[-1]  # 经营现金流<50%净利润
            
            fundamental_safe = not (high_debt or high_receivables or high_fin_exp or low_cash_flow)
            roe_good = df['roe'].iloc[-1] > 0.1  # ROE>10%
            pe_reasonable = 0 < df['pe'].iloc[-1] < 30  # PE在合理区间
            
            # 3. 成交量筛选
            turnover = df['volume'] * df['close'] / df['float_shares']  # 简化换手率计算
            turnover_ma = turnover.rolling(30).mean()
            volume_good = 0.01 < turnover_ma.iloc[-1] < 0.05  # 1%-5%的换手率
            
            # 综合判断：三条腿原则
            if (tech_upward or tech_bottom_divergence or tech_volume_signal) and \
               fundamental_safe and roe_good and pe_reasonable and \
               volume_good:
                selected_stocks.append(stock)
                
        except Exception as e:
            print(f"筛选{stock}时出错: {str(e)}")
            continue
    
    # 限制最多选择4只股票
    return selected_stocks[:4]

# =========================
# 3. 市场状态检测器
# =========================
def detect_market_regime(data):
    """
    检测当前市场状态：上涨、下跌或震荡
    :param data: 市场指数数据 (如沪深300)
    :return: 'up', 'down', 'sideways'
    """
    # 计算关键指标
    sma50 = data['close'].rolling(50).mean()
    sma200 = data['close'].rolling(200).mean()
    adx = talib.ADX(data['high'], data['low'], data['close'], timeperiod=14)
    
    # 趋势强度
    trend_strength = adx.iloc[-1] if len(adx) > 0 else 0
    
    # 判断市场状态
    if sma50.iloc[-1] > sma200.iloc[-1] * 1.02 and trend_strength > 25:
        return 'up'
    elif sma50.iloc[-1] < sma200.iloc[-1] * 0.98 and trend_strength > 25:
        return 'down'
    else:
        return 'sideways'

# =========================
# 4. 交易策略核心实现
# =========================
class TeaTalkStrategy(bt.Strategy):
    """
    茶话三条腿交易策略核心实现
    """
    params = (
        ('max_position_per_stock', 0.4),  # 单只股票最大仓位40%
        ('base_position', 0.25),           # 基础仓位25%
        ('min_cash', 0.25),                # 最小现金比例25%（机动资金）
        ('stop_loss_pct', 0.08),           # 止损比例8%
        ('profit_protect_pct', 0.2),       # 盈利保护比例20%
        ('market_index', None),            # 用于市场状态检测的指数数据
    )

    def __init__(self):
        # 初始化指标
        self.sma250 = {}
        self.macd = {}
        self.macd_signal = {}
        self.macd_hist = {}
        self.volume_ma = {}
        
        for d in self.datas:
            # 技术指标
            self.sma250[d] = bt.indicators.SMA(d.close, period=250)
            self.macd[d], self.macd_signal[d], self.macd_hist[d] = bt.indicators.MACD(
                d.close, period_me1=12, period_me2=26, period_signal=9)
            self.volume_ma[d] = bt.indicators.SMA(d.volume, period=20)
            
            # 背离检测
            self.bottom_divergence = False
            self.top_divergence = False
            
        # 状态跟踪
        self.market_regime = 'sideways'  # 当前市场状态
        self.entry_prices = {}  # 记录入场价格
        self.hold_days = {}     # 记录持仓天数
        
        # 初始化状态字典
        for d in self.datas:
            self.entry_prices[d] = 0
            self.hold_days[d] = 0

    def next(self):
        # 1. 每日开始更新市场状态
        if self.params.market_index:
            self.market_regime = detect_market_regime(self.params.market_index)
        
        # 2. 为每个股票生成交易信号
        for d in self.datas:
            # 跳过没有足够数据的股票
            if len(d) < 250:
                continue
                
            # 2.1 生成交易信号
            buy_signal = self._generate_buy_signal(d)
            sell_signal = self._generate_sell_signal(d)
            
            # 2.2 执行交易决策
            if buy_signal:
                self._execute_buy(d)
            elif sell_signal:
                self._execute_sell(d)
                
            # 2.3 更新持仓信息
            if self.getposition(d).size > 0:
                self.hold_days[d] += 1
                if self.entry_prices[d] == 0:
                    self.entry_prices[d] = d.close[0]
            else:
                self.hold_days[d] = 0
                self.entry_prices[d] = 0

    def _generate_buy_signal(self, data):
        """生成买入信号"""
        # 1. 基本面过滤（假设数据中包含基本面指标）
        if hasattr(data, 'debt_ratio') and data.debt_ratio[0] > 0.5:
            return False  # 负债率过高
        
        # 2. 技术面信号
        # 年线向上
        sma250_up = self.sma250[data][-1] > self.sma250[data][-2] > self.sma250[data][-3]
        
        # 战略买入点：回调至年线附近
        price_near_sma250 = abs(data.close[0] - self.sma250[data][0]) / self.sma250[data][0] < 0.05
        
        # 底背离信号
        price_lower = data.close[0] < data.close[-1] < data.close[-2]
        macd_higher = self.macd_hist[data][0] > self.macd_hist[data][-1] > self.macd_hist[data][-2]
        bottom_divergence = price_lower and macd_higher
        
        # 低位放量滞涨
        volume_ratio = data.volume[0] / self.volume_ma[data][0] if self.volume_ma[data][0] > 0 else 0
        price_stagnation = abs(data.close[0] - data.close[-5]) / data.close[-5] < 0.05  # 5日涨幅<5%
        volume_signal = volume_ratio > 1.5 and price_stagnation
        
        # 3. 根据市场状态调整信号
        if self.market_regime == 'up':
            # 上涨趋势：买入条件宽松
            return (sma250_up and (price_near_sma250 or bottom_divergence)) or volume_signal
        elif self.market_regime == 'down':
            # 下跌趋势：只考虑明显底背离
            return bottom_divergence
        else:  # 震荡市
            # 震荡市：低位支撑位或底背离
            return (bottom_divergence or price_near_sma250) and volume_signal

    def _generate_sell_signal(self, data):
        """生成卖出信号"""
        # 没有持仓则无需卖出
        if self.getposition(data).size <= 0:
            return False
            
        # 1. 动态止损
        if self.entry_prices[data] > 0:
            loss_pct = (data.close[0] - self.entry_prices[data]) / self.entry_prices[data]
            if loss_pct < -self.p.stop_loss_pct:
                return True
                
        # 2. 盈利保护
        profit_pct = (data.close[0] - self.entry_prices[data]) / self.entry_prices[data] if self.entry_prices[data] > 0 else 0
        if profit_pct > self.p.profit_protect_pct and self.hold_days[data] > 5:
            # 已有20%以上盈利且持有5天以上
            pass  # 进入下一步判断
            
        # 3. 技术面信号
        # 顶背离信号
        price_higher = data.close[0] > data.close[-1] > data.close[-2]
        macd_lower = self.macd_hist[data][0] < self.macd_hist[data][-1] < self.macd_hist[data][-2]
        top_divergence = price_higher and macd_lower
        
        # 高位放量滞涨
        volume_ratio = data.volume[0] / self.volume_ma[data][0] if self.volume_ma[data][0] > 0 else 0
        price_stagnation = abs(data.close[0] - data.close[-5]) / data.close[-5] < 0.05
        volume_signal = volume_ratio > 2.0 and price_stagnation
        
        # 4. 根据市场状态调整信号
        if self.market_regime == 'up':
            # 上涨趋势：卖出条件严格
            return top_divergence or (volume_signal and profit_pct > 0.3)  # 翻倍以上或顶背离
        elif self.market_regime == 'down':
            # 下跌趋势：卖出条件宽松
            return top_divergence or volume_signal
        else:  # 震荡市
            # 震荡市：顶背离或高位放量
            return top_divergence or volume_signal

    def _execute_buy(self, data):
        """执行买入操作（考虑仓位管理）"""
        # 检查是否已有持仓
        position = self.getposition(data)
        current_size = position.size
        
        # 如果已有持仓，只考虑加仓机动部分
        if current_size > 0:
            # 计算是否可以加仓（不超过最大仓位）
            current_value = current_size * data.close[0]
            total_value = self.broker.getvalue()
            current_pct = current_value / total_value
            
            if current_pct >= self.p.max_position_per_stock:
                return  # 已达到最大仓位，不再加仓
                
            # 计算可加仓金额（机动资金部分）
            available_cash = self.broker.getcash() * (1 - self.p.min_cash)
            add_size = available_cash / data.close[0]
            
            # 限制加仓幅度为5%
            max_add_pct = 0.05 * total_value / data.close[0]
            add_size = min(add_size, max_add_pct)
            
            if add_size > 0:
                self.buy(data, size=add_size)
        else:
            # 新建仓，使用基础仓位
            total_value = self.broker.getvalue()
            target_value = total_value * self.p.base_position
            size = target_value / data.close[0]
            
            if size > 0:
                self.buy(data, size=size)
                self.entry_prices[data] = data.close[0]  # 记录入场价

    def _execute_sell(self, data):
        """执行卖出操作（考虑仓位管理）"""
        position = self.getposition(data)
        current_size = position.size
        
        if current_size <= 0:
            return
            
        # 如果是盈利保护卖出，只卖机动部分
        profit_pct = (data.close[0] - self.entry_prices[data]) / self.entry_prices[data] if self.entry_prices[data] > 0 else 0
        
        if profit_pct > self.p.profit_protect_pct:
            # 卖出机动部分（总仓位减去基础仓位）
            total_value = self.broker.getvalue()
            base_size = int(total_value * self.p.base_position / data.close[0])
            sell_size = max(0, current_size - base_size)
        else:
            # 止损或趋势反转，全部卖出
            sell_size = current_size
            
        if sell_size > 0:
            self.sell(data, size=sell_size)

    def stop(self):
        """回测结束时的统计"""
        print('=== 策略回测结果 ===')
        print(f'最终资金: {self.broker.getvalue():.2f}')
        print(f'总收益率: {(self.broker.getvalue() / self.broker.startingcash - 1) * 100:.2f}%')
        
        # 计算年化收益率
        days = len(self)
        years = days / 252
        annual_return = (self.broker.getvalue() / self.broker.startingcash) ** (1/years) - 1
        print(f'年化收益率: {annual_return * 100:.2f}%')
        
        # 计算最大回撤
        max_drawdown = 0
        peak = self.broker.startingcash
        for i in range(days):
            value = self.broker.get_value(i)
            if value > peak:
                peak = value
            else:
                drawdown = (peak - value) / peak
                max_drawdown = max(max_drawdown, drawdown)
        print(f'最大回撤: {max_drawdown * 100:.2f}%')

# =========================
# 5. 仓位管理模块
# =========================
def tea_talk_position_sizer(strategy, data, cash, target_percent):
    """
    茶话仓位管理器
    :param strategy: 策略实例
    :param data: 数据
    :param cash: 可用现金
    :param target_percent: 目标仓位比例
    :return: 应买入的股数
    """
    # 计算目标价值
    target_value = strategy.broker.getvalue() * target_percent
    
    # 确保不超过单只股票最大仓位
    current_value = strategy.getposition(data).size * data.close[0]
    if current_value / strategy.broker.getvalue() > strategy.params.max_position_per_stock:
        return 0
    
    # 计算需要买入的价值
    buy_value = target_value - current_value
    if buy_value <= 0:
        return 0
    
    # 考虑机动资金限制
    available_cash = cash * (1 - strategy.params.min_cash)
    buy_value = min(buy_value, available_cash)
    
    # 转换为股数
    size = buy_value / data.close[0]
    return size

# =========================
# 6. 回测执行与可视化
# =========================
def run_backtest(stock_data, market_index_data=None, start_date=None, end_date=None, initial_cash=100000):
    """
    运行回测并生成结果
    :param stock_data: 股票数据字典 {股票代码: DataFrame}
    :param market_index_data: 市场指数数据 (用于市场状态检测)
    :param start_date: 回测开始日期
    :param end_date: 回测结束日期
    :param initial_cash: 初始资金
    :return: 回测结果
    """
    # 1. 选股
    selected_stocks = tea_talk_screening(stock_data)
    print(f"选股结果: {selected_stocks}")
    
    if not selected_stocks:
        print("未选出符合条件的股票，无法进行回测")
        return None
    
    # 2. 准备数据
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(initial_cash)
    cerebro.broker.setcommission(commission=0.001)  # 0.1%交易佣金
    
    # 添加股票数据
    for stock in selected_stocks:
        df = stock_data[stock].copy()
        
        # 确保日期索引正确
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        
        # 过滤日期范围
        if start_date:
            df = df[df.index >= start_date]
        if end_date:
            df = df[df.index <= end_date]
            
        # 创建数据源
        data = FundamentalPandasData(
            dataname=df,
            name=stock,
            timeframe=bt.TimeFrame.Days
        )
        cerebro.adddata(data)
    
    # 3. 添加策略
    cerebro.addstrategy(TeaTalkStrategy, market_index=market_index_data)
    
    # 4. 添加分析器
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    
    # 5. 运行回测
    print(f"初始资金: {cerebro.broker.getvalue():.2f}")
    results = cerebro.run()
    strat = results[0]
    
    # 6. 打印分析结果
    print('\n===== 回测分析 =====')
    print(f'最终资金: {cerebro.broker.getvalue():.2f}')
    
    # 计算关键指标
    sharpe = strat.analyzers.sharpe.get_analysis()['sharperatio']
    drawdown = strat.analyzers.drawdown.get_analysis()['max']['drawdown']
    total_return = strat.analyzers.returns.get_analysis()['rtot']
    trades = strat.analyzers.trades.get_analysis()
    
    print(f'年化收益率: {total_return * 100:.2f}%')
    print(f'夏普比率: {sharpe:.2f}')
    print(f'最大回撤: {drawdown:.2f}%')
    
    # 检查是否达到策略目标
    success = True
    if total_return < 0.15:
        print(f"⚠️ 未达标: 年化收益率 {total_return*100:.2f}% < 15%")
        success = False
    if drawdown > 20:
        print(f"⚠️ 未达标: 最大回撤 {drawdown:.2f}% > 20%")
        success = False
    
    if success:
        print("✅ 策略满足所有回测关键指标要求")
    
    # 7. 绘制回测结果
    cerebro.plot(style='candlestick', barup='green', bardown='red', 
                 figsize=(14, 7), title='茶话三条腿策略回测结果')
    
    return results

# =========================
# 7. 信号生成工具 - 用于实盘信号
# =========================
def generate_trading_signals(stock_data, market_index_data=None):
    """
    生成交易信号（用于实盘）
    :param stock_data: 当前股票数据 {股票代码: DataFrame}
    :param market_index_data: 市场指数数据
    :return: 信号列表 [(股票代码, 信号类型, 理由)]
    """
    signals = []
    
    # 检测市场状态
    market_regime = detect_market_regime(market_index_data) if market_index_data else 'sideways'
    
    for stock, df in stock_data.items():
        try:
            # 创建策略实例（仅用于信号生成）
            cerebro = bt.Cerebro()
            cerebro.broker.setcash(100000)
            
            # 准备数据
            data = FundamentalPandasData(
                dataname=df,
                name=stock,
                timeframe=bt.TimeFrame.Days
            )
            cerebro.adddata(data)
            
            # 添加策略
            strategy = TeaTalkStrategy
            cerebro.addstrategy(strategy, market_index=market_index_data)
            
            # 运行到最新数据
            results = cerebro.run(runonce=False)
            strat = results[0]
            
            # 获取最新数据
            d = strat.datas[0]
            
            # 生成信号
            buy_signal = strat._generate_buy_signal(d)
            sell_signal = strat._generate_sell_signal(d)
            
            if buy_signal:
                reason = "战略买入点: "
                if abs(d.close[0] - strat.sma250[d][0]) / strat.sma250[d][0] < 0.05:
                    reason += "年线附近; "
                if (d.close[0] < d.close[-1] < d.close[-2]) and (strat.macd_hist[d][0] > strat.macd_hist[d][-1] > strat.macd_hist[d][-2]):
                    reason += "底背离; "
                if d.volume[0] / strat.volume_ma[d][0] > 1.5 and abs(d.close[0] - d.close[-5]) / d.close[-5] < 0.05:
                    reason += "低位放量滞涨"
                signals.append((stock, "BUY", reason, market_regime))
            elif sell_signal:
                reason = "卖出信号: "
                if (d.close[0] > d.close[-1] > d.close[-2]) and (strat.macd_hist[d][0] < strat.macd_hist[d][-1] < strat.macd_hist[d][-2]):
                    reason += "顶背离; "
                if d.volume[0] / strat.volume_ma[d][0] > 2.0 and abs(d.close[0] - d.close[-5]) / d.close[-5] < 0.05:
                    reason += "高位放量滞涨; "
                if (d.close[0] - strat.entry_prices.get(d, 0)) / strat.entry_prices.get(d, 1) > 0.2:
                    reason += "盈利保护"
                signals.append((stock, "SELL", reason, market_regime))
                
        except Exception as e:
            print(f"生成{stock}信号时出错: {str(e)}")
            continue
    
    return signals

# =========================
# 8. 示例用法
# =========================
if __name__ == '__main__':
    # 示例：加载数据（实际使用时需要替换为真实数据）
    def load_sample_data():
        """加载示例数据（实际应用中替换为真实数据源）"""
        # 这里应该从数据库或API加载真实数据
        # 以下为模拟数据生成
        
        # 模拟沪深300指数数据（用于市场状态检测）
        dates = pd.date_range(start='2020-01-01', end='2023-12-31', freq='D')
        market_data = pd.DataFrame(index=dates)
        market_data['open'] = 3500 + np.random.randn(len(dates)).cumsum() * 5
        market_data['high'] = market_data['open'] * 1.01
        market_data['low'] = market_data['open'] * 0.99
        market_data['close'] = (market_data['open'] + market_data['high'] + market_data['low']) / 3
        market_data['volume'] = np.random.randint(1000000, 5000000, len(dates))
        
        # 模拟几只股票数据
        stock_data = {}
        
        # 茶话案例中的中鼎股份
        df = pd.DataFrame(index=dates)
        df['open'] = 10 + np.random.randn(len(dates)).cumsum() * 0.1
        df['high'] = df['open'] * 1.02
        df['low'] = df['open'] * 0.98
        df['close'] = (df['open'] + df['high'] + df['low']) / 3
        df['volume'] = np.random.randint(500000, 2000000, len(dates))
        # 添加基本面数据
        df['roe'] = 0.12  # ROE 12%
        df['pe'] = 15     # PE 15
        df['debt_ratio'] = 0.4  # 负债率40%
        df['cash_flow'] = 0.8   # 经营现金流良好
        df['market_cap'] = 100  # 市值100亿
        stock_data['000001.SZ'] = df
        
        # 另一只股票
        df = pd.DataFrame(index=dates)
        df['open'] = 50 + np.random.randn(len(dates)).cumsum() * 0.5
        df['high'] = df['open'] * 1.02
        df['low'] = df['open'] * 0.98
        df['close'] = (df['open'] + df['high'] + df['low']) / 3
        df['volume'] = np.random.randint(1000000, 3000000, len(dates))
        # 添加基本面数据
        df['roe'] = 0.08  # ROE 8%
        df['pe'] = 25     # PE 25
        df['debt_ratio'] = 0.6  # 负债率60% (不符合)
        df['cash_flow'] = 0.5   # 经营现金流一般
        df['market_cap'] = 300  # 市值300亿
        stock_data['600000.SS'] = df
        
        return stock_data, market_data
    
    # 加载数据
    stock_data, market_data = load_sample_data()
    
    # 运行回测
    print("="*50)
    print("开始回测...")
    run_backtest(
        stock_data=stock_data,
        market_index_data=market_data,
        start_date=datetime.datetime(2020, 1, 1),
        end_date=datetime.datetime(2023, 12, 31),
        initial_cash=100000
    )
    
    # 生成交易信号（实盘使用）
    print("\n" + "="*50)
    print("生成交易信号...")
    signals = generate_trading_signals(
        stock_data={k: v.copy().tail(100) for k, v in stock_data.items()},  # 只用最近100天数据
        market_index_data=market_data.copy().tail(100)
    )
    
    print("\n今日交易信号:")
    for stock, signal, reason, regime in signals:
        print(f"{stock}: {signal} | 市场状态: {regime} | 理由: {reason}")