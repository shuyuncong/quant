# 部署指南

> Oracle Cloud 服务器落地部署（Nginx 反代 + HTTPS + rclone 备份）见 [DEPLOY-ORACLE.md](../docs/DEPLOY-ORACLE.md)。

本系统由两部分组成，必须一起部署：

- Web 控制台（`web/`）：Next.js 16 + Node.js 22，负责页面、配置、定时调度（node-cron）
- 信号引擎（`signal_system/`）：Python 3.10+，负责拉行情、跑缠论分析、发通知

Web 进程通过子进程调用 `signal_system/web_bridge.py`，因此部署机需要同时具备 Node.js 和 Python。定时任务由 Web 进程内部调度，**一个常驻进程即可跑完整系统**。

> 由于依赖 Python 子进程 + 本地信号 SQLite + 常驻调度器，**无法直接部署到 Vercel / Cloudflare Pages / Netlify 等 Serverless 平台**，建议自托管（Docker 或裸机）。Web 业务数据存于 PostgreSQL（推荐同机自建 `quant-db`，也可用 Supabase / 外部 PG）。

## PostgreSQL 连接

Web 控制台通过 `DATABASE_URL` 连接 PostgreSQL，`DATABASE_URL` 没有默认值。Docker/Oracle 部署时把它放在 Compose 同目录的 `.env`，不要提交到 Git。

**方式一（推荐）：同机自建 `quant-db`（Docker 内部网络，不暴露端口，无传输配额限制）**

需叠加 DB override（`docker-compose.db.yml` / `docker-compose.oracle.db.yml`）并设置部署模式：

```dotenv
# 应用以受限角色 quant_app 连接（非超级用户）
DATABASE_URL=postgresql://quant_app:<密码>@quant-db:5432/quant
DATABASE_SSL_MODE=disable
DB_MODE=selfhost
# 三角色密码（quant_owner 建库/DDL；quant_app 应用；quant_backup 备份只读）
PG_OWNER_PASSWORD=<owner 密码>
PG_APP_PASSWORD=<应用密码>
PG_BACKUP_PASSWORD=<备份密码>
```

> 密码限字母/数字/`_`（受控角色初始化与 URL 拼串要求，见 `docs/自建数据库切换方案.md`）。

切换 / 迁移步骤见 `docs/自建数据库切换方案.md`。

**方式二：Supabase / 外部 PostgreSQL**

```dotenv
DATABASE_URL=postgresql://postgres.qutqrxicwrnorvujdrvp:<URL编码后的密码>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
DATABASE_SSL_REJECT_UNAUTHORIZED=true
DATABASE_POOL_MAX=5
```

`DATABASE_SSL_REJECT_UNAUTHORIZED` 默认是 `true`，`DATABASE_POOL_MAX` 默认是 `5`。Supabase Publishable/Secret key 不需要配置；开发环境已禁用生产库 relay，只使用本地 Docker PostgreSQL。

> 从 Supabase 切换到自建库的完整步骤（停写窗口、迁移、验证、回滚、备份）见 `docs/自建数据库切换方案.md`。

## 方式一：Docker 部署（推荐）

服务器需安装 Docker 与 Docker Compose（v2 以上）。

以下完整命令以首次部署自建库为例；外部 PostgreSQL / Supabase 使用主 compose，不执行角色初始化。

