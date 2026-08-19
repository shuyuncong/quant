# Oracle Cloud 部署指南（quant.illsky.com）

> 与现有服务（CLIProxyAPI 等）保持一致的部署方式：Docker Compose + Nginx 反向代理 + Certbot HTTPS + rclone 备份。通用部署说明见 [DEPLOY.md](DEPLOY.md)。

## 目录结构

```text
/opt/docker/quant/
├── data/                # Web 数据库（app.db）——必须保留
├── output/              # 分析/扫描结果 JSON——必须保留
├── cache/               # 行情缓存（可清空，自动重建）
├── logs/                # Python 引擎日志
└── quant-python/        # 代码仓库（git clone）
    └── quant-python/    # 仓库根目录内的项目子目录（Dockerfile 位于此处）
        ├── Dockerfile
        ├── .env                       # 环境变量（可选，也可在网页里配置）
        ├── docker-compose.yml         # 仓库自带的通用版 Compose
        ├── docker-compose.oracle.yml  # 服务器专用 Compose（见第 3 节）
        ├── web/
        └── signal_system/
```

## 0. 准备

需已安装 Docker 与 Docker Compose v2（Ubuntu 未安装时）：

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu   # 重新登录后生效
```

确保域名 `quant.illsky.com` 的 DNS A 记录已指向服务器公网 IP。

## 3. 创建目录并配置 Docker Compose

```bash
sudo mkdir -p /opt/docker/quant
cd /opt/docker/quant
sudo chown -R ubuntu:ubuntu /opt/docker/quant

# 克隆代码（仓库根目录将位于 /opt/docker/quant/quant-python）
# 注意：仓库根目录内还有 quant-python/ 项目子目录，Dockerfile 位于
# /opt/docker/quant/quant-python/quant-python/Dockerfile
git clone https://github.com/shuyuncong/quant.git quant-python

# 创建数据目录（对应容器内 /app/data、/app/signal_system/output 等）
mkdir -p data output cache logs

# 环境变量（可选，也可以全部在网页里配置；.env 必须与 compose 文件同目录）
cp quant-python/quant-python/web/.env.example quant-python/quant-python/.env
nano quant-python/quant-python/.env
```

服务器专用 Compose 已随仓库提供：`quant-python/quant-python/docker-compose.oracle.yml`（与 Dockerfile 同目录，内容如下）：

```yaml
services:
  quant-web:
    build:
      context: .
      dockerfile: Dockerfile
    image: quant-web:latest
    container_name: quant-web
    restart: unless-stopped
    ports:
      # 只绑定本机回环，由 Nginx 对外提供 80/443，不直接暴露公网
      - "127.0.0.1:3111:3111"
    environment:
      TZ: Asia/Shanghai
      # 镜像内 Python 依赖安装在 /opt/venv，勿改为 python3
      PYTHON_BIN: /opt/venv/bin/python
      WEB_DATA_DIR: /app/data
      TUSHARE_TOKEN: "${TUSHARE_TOKEN:-}"
      WECHAT_WEBHOOK_URL: "${WECHAT_WEBHOOK_URL:-}"
      SIGNAL_WEBHOOK_URL: "${SIGNAL_WEBHOOK_URL:-}"
      SIGNAL_WEBHOOK_AUTH: "${SIGNAL_WEBHOOK_AUTH:-}"
      SIGNAL_EMAIL_SENDER: "${SIGNAL_EMAIL_SENDER:-}"
      SIGNAL_EMAIL_PASSWORD: "${SIGNAL_EMAIL_PASSWORD:-}"
      SIGNAL_EMAIL_RECEIVER: "${SIGNAL_EMAIL_RECEIVER:-}"
      SIGNAL_BARK_DEVICE_KEY: "${SIGNAL_BARK_DEVICE_KEY:-}"
    volumes:
      # Web 控制台数据库（模型/策略/推送/定时/股票池等全部配置）
      - /opt/docker/quant/data:/app/data
      # Python 信号库（候选池、事件去重、outbox、全市场初始化进度）
      - /opt/docker/quant/data:/app/signal_system/data
      # 分析/扫描结果 JSON
      - /opt/docker/quant/output:/app/signal_system/output
      # 行情缓存与日志（可清空，丢失后会自动重建）
      - /opt/docker/quant/cache:/app/signal_system/cache
      - /opt/docker/quant/logs:/app/signal_system/logs
