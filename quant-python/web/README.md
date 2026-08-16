# 缠论信号监控 Web 控制台

基于 Next.js 16（App Router / React 19 / TypeScript / Tailwind CSS 4 / shadcn-ui + Base UI）的本地控制台，复用 `quant-python/signal_system` 的现有分析引擎。

## 功能

- 结果：查看 analyze / scan / monitor 结果，支持详情与 AI 解读。
- 策略配置：缠论 / MACD / 周期 / 评分阈值 / watchlist / 扫描模式。
- 推送配置：微信 webhook / 通用 webhook / 邮件通道，支持测试通知。
- 模型配置：1~N 个 OpenAI 兼容接口（base_url / model / api_key / env_key），可测试连通性。
- 定时任务：每日扫描时间、监控间隔、仅交易日，Web 进程内调度。
- 股票池：增删改查；文本导入（粘贴解析）；图片导入（视觉模型识别后页面确认入库）。

## 启动

前置：Python 3.10+（quant-python/signal_system 依赖：`pip install -r requirements.txt`），Node 20.9+。

```bash
cd quant-python/web
npm install
npm run dev
```

打开 http://localhost:3111 （也可 `npm run start` 生产模式；默认仅绑定 127.0.0.1）。

Web 通过 `quant-python/signal_system/web_bridge.py` 以子进程方式调用现有引擎（分析/扫描/监控/推送/日历/outbox）。请勿删除该文件。

## 配置优先级

环境变量 > Web 数据库（web/data/app.db） > config.yaml。

- 密钥可在页面“模型配置 / 推送配置”中填写并存入本地 SQLite，也可绑定环境变量名；
- 绑定环境变量后，运行时以环境变量值优先，配置回显统一脱敏（****）；
- 常用环境变量：`TUSHARE_TOKEN`、`WECHAT_WEBHOOK_URL`、`SIGNAL_WEBHOOK_URL`、`SIGNAL_WEBHOOK_AUTH`、`SIGNAL_EMAIL_SENDER`、`SIGNAL_EMAIL_PASSWORD`、`SIGNAL_EMAIL_RECEIVER`、`SIGNAL_BARK_DEVICE_KEY`。

## 安全说明

- v1 仅面向本地使用（127.0.0.1），未内置登录；上线前必须启用访问鉴权。
- API Key 以明文存于本地 SQLite（web/data/app.db），建议敏感密钥走环境变量。
- 所有返回给前端的密钥值均已脱敏。

## 测试

```bash
npm test        # vitest（配置优先级/符号/LLM/调度）
npm run lint
npm run build
cd ../signal_system && python -m unittest discover -s tests -q
```