```bash
# 1. 拉取代码
git clone https://github.com/shuyuncong/quant.git quant && cd quant/quant-python

# 2. 配置 PostgreSQL 连接和可选通知变量（DATABASE_URL 必填；自建库还需三角色密码）
#    推荐同机自建 quant-db（叠加 db override，DB_MODE=selfhost）：
#    DATABASE_URL=postgresql://quant_app:<密码>@quant-db:5432/quant，DATABASE_SSL_MODE=disable
cp web/.env.example .env
# 编辑 .env 填入 DATABASE_URL/PG_OWNER_PASSWORD/PG_APP_PASSWORD/PG_BACKUP_PASSWORD，
# 以及需要的 TUSHARE_TOKEN、SIGNAL_BARK_DEVICE_KEY 等
chmod 600 .env

# Compose 会读取 .env 做插值，但不会把值导出到宿主 shell；仅提取需要的三项。
get_env() {
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\([^#[:space:]]*\).*/\1/p" ./.env | tail -n1
}
PG_OWNER_PASSWORD="$(get_env PG_OWNER_PASSWORD)"
PG_APP_PASSWORD="$(get_env PG_APP_PASSWORD)"
PG_BACKUP_PASSWORD="$(get_env PG_BACKUP_PASSWORD)"
: "${PG_OWNER_PASSWORD:?PG_OWNER_PASSWORD 未在 .env 中定义}"
: "${PG_APP_PASSWORD:?PG_APP_PASSWORD 未在 .env 中定义}"
: "${PG_BACKUP_PASSWORD:?PG_BACKUP_PASSWORD 未在 .env 中定义}"
enc() { python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }
OWNER_URL="postgresql://quant_owner:$(enc "$PG_OWNER_PASSWORD")@quant-db:5432/quant"
APP_URL="postgresql://quant_app:$(enc "$PG_APP_PASSWORD")@quant-db:5432/quant"

# 3. 构建镜像；首次自建库只启动 DB，不能在 schema/授权就绪前启动 Web
docker compose -f docker-compose.yml -f docker-compose.db.yml build quant-web
docker compose -f docker-compose.yml -f docker-compose.db.yml \
  up -d --wait --wait-timeout 120 quant-db

# 4. 首次自建库部署：初始化 schema（幂等 db:setup，用 owner 连接；DDL 唯一通道）
#    ⚠ 必须等 quant-db healthcheck 通过后执行，否则空库无表、quant_app 无授权，Web 启动即报错
docker compose -f docker-compose.yml -f docker-compose.db.yml exec -T quant-db \
  env PGPASSWORD="$PG_OWNER_PASSWORD" pg_isready -U quant_owner -d quant
docker compose -f docker-compose.yml -f docker-compose.db.yml run --rm \
  -e DATABASE_URL="$OWNER_URL" \
  -e DATABASE_SSL_MODE=disable \
  -e QUANT_SETUP_ROLES=1 \
  -e PG_APP_PASSWORD="$PG_APP_PASSWORD" \
  -e PG_BACKUP_PASSWORD="$PG_BACKUP_PASSWORD" \
  quant-web npm run db:setup
docker compose -f docker-compose.yml -f docker-compose.db.yml run --rm \
  -e DATABASE_URL="$APP_URL" \
  -e DATABASE_SSL_MODE=disable quant-web npm run db:verify

# 5. setup + verify 成功后才启动 Web
docker compose -f docker-compose.yml -f docker-compose.db.yml up -d --no-build quant-web

# 6. 查看日志 / 停止
docker compose -f docker-compose.yml -f docker-compose.db.yml logs -f
docker compose -f docker-compose.yml -f docker-compose.db.yml down
```

外部 PostgreSQL / Supabase 首次部署只使用主 compose，并以其运行账号执行只读验证：

```bash
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml exec quant-web npm run db:verify
```

> **schema 版本注意**：当前主分支为 schema v1（无 quant_app 授权块）。若首次部署到自建库，
> 请使用**切换 commit**（schema v2 含授权块），否则 `db:setup` 不会给 quant_app 授权，应用无法读写。
> 版本升级与切换必须同一维护窗口完成（见 `docs/自建数据库切换方案.md` §6.2）。

> `DB_MODE` 是 **Oracle 部署脚本**（deploy-quant.sh）的解释开关；**通用部署**没有自动脚本，
> 直接用上面的 `-f docker-compose.db.yml` 显式叠加即可，不依赖 DB_MODE。

启动后访问 `http://服务器IP:3111`。容器会自动重启（`restart: unless-stopped`）。

### 持久化数据

`docker-compose.yml` 已用命名卷持久化以下内容，删容器不丢数据：

| 卷 | 内容 | 说明 |
| --- | --- | --- |
| `quant-db-data` | PostgreSQL 业务数据库（`quant` schema，自建 `quant-db`） | **必须保留** |
| `quant-data` | Web 运行时文件和回滚快照 | 建议保留 |
| `quant-signal-data` | Python 信号引擎状态库（`signal_system/state/signal_monitor.db`：候选池/事件/outbox/全市场初始化进度） | 必须保留 |
| `quant-output` | 分析/扫描结果 JSON | 必须保留 |
| `quant-cache` | 行情缓存 | 可清空，会自动重建 |
| `quant-logs` | Python 引擎日志 | 可清空 |

> ⚠️ 信号引擎的 SQLite 数据库必须挂载在独立的 `state/` 目录，**不能**挂到 `signal_system/data/`。`data/` 目录是 Python 源码包（`market_data.py`、`symbols.py`、`providers/` 等）。命名卷只在第一次创建时从镜像拷贝内容，之后**不会被镜像更新覆盖**：如果把卷挂到源码目录，新构建镜像里的 Python 模块会被旧卷内容遮蔽，导致 `No module named 'data.market_data'` 之类的导入失败。

备份时把卷拷走即可，或直接进容器复制：