```

> 说明：仓库自带的 `quant-python/quant-python/docker-compose.yml` 面向通用部署（绑定 `0.0.0.0` + 命名卷）；`docker-compose.oracle.yml` 是服务器专用版，仅监听 `127.0.0.1`，并把数据放到宿主机目录，方便 Nginx 转发与备份脚本打包。
> **compose 文件必须与 Dockerfile 同目录**：Docker Compose ≥ 2.35 默认用 bake 构建，Dockerfile 与 context 按 compose 文件所在目录解析（而不是按 build context）；把 compose 放在 `/opt/docker/quant` 下引用子目录里的 Dockerfile 会报 `failed to read dockerfile: open Dockerfile: no such file or directory` 或 `resolve: lstat ...: no such file or directory`。
> 若服务器上残留旧容器（报 `container name already in use`），先 `docker rm -f quant-web` 再启动。

构建并启动（**必须在项目子目录内执行**，首次构建需要几分钟）：

```bash
cd /opt/docker/quant/quant-python/quant-python
docker compose -f docker-compose.oracle.yml up -d --build
docker compose -f docker-compose.oracle.yml logs -f quant-web
```

本服务为本地构建镜像，无需 `docker compose pull`。验证应用是否起来：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3111   # 应返回 200
```

## 4. 配置 Nginx 反向代理

安装 Nginx：

```bash
sudo apt update && sudo apt install -y nginx
```

创建配置文件 `/etc/nginx/sites-available/quant`：

```bash
sudo nano /etc/nginx/sites-available/quant
```

写入以下配置：

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name quant.illsky.com;

    location / {
        proxy_pass http://127.0.0.1:3111;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        client_max_body_size 500M;
        # 增加超时设置，防止长时间分析/扫描时连接断开
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;

        proxy_buffering off;
    }
}
```

启用并重启 Nginx：

```bash
sudo ln -s /etc/nginx/sites-available/quant /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx
```

## 5. 使用 Certbot 获取 HTTPS 证书

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d quant.illsky.com
```

- 按提示输入邮箱（用于接收更新提醒）、同意服务条款。
- Certbot 会自动验证域名所有权，并修改 Nginx 配置以强制 HTTPS。

## 6. 开放端口（Oracle Cloud）

- OCI 控制台 → 网络 → 虚拟云网络（VCN）→ 安全列表：添加入站规则 TCP 80、443（源 `0.0.0.0/0`）。
- 实例防火墙（若启用 ufw）：`sudo ufw allow 80,443/tcp`。
- 应用端口 3111 只绑定 `127.0.0.1`，不需要对外开放。

## 7. 测试与验证

- **访问地址**：`https://quant.illsky.com`
- **API 测试**：`curl -s https://quant.illsky.com/ | head`
- **容器状态**：`docker compose ps`
- **查看日志**：`docker compose logs -f quant-web`

## 8. 备份脚本（与现有备份保持一致）

在现有备份脚本的 `TARGETS` 中追加以下两项（`quant-web` 备份前会自动停止，打包后自动恢复；停服后打包可避免 SQLite WAL 数据丢失）：

```bash
TARGETS=(
    "quant-web|/opt/docker/quant/data"
    "quant-web|/opt/docker/quant/output"
    # 其他服务按同样格式追加，例如：
    # "memos|/opt/memos"
)
```

`cache/` 与 `logs/` 无需备份，丢失后会自动重建。完整脚本示例（与 CLIProxyAPI 同一套，rclone 上传到 Cloudflare R2）：

