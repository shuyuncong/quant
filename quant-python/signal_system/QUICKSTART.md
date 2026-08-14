# 快速启动

```powershell
cd D:\development\github\quant\quant-python\signal_system
python -m pip install -r requirements.txt
python main.py analyze --symbols 000001.SZ --no-notify
```

配置企业微信或通用 Webhook 后测试：

```powershell
$env:WECHAT_WEBHOOK_URL = "你的企业微信机器人地址"
python main.py test-notify
```

常驻监控：

```powershell
python main.py monitor
```

全市场 0 轴金叉筛选需要把 `config/config.yaml` 中的 `scan.universe_mode` 改为 `all_a`，然后多次运行 `python main.py scan` 完成首次历史回填。详细说明见 [README.md](README.md)。
