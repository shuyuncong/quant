# 量化交易信号系统

一个基于 Python 的量化交易信号生成系统，实现自动化选股、技术分析和信号推送。

## 功能特性

- ✅ **数据获取**: 支持 Tushare 数据源，带缓存机制
- ✅ **基本面筛选**: 三高一低过滤（负债率、应收账款、财务费用、现金流）
- ✅ **技术指标**: MACD、均线、RSI、背离检测
- ✅ **信号生成**: 买入/卖出信号自动生成
- ✅ **风险控制**: 止损止盈、仓位管理、市场环境判断
- ✅ **通知推送**: 企业微信、邮件通知
- ✅ **历史记录**: 信号历史保存

## 系统架构

```
signal_system/
├── config/              # 配置文件
│   └── config.yaml
├── data/                # 数据获取模块
│   └── data_fetcher.py
├── strategy/            # 策略引擎
│   ├── indicators.py    # 技术指标
│   └── strategy_engine.py
├── notification/        # 通知模块
│   └── notifier.py
├── utils/               # 工具函数
│   └── helpers.py
├── main.py             # 主程序
└── requirements.txt    # 依赖包
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

**注意**: TA-Lib 需要单独安装：
- Windows: 下载 whl 文件 https://www.lfd.uci.edu/~gohlke/pythonlibs/#ta-lib
- Linux/Mac: `brew install ta-lib` 或从源码编译

### 2. 配置系统

编辑 `config/config.yaml`:

```yaml
# 必须配置
data_source:
  tushare_token: "你的Tushare_Token"  # 在 https://tushare.pro 注册获取

# 可选配置
notification:
  wechat:
    enabled: true
    webhook_url: "你的企业微信机器人webhook"
```

### 3. 运行系统

```bash
# 执行每日扫描（发送通知）
python main.py

# 执行扫描但不发送通知
python main.py --no-notify

# 测试通知功能
python main.py --test-notify
```

## 配置说明

### 数据源配置

```yaml
data_source:
  tushare_token: "你的Token"
  use_cache: true          # 是否使用缓存
  cache_dir: "./cache"     # 缓存目录
```

### 策略参数

```yaml
strategy:
  fundamental:
    min_roe: 10           # 最小ROE (%)
    max_debt_ratio: 50    # 最大负债率 (%)
    max_pe: 30            # 最大市盈率

  technical:
    ma_period: 250        # 年线周期
    macd_fast: 12
    macd_slow: 26

  volume:
    min_turnover_rate: 1  # 最小换手率 (%)
    max_turnover_rate: 5  # 最大换手率 (%)
```

### 风控参数

```yaml
risk_control:
  position:
    max_total_position: 0.8      # 最大总仓位
    max_single_position: 0.25    # 单只股票最大仓位

  stop_loss: 0.08               # 止损比例 8%
  stop_profit: 0.30             # 止盈比例 30%
```

## 使用流程

### 1. 首次运行

```bash
# 测试通知功能
python main.py --test-notify

# 如果通知成功，执行完整扫描
python main.py
```

### 2. 查看结果

- **控制台输出**: 实时显示扫描进度和结果
- **日志文件**: `logs/signal_system.log`
- **信号历史**: `output/signals_YYYYMMDD_HHMMSS.yaml`

### 3. 管理持仓

创建 `data/positions.yaml` 文件记录持仓：

```yaml
- ts_code: "000001.SZ"
  name: "平安银行"
  buy_price: 12.50
  buy_date: "2024-01-15"

- ts_code: "600036.SH"
  name: "招商银行"
  buy_price: 35.80
  buy_date: "2024-01-20"
```

系统会自动检查持仓的卖出信号。

## 定时任务

### Linux/Mac (crontab)

```bash
# 每个交易日 15:30 执行
30 15 * * 1-5 cd /path/to/signal_system && python main.py
```

### Windows (任务计划程序)

1. 打开"任务计划程序"
2. 创建基本任务
3. 触发器: 每天 15:30
4. 操作: 启动程序 `python.exe`
5. 参数: `main.py`
6. 起始于: `D:\path\to\signal_system`

## 通知配置

### 企业微信机器人

1. 创建企业微信群
2. 添加群机器人
3. 复制 webhook 地址到配置文件

### 邮件通知

```yaml
notification:
  email:
    enabled: true
    smtp_server: "smtp.qq.com"
    smtp_port: 465
    sender: "your@email.com"
    password: "授权码"  # QQ邮箱需要使用授权码
    receiver: "your@email.com"
```

## 策略说明

### 选股逻辑

系统采用"三条腿"原则：

1. **基本面**: ROE>10%, 负债率<50%, PE<30, 市值50-500亿
2. **技术面**: 年线向上, 回调至年线附近, 底背离
3. **成交量**: 换手率1-5%, 放量突破

### 买入信号

- 年线向上 + 回调至年线 + 底背离 + 放量
- MACD金叉 + 年线向上 + 放量
- 评分机制: 满足条件越多，评分越高

### 卖出信号

- 止损: 亏损超过8%
- 止盈: 盈利超过30%
- 顶背离
- 破年线（年线向下）

### 市场环境判断

- **牛市**: 上证指数年线向上 + 价格在年线上 + MACD>0
- **熊市**: 上证指数年线向下 + 价格在年线下 + MACD<0
- **震荡**: 其他情况

不同市场环境采用不同仓位策略。

## 注意事项

1. **数据源**: Tushare 免费版有调用限制，建议使用缓存
2. **回测**: 本系统为信号提示系统，不包含回测功能
3. **风险**: 量化信号仅供参考，不构成投资建议
4. **实盘**: 建议先纸面交易验证策略有效性

## 常见问题

### Q: TA-Lib 安装失败？

A: Windows 用户下载对应版本的 whl 文件安装：
```bash
pip install TA_Lib-0.4.24-cp39-cp39-win_amd64.whl
```

### Q: Tushare 调用失败？

A: 检查：
1. Token 是否正确
2. 积分是否足够
3. 是否超过调用频率限制

### Q: 没有收到通知？

A: 检查：
1. webhook 地址是否正确
2. 网络是否正常
3. 运行 `python main.py --test-notify` 测试

### Q: 如何调整策略参数？

A: 编辑 `config/config.yaml`，调整对应参数后重新运行。

## 后续优化

- [ ] 支持更多数据源 (AkShare, 聚宽)
- [ ] 添加回测功能
- [ ] Web 界面
- [ ] 数据库存储
- [ ] 更多技术指标
- [ ] 机器学习模型

## 许可证

MIT License

## 免责声明

本系统仅用于学习和研究目的，不构成任何投资建议。
使用本系统进行实盘交易的风险由使用者自行承担。
股市有风险，投资需谨慎。
