import pandas as pd
import numpy as np

# 生成一些模拟K线数据
def generate_dummy_data(filename="dummy_data.csv"):
    dates = pd.to_datetime(pd.date_range(start="2022-01-01", periods=500))
    data = pd.DataFrame(index=dates)
    
    price = 100
    prices = []
    volumes = []
    
    for _ in range(500):
        # 制造一些趋势和波动
        price += np.random.randn() * 2 + np.sin(_ / 50) * 2
        price = max(price, 20) # 保证价格不为负
        prices.append(price)
        volumes.append(np.random.randint(100000, 500000) * (1 + np.sin(_ / 20) * 0.5))
        
    data['Open'] = [p - np.random.random() for p in prices]
    data['High'] = [p + np.random.random() * 2 for p in prices]
    data['Low'] = [p - np.random.random() * 2 for p in prices]
    data['Close'] = prices
    data['Volume'] = volumes
    
    # 调整为backtrader期望的格式
    data.index.name = 'datetime'
    data.to_csv(filename)
    print(f"虚拟数据已生成: {filename}")

# 运行一次即可
# generate_dummy_data()