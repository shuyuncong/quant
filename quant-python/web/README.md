# 量化学习

基于 Next.js 16（App Router / React 19 / TypeScript / Tailwind CSS 4 / shadcn-ui + Base UI）的本地控制台，复用 `quant-python/signal_system` 的现有分析引擎。

## 功能

- 结果：查看 analyze / scan / monitor 结果，支持详情与 AI 解读。
- 策略配置：缠论 / MACD / 周期 / 评分阈值 / watchlist / 扫描模式。
- 推送配置：微信 webhook / 通用 webhook / 邮件通道，支持测试通知。
- 模型配置：1~N 个 OpenAI 兼容接口（base_url / model / api_key / env_key / 代理地址），可测试连通性。
- 定时任务：每日扫描时间、监控间隔、仅交易日，Web 进程内调度。
- 股票池：增删改查；文本导入（粘贴解析）；图片导入（视觉模型识别后页面确认入库）。
- 我的持仓：手动维护持仓（代码/名称/份额/持仓价/总金额），个股分析与 AI 解读自动携带相关持仓。
- 操作日志：任务执行、AI 解读等操作记录，报错原因可直接在页面查看。

所有 Web 侧时间统一为北京时间（Asia/Shanghai），与 Python 引擎的 `now_shanghai()` 保持一致。

## 启动

前置：Python 3.10+（quant-python/signal_system 依赖：`pip install -r requirements.txt`），Node 20.9+。

```bash
cd quant-python/web
npm install
npm run dev
```

打开 http://localhost:3111 （也可 `npm run start` 生产模式；默认仅绑定 127.0.0.1）。

### 数据库连接（Supabase）

Web 数据库使用 Supabase PostgreSQL（`web/.env.local` 的 `DATABASE_URL`）。
开发机直连 AWS pooler 会被网络策略在 TLS 层掐断，因此 `npm run dev` 会自动
启动一个本地中继 `scripts/db-tunnel.mjs`：

- 监听 `127.0.0.1:15432`（即 `.env.local` 里 `DATABASE_URL` 的地址）；
- 通过本机 Clash HTTP 代理（`127.0.0.1:7890`）以 `CONNECT` 隧道转发到
  `aws-0-ap-southeast-1.pooler.supabase.com:5432`。

如中继端口/代理地址不同，可用环境变量覆盖后手动启动：

```bash
npm run db:tunnel                      # 默认 7890 / supabase pooler
RELAY_PROXY=127.0.0.1:7891 npm run db:tunnel   # 自定义代理
```

若 `npm run dev` 报 `connect ECONNREFUSED 127.0.0.1:15432`，说明中继未启动
（或 Clash 代理未运行）——请先确认 7890 端口有代理在监听。

Web 通过 `quant-python/signal_system/web_bridge.py` 以子进程方式调用现有引擎（分析/扫描/监控/推送/日历/outbox）。请勿删除该文件。

## 配置优先级

环境变量 > Web 数据库（web/data/app.db） > config.yaml。

- 密钥可在页面“模型配置 / 推送配置”中填写并存入本地 SQLite，也可绑定环境变量名；
- 绑定环境变量后，运行时以环境变量值优先，配置回显统一脱敏（****）；
- 常用环境变量：`TUSHARE_TOKEN`、`WECHAT_WEBHOOK_URL`、`SIGNAL_WEBHOOK_URL`、`SIGNAL_WEBHOOK_AUTH`、`SIGNAL_EMAIL_SENDER`、`SIGNAL_EMAIL_PASSWORD`、`SIGNAL_EMAIL_RECEIVER`、`SIGNAL_BARK_DEVICE_KEY`、`MODEL_TIMEOUT_SECONDS`（AI 解读单次请求超时秒数，默认 180）。

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
