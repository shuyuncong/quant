# 部署指南

> Oracle Cloud 服务器落地部署（Nginx 反代 + HTTPS + rclone 备份）见 [DEPLOY-ORACLE.md](DEPLOY-ORACLE.md)。

本系统由两部分组成，必须一起部署：

- Web 控制台（`web/`）：Next.js 16 + Node.js 22，负责页面、配置、定时调度（node-cron）
- 信号引擎（`signal_system/`）：Python 3.10+，负责拉行情、跑缠论分析、发通知

Web 进程通过子进程调用 `signal_system/web_bridge.py`，因此部署机需要同时具备 Node.js 和 Python。定时任务由 Web 进程内部调度，**一个常驻进程即可跑完整系统**。

> 由于依赖 Python 子进程 + SQLite + 常驻调度器，**无法直接部署到 Vercel / Cloudflare Pages / Netlify 等 Serverless 平台**，建议自托管（Docker 或裸机）。

## 方式一：Docker 部署（推荐）

服务器需安装 Docker 与 Docker Compose（v2 以上）。

```bash
# 1. 拉取代码
git clone https://github.com/shuyuncong/quant.git quant && cd quant/quant-python

# 2.（可选）写环境变量，也可以在 Web 页面里配置
cp web/.env.example .env
# 编辑 .env 填入 TUSHARE_TOKEN、SIGNAL_BARK_DEVICE_KEY 等

# 3. 构建并启动（首次构建需要几分钟）
docker compose up -d --build

# 4. 查看日志 / 停止
docker compose logs -f
docker compose down
```

启动后访问 `http://服务器IP:3111`。容器会自动重启（`restart: unless-stopped`）。

### 持久化数据

`docker-compose.yml` 已用命名卷持久化以下内容，删容器不丢数据：

| 卷 | 内容 | 说明 |
| --- | --- | --- |
| `quant-data` | Web 数据库（模型/策略/推送/定时/股票池配置） | 必须保留 |
| `quant-output` | 分析/扫描结果 JSON | 必须保留 |
| `quant-cache` | 行情缓存 | 可清空，会自动重建 |
| `quant-logs` | Python 引擎日志 | 可清空 |

备份时把卷拷走即可，或直接进容器复制：

```bash
docker cp quant-web:/app/data/app.db ./app-backup.db
docker cp quant-web:/app/signal_system/output ./output-backup
```

## 方式二：裸机部署

需要 Node.js 22+ 与 Python 3.10+（3.11/3.12 均可）。

```bash
# 1. Python 依赖（默认行情源为腾讯/新浪/东财，无需额外数据源）
cd quant/quant-python/signal_system
python3 -m pip install -r requirements.txt

# 2. Web 依赖与构建
cd ../web
npm ci
npm run build

# 3. 配置环境变量（也全部支持在页面里配置）
export PYTHON_BIN=python3          # Linux 上必须指定，否则找不到 python
export WEB_DATA_DIR=/opt/quant-data
export TUSHARE_TOKEN=xxx
export SIGNAL_BARK_DEVICE_KEY=xxx

# 4. 启动（默认只监听 127.0.0.1，配合 Nginx 反代或加 -H 0.0.0.0 暴露局域网）
npm start
```

生产环境建议用 systemd 守护（`/etc/systemd/system/quant-web.service`）：

```ini
[Unit]
Description=Chan Signal Web Console
After=network.target

[Service]
WorkingDirectory=/opt/quant/quant-python/web
Environment=PYTHON_BIN=python3
Environment=WEB_DATA_DIR=/opt/quant-data
Environment=NODE_ENV=production
ExecStart=/usr/bin/node node_modules/next/dist/bin/next start -H 0.0.0.0 -p 3111
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now quant-web
sudo systemctl status quant-web
```

## 环境变量

环境变量优先于数据库/配置文件，设置后同名配置项由环境变量提供：

| 变量 | 作用 |
| --- | --- |
| `PYTHON_BIN` | Python 可执行文件（Linux 填 `python3`，Windows 可省略） |
| `WEB_DATA_DIR` | Web 数据库目录，默认 `web/data` |
| `TUSHARE_TOKEN` | Tushare Token |
| `WECHAT_WEBHOOK_URL` | 企业微信机器人 Webhook |
| `SIGNAL_WEBHOOK_URL` / `SIGNAL_WEBHOOK_AUTH` | 通用 Webhook 地址与鉴权 |
| `SIGNAL_EMAIL_SENDER` / `SIGNAL_EMAIL_PASSWORD` / `SIGNAL_EMAIL_RECEIVER` | 邮件通知 |
| `SIGNAL_BARK_DEVICE_KEY` | Bark iOS 推送设备 key（也可在页面配置） |

## 迁移现有数据

把旧机器（或本机开发环境）上的 `web/data/app.db`（或 `WEB_DATA_DIR` 下的 `app.db`）与 `signal_system/output/` 拷贝到服务器即可，无需重新配置。

以本机开发环境迁移到 Docker 部署的服务器为例：

```bash
# 在本机执行：先把数据传到服务器（SQLite WAL 模式下 app.db / -shm / -wal 三个文件要一起拷）
scp quant-python/web/data/app.db* 用户@服务器IP:/tmp/
scp -r quant-python/signal_system/output 用户@服务器IP:/tmp/

# 在服务器执行：停容器 → 拷入数据卷 → 重新启动
cd quant/quant-python
docker compose stop
docker cp /tmp/app.db quant-web:/app/data/app.db
docker cp /tmp/output quant-web:/app/signal_system/output
docker compose up -d
```

> 提示：SQLite 使用 WAL 模式，直接拷贝时请把 `app.db`、`app.db-shm`、`app.db-wal` 一并拷贝，避免丢失最近写入的数据；拷贝前最好先正常停止旧实例。

裸机部署则直接把文件放到 `WEB_DATA_DIR`（默认 `web/data`）和 `signal_system/output/` 对应位置即可。

## 常见问题

- **服务器只有 `python3` 没有 `python`**：已支持，设置 `PYTHON_BIN=python3`，Docker 镜像已内置。
- **使用 akshare / tushare 行情源**：默认 `provider: auto` 不需要它们；如需切换，在 Dockerfile 中追加安装 `signal_system/requirements-akshare.txt` 或 `requirements-tushare.txt` 后重新构建。
- **访问控制**：当前页面还没有登录密码，公网部署前务必用 Nginx Basic Auth、防火墙或后续加入的登录功能保护。
- **端口**：默认 3111，改端口请同步修改 `docker-compose.yml` 的 `ports` 或 `npm start` 参数。