```bash
#!/bin/bash

# ================= 配置区域 =================

# Rclone 配置名称和存储桶
RCLONE_REMOTE="r2-oracle-backup:oracle-backup"

# 本地临时备份目录
BACKUP_ROOT="/home/ubuntu/backups"

# 日期后缀
DATE=$(date +%Y%m%d_%H%M%S)

# -------------------------------------------
# [关键] 备份清单配置
# 格式: "容器名称|数据目录路径"
# 如果不需要停止容器(如静态网页)，容器名称填 "none"
# -------------------------------------------
TARGETS=(
    "quant-web|/opt/docker/quant/data"
    "quant-web|/opt/docker/quant/output"
    # 其他服务按同样格式追加，例如：
    # "memos|/opt/memos"
)

# ================= 逻辑区域 =================

# 创建临时备份根目录
mkdir -p "$BACKUP_ROOT"

echo "========== 开始备份任务 [$DATE] =========="

# 遍历备份清单
for target in "${TARGETS[@]}"; do
    # 解析配置，以 | 分割
    CONTAINER_NAME="${target%%|*}"
    SOURCE_DIR="${target##*|}"

    # 提取目录名作为文件名的一部分 (例如 /opt/docker/quant/data -> data)
    DIR_NAME=$(basename "$SOURCE_DIR")
    ARCHIVE_NAME="${DIR_NAME}_${DATE}.tar.gz"

    echo ">>> 处理目标: $DIR_NAME (容器: $CONTAINER_NAME)"

    # 1. 检查目录是否存在
    if [ ! -d "$SOURCE_DIR" ]; then
        echo "   [警告] 目录 $SOURCE_DIR 不存在，跳过！"
        continue
    fi

    # 2. 停止容器 (如果指定了容器名且不是 none)
    if [ "$CONTAINER_NAME" != "none" ]; then
        echo "   停止容器 $CONTAINER_NAME ..."
        docker stop "$CONTAINER_NAME"
    fi

    # 3. 打包数据
    echo "   打包数据中..."
    # 使用 tar -czf 打包，排除可能产生的 socket 文件等
    tar -czf "$BACKUP_ROOT/$ARCHIVE_NAME" -C "$(dirname "$SOURCE_DIR")" "$DIR_NAME"

    # 4. 恢复容器
    if [ "$CONTAINER_NAME" != "none" ]; then
        echo "   启动容器 $CONTAINER_NAME ..."
        docker start "$CONTAINER_NAME"
    fi

    echo "   $DIR_NAME 打包完成."
done

echo "----------------------------------------"

# 5. 统一上传到 Cloudflare R2
echo ">>> 开始上传到 Cloudflare R2 ..."
# copy 命令只会上传新文件
rclone copy "$BACKUP_ROOT" "$RCLONE_REMOTE/$DATE" --config /home/ubuntu/.config/rclone/rclone.conf

# 6. 清理工作
# 删除本地备份文件（节省服务器空间）
echo ">>> 清理本地临时文件..."
rm -rf "$BACKUP_ROOT"/*

# (可选) 清理 R2 上的旧数据，保留最近 30 天
# echo ">>> 清理 R2 旧备份..."
# rclone delete "$RCLONE_REMOTE" --min-age 14d --rmdirs

echo "========== 备份任务完成 =========="
```

> 提示：同一容器有多个备份目录时，脚本会对每个目录分别停止/启动一次容器，属正常现象。

建议加定时任务（每天凌晨 3 点）：

```bash
crontab -e
0 3 * * * /home/ubuntu/backup.sh >> /home/ubuntu/backup.log 2>&1
```

## 更新代码与迁移数据

更新到最新代码：

```bash
cd /opt/docker/quant/quant-python/quant-python
git -C /opt/docker/quant/quant-python pull
docker compose -f docker-compose.oracle.yml up -d --build
```

首次上线前迁移本机已有数据（SQLite WAL 三个文件一起拷）：

```bash
# 在本机执行
scp quant-python/web/data/app.db* ubuntu@服务器IP:/opt/docker/quant/data/
scp -r quant-python/signal_system/output/* ubuntu@服务器IP:/opt/docker/quant/output/
```

## 注意事项

- **页面暂无登录密码**：公网使用前建议在 Nginx 加 Basic Auth（`sudo apt install -y apache2-utils && sudo htpasswd -c /etc/nginx/.htpasswd admin`，并在 server 块加 `auth_basic "Restricted";` 与 `auth_basic_user_file /etc/nginx/.htpasswd;`）或限制来源 IP。
- **行情源**：默认 `provider: auto`（腾讯/新浪/东财），无需额外数据源；如需 akshare/tushare，见 [DEPLOY.md](DEPLOY.md)。
- **端口**：容器内固定 3111；改端口需同步修改 Compose 的 `ports` 与 Nginx 的 `proxy_pass`。
- **时区**：Compose 已设置 `TZ=Asia/Shanghai`，定时任务按北京时间执行。