```bash
# Web 业务数据在 quant-db 卷 / 自建库；/app/data 只保留运行时文件或回滚快照
docker cp quant-web:/app/signal_system/state/signal_monitor.db ./signal-backup.db
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
| `DATABASE_URL` | PostgreSQL 连接串（必填；自建 `quant-db` 或 Supabase pooler 均可） |
| `DATABASE_SSL_MODE` | 显式禁用 TLS：`disable`（自建库无 TLS 时用）；不设置则按原有主机名判断 |
| `DB_MODE` | 部署模式：`legacy`（仅主 compose，默认）/ `selfhost`（叠加 db override，切换自建库后必须） |
| `DATABASE_SSL_REJECT_UNAUTHORIZED` | PostgreSQL TLS 校验，默认 `true` |
| `PG_OWNER_PASSWORD` / `PG_APP_PASSWORD` / `PG_BACKUP_PASSWORD` | 自建 `quant-db` 三角色密码（owner/应用/备份），切换时用（见 `docs/自建数据库切换方案.md`） |
| `DATABASE_POOL_MAX` | PostgreSQL 连接池上限，默认 `5` |
| `PYTHON_BIN` | Python 可执行文件（Linux 填 `python3`，Windows 可省略） |
| `WEB_DATA_DIR` | Web 运行时文件目录，默认 `web/data`；不再存放 Web 业务数据库 |
| `TUSHARE_TOKEN` | Tushare Token |
| `WECHAT_WEBHOOK_URL` | 企业微信机器人 Webhook |
| `SIGNAL_WEBHOOK_URL` / `SIGNAL_WEBHOOK_AUTH` | 通用 Webhook 地址与鉴权 |
| `SIGNAL_EMAIL_SENDER` / `SIGNAL_EMAIL_PASSWORD` / `SIGNAL_EMAIL_RECEIVER` | 邮件通知 |
| `SIGNAL_BARK_DEVICE_KEY` | Bark iOS 推送设备 key（也可在页面配置） |

## 迁移现有数据

旧的“本地 SQLite → Supabase”首次迁移已经完成，该上行流程现已退役。禁止在开发机配置生产
`DATABASE_URL` 后运行 `db:migrate`，也禁止把本地测试/回测产生的 Web、信号状态或输出复制到生产。

`db:migrate` 与 `db:rollback-snapshot` 现只允许 `loopback:5432/quant` 本地库。当前唯一允许的业务数据
方向是生产 → 本地，通过带安全门禁的 `npm run db:sync-from-prod` 覆盖本地数据；生产 schema 变更只走
幂等 `npm run db:setup`，随后执行 `npm run db:verify`。

Web 的 `app.db` 仅是历史迁移源或本地快照，不得再复制到生产容器作为运行时业务库。

### 升级：已有 Docker 部署（旧卷曾挂到 `signal_system/data`）

早于本次修改的部署把 `quant-data` 卷同时挂到 `/app/signal_system/data`。该目录是 Python 源码包，而命名卷只在首次创建时从镜像拷贝内容、之后不会被镜像更新覆盖，因此重建后旧卷内容遮蔽镜像源码，报 `No module named 'data.market_data'`。修复并迁移遗留状态库：

```bash
cd quant/quant-python
git pull
docker compose stop

# 1. 创建新卷 quant-signal-data，并把旧信号状态库从 quant-data 卷拷过来
#    （SQLite WAL 模式：signal_monitor.db / -wal / -shm 一起拷；旧卷里没有该文件也属正常）
docker run --rm -v quant-data:/old -v quant-signal-data:/new alpine \
  sh -c "cp -a /old/signal_monitor.db* /new/ 2>/dev/null || true; ls -la /new"

# 2. 用新配置重建镜像并启动
docker compose up -d --build
```

> 注意：旧 `quant-data` 卷里混合了 Web 数据库（`app.db` 等）与信号库，升级命令只拷贝 `signal_monitor.db*`，切勿用整卷数据覆盖新卷。

裸机部署则直接把文件放到 `WEB_DATA_DIR`（默认 `web/data`）和 `signal_system/output/` 对应位置即可。

## 常见问题

- **服务器只有 `python3` 没有 `python`**：已支持，设置 `PYTHON_BIN=python3`，Docker 镜像已内置。
- **使用 akshare / tushare 行情源**：默认 `provider: auto` 不需要它们；如需切换，在 Dockerfile 中追加安装 `signal_system/requirements-akshare.txt` 或 `requirements-tushare.txt` 后重新构建。
- **访问控制**：当前页面还没有登录密码，公网部署前务必用 Nginx Basic Auth、防火墙或后续加入的登录功能保护。
- **端口**：默认 3111，改端口请同步修改 `docker-compose.yml` 的 `ports` 或 `npm start` 参数。
