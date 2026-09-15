#!/bin/bash
# ============================================================
# quant 服务器自动部署脚本（Oracle Cloud）
# 触发方式：
#   1. GitHub Actions 推送后通过 SSH 执行（推荐）
#   2. 手动执行: /home/ubuntu/ops/deploy-quant.sh
#   3. cron 轮询: 见文档"自动部署"章节
# 说明：
#   - 构建失败时旧容器不受影响（docker compose up 构建失败即中止）
#   - 部署暂停（DEPLOY_PAUSED=1，.env 中设置）：切换 commit 期间由操作员手动执行
#     迁移/切换，自动部署本轮直接退出，避免 v2 代码先于迁移连上 v1 Supabase。
#   - 数据库部署模式（DB_MODE，.env 中设置）：
#       legacy   只加载主 compose（Supabase 时代/未切换时）
#       selfhost 叠加 docker-compose.oracle.db.yml（切换到自建库后必须永久使用此模式，
#                否则 quant-web 会丢失 internal 网络与 DATABASE_SSL_MODE=disable）
#   - DB_MODE/DEPLOY_PAUSED 均用 sed 从 .env 提取（不 source 整个 dotenv——
#     Compose dotenv 非 shell 语法，整文件 source 可能被特殊字符/命令替换利用）。
#   - 白名单校验：非法 DB_MODE 直接中止部署，绝不静默退回 legacy。
# ============================================================
set -euo pipefail

LOG=/home/ubuntu/ops/deploy-quant.log
REPO=/opt/docker/quant/quant-python
COMPOSE_DIR="$REPO/quant-python"
ENV_FILE="$COMPOSE_DIR/.env"
MAINTENANCE_LOCK=/home/ubuntu/quant-maintenance.lock

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

# 从 .env 安全提取单个键的最终值（dotenv 重复键以后者为准；不展开、不执行整文件）
get_env() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\([^#[:space:]]*\).*/\1/p" "$ENV_FILE" | tail -n1
}

# 自动部署、备份、迁移和恢复共用同一把锁。维护窗口持锁时，新部署直接跳过；
# 部署一旦开始则全程持锁，维护流程只有等待它结束后才能继续。
exec 8>"$MAINTENANCE_LOCK"
flock -n 8 || { log "维护锁已被占用，跳过本轮自动部署"; exit 0; }

# 部署暂停：切换/迁移窗口内跳过自动部署（防 v2 代码先连 v1 库）
if [ "$(get_env DEPLOY_PAUSED)" = "1" ]; then
    log "DEPLOY_PAUSED=1，跳过本轮自动部署（操作员正手动执行切换/迁移）"
    exit 0
fi

log "=== 部署开始 ==="

# 即使没有新 commit，也先校验并记录当前数据库模式，供切换后人工确认。
DB_MODE="$(get_env DB_MODE)"
if [ -z "$DB_MODE" ]; then
    DB_MODE="legacy"
    log "WARN: .env 中未设置 DB_MODE，按 legacy 部署；若已切换到自建库请在 .env 设置 DB_MODE=selfhost"
fi

case "$DB_MODE" in
    selfhost|legacy) log "DB_MODE=$DB_MODE" ;;
    *)
        log "ERROR: 非法 DB_MODE='$DB_MODE'，中止部署（合法值: selfhost|legacy）"
        exit 1
        ;;
esac

# 模式与连接串必须双向一致。尤其禁止显式 legacy + quant-db：主 compose 不带 internal 网络，
# 这种组合会把正在工作的 selfhost Web 重建成无法解析 quant-db 的容器。
DATABASE_URL_VALUE="$(get_env DATABASE_URL)"
if [ "$DB_MODE" = "selfhost" ]; then
    if ! printf '%s\n' "$DATABASE_URL_VALUE" | grep -Eq '^postgres(ql)?://[^[:space:]]+@quant-db:5432/quant$'; then
        log "ERROR: DB_MODE=selfhost 但 DATABASE_URL 未精确指向 quant-db:5432/quant，中止部署"
        exit 1
    fi
elif printf '%s\n' "$DATABASE_URL_VALUE" | grep -Eq '@quant-db(:[0-9]+)?(/|$)'; then
    log "ERROR: DB_MODE=legacy 但 DATABASE_URL 指向 quant-db，中止部署。请改为 DB_MODE=selfhost"
    exit 1
fi

cd "$REPO"
git fetch origin master 2>&1 | tee -a "$LOG"

# 无新提交时直接跳过（cron 轮询模式每 5 分钟跑一次，避免空构建）
if git diff --quiet HEAD origin/master; then
    log "无新提交，跳过部署"
    exit 0
fi

git pull --ff-only origin master 2>&1 | tee -a "$LOG"

cd "$COMPOSE_DIR"
if [ "$DB_MODE" = "selfhost" ]; then
    log "使用自建库模式（叠加 docker-compose.oracle.db.yml）"
    docker compose -f docker-compose.oracle.yml -f docker-compose.oracle.db.yml up -d --build 2>&1 | tee -a "$LOG"
    # 数据库 schema 迁移（幂等）：quant_app 只有 DML 权限，ALTER/CREATE 必须用
    # quant_owner 连接执行 db:setup。schema.sql 全部幂等，每次部署跑一次安全；
    # 缺此步时新代码要求的 schema_meta 版本不匹配，所有 /api/* 直接 500。
    log "应用数据库 schema 迁移（db:setup，幂等）"
    OWNER_PASSWORD="$(get_env PG_OWNER_PASSWORD)"
    if [ -z "$OWNER_PASSWORD" ]; then
        log "ERROR: 未找到 PG_OWNER_PASSWORD，跳过迁移（后续页面会 500，请手动执行 db:setup）"
    else
        docker exec \
            -e "DATABASE_URL=postgresql://quant_owner:${OWNER_PASSWORD}@quant-db:5432/quant" \
            -e DATABASE_SSL_MODE=disable \
            quant-web npm run db:setup 2>&1 | tee -a "$LOG"
        # 迁移晚于容器启动时（新代码先起、后升 schema），instrumentation 启动即失败、
        # 进程挂起不健康但端口仍在服务、全部 500。迁移成功后重启让进程重新加载。
        log "重启 quant-web 以加载新 schema"
        docker restart quant-web 2>&1 | tee -a "$LOG"
    fi
else
    log "使用主 compose 模式（legacy）"
    docker compose -f docker-compose.oracle.yml up -d --build 2>&1 | tee -a "$LOG"
fi

# 清理历史构建产生的悬空镜像，避免磁盘膨胀（保留正在使用的）
docker image prune -f >/dev/null 2>&1 || true

log "=== 部署完成 ==="
