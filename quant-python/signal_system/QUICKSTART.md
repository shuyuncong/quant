# 快速入门指南

## 第一步：安装依赖

```bash
cd signal_system
pip install -r requirements.txt
```

**重要**: TA-Lib 需要单独安装
- Windows: 下载 https://www.lfd.uci.edu/~gohlke/pythonlibs/#ta-lib
- 然后: `pip install TA_Lib-0.4.24-cpXX-cpXX-win_amd64.whl`

## 第二步：配置 Tushare Token

1. 注册 Tushare: https://tushare.pro/register
2. 获取 Token: https://tushare.pro/user/token
3. 编辑 `config/config.yaml`，填入你的 Token:

```yaml
data_source:
  tushare_token: "你的Token"
```

## 第三步：配置通知（可选）

### 企业微信通知

1. 创建企业微信群
2. 添加群机器人
3. 复制 webhook 地址
4. 编辑 `config/config.yaml`:

```yaml
notification:
  wechat:
    enabled: true
    webhook_url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY"
```

## 第四步：测试运行

```bash
# 测试通知功能
python main.py --test-notify

# 执行完整扫描（不发送通知）
python main.py --no-notify

# 执行完整扫描并发送通知
python main.py
```

## 第五步：查看结果

- **控制台**: 实时显示扫描进度
- **日志**: `logs/signal_system.log`
- **信号历史**: `output/signals_YYYYMMDD_HHMMSS.yaml`

## 持仓管理

创建 `data/positions.yaml` 记录持仓：

```yaml
- ts_code: "000001.SZ"
  name: "平安银行"
  buy_price: 12.50
  buy_date: "2024-01-15"
```

系统会自动检查卖出信号。

## 定时任务

### Windows 任务计划

1. 打开"任务计划程序"
2. 创建基本任务
3. 触发器: 每天 15:30
4. 操作: `python.exe`
5. 参数: `main.py`
6. 起始于: `D:\path\to\signal_system`

### Linux/Mac crontab

```bash
30 15 * * 1-5 cd /path/to/signal_system && python main.py
```

## 常见问题

### Q: 没有收到通知？
A: 运行 `python main.py --test-notify` 测试

### Q: Tushare 调用失败？
A: 检查 Token 是否正确，积分是否足够

### Q: 如何调整策略参数？
A: 编辑 `config/config.yaml`

## 下一步

1. 运行几天观察信号质量
2. 根据实际情况调整参数
3. 记录持仓，系统会自动监控

## 风险提示

⚠️ 本系统仅供学习研究，不构成投资建议
⚠️ 建议先纸面交易验证策略
⚠️ 股市有风险，投资需谨慎
