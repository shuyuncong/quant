#!/bin/bash
# ============================================================
# quant 手动重建容器脚本（Oracle Cloud）——「正确的重建姿势」
#
# 为什么需要这个脚本：
#   2026-10-14 两天内发生两次整站 500，根因都不在策略代码，而在「手动重建容器」这个动作：
#     第一次：重建了新镜像，但 db:setup 因缺 PG_OWNER_PASSWORD 被跳过
#             → 库 v4 / 代码 v5 → instrumentation 校验失败 → 整站 500；
#     第二次：重建时漏了 -f docker-compose.oracle.db.yml
#             → 丢了 internal 网络与 DATABASE_URL 覆盖 → 连不上 quant-db → 整站 500。
#   deploy-quant.sh 的加固只覆盖自动部署路径；本脚本把手动路径也固化下来。
#
# 本脚本做的事（缺一不可）：
#   1. 强制带 -f docker-compose.oracle.yml -f docker-compose.oracle.db.yml
#   2. **重建之前**先跑 db:setup（幂等），而不是事后补
#   3. 重建后自动巡检：config_files 标签 / internal 网络 / 存活探针 / 业务 API
#   4. 任一项不过就报明确原因，不留"看起来起来了"的假象
#
# 用法：
#   /home/ubuntu/ops/recreate-quant.sh              # 重建全部服务
#   /home/ubuntu/ops/recreate-quant.sh quant-web    # 只重建 quant-web
#   /home/ubuntu/ops/recreate-quant.sh --check      # 只巡检，不动容器
# ============================================================
set -euo pipefail

REPO=/opt/docker/quant/quant-python
COMPOSE_DIR="$REPO/quant-python"
ENV_FILE="$COMPOSE_DIR/.env"
COMPOSE_FILES=(-f docker-compose.oracle.yml -f docker-compose.oracle.db.yml)
# 日志目录：默认 /home/ubuntu/ops（部署形态）；不可写时退回 /tmp。
# 此前硬编码 /home/ubuntu/ops，导致"直接从仓库跑"会在第一条 log() 上
# 因 tee 无法创建文件而 exit（set -e + pipefail）—— 2026-10-16 实测踩到。
LOG_DIR="${QUANT_OPS_LOG_DIR:-/home/ubuntu/ops}"
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="/tmp"
LOG="$LOG_DIR/recreate-quant.log"

# 巡检用的探针。必须是**实际存在**的路径：
#   /favicon.ico -> 200（src/app/favicon.ico）
#   /            -> 307（page.tsx 写死 redirect("/results")）
#   /api/analyses-> 200（需要库；这也顺带验证 DB 连通）
# 注意：/logo.svg 与 /login 在本项目里**不存在**，正常态就是 404，绝不能当探针
# （2026-10-14 排查时曾误用，浪费一轮）。
PROBES=(
    "/favicon.ico 200"
    "/ 307"
    "/api/analyses 200"
    "/api/jobs 200"
)

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
die() { log "ERROR: $*"; exit 1; }

# 从 .env 安全提取单个键（dotenv 重复键以后者为准；不 source 整文件）
get_env() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\([^#[:space:]]*\).*/\1/p" "$ENV_FILE" | tail -n1
}

# ------------------------------------------------------------
# 巡检：检查正在运行的容器是否处于"正确配置 + 服务可用"状态
# ------------------------------------------------------------
check() {
    local failed=0

    echo "--- 1. compose 文件（必须含两个 oracle 文件）---"
    for svc in quant-web quant-backtest; do
        local files
        files="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$svc" 2>/dev/null || true)"
        if [ -z "$files" ]; then
            echo "  $svc: 容器不存在"; failed=1; continue
        fi
        case "$files" in
            *docker-compose.oracle.yml*docker-compose.oracle.db.yml*)
                echo "  $svc: OK" ;;
            *)
                echo "  $svc: 配置错误 —— config_files=$files"
                echo "        期望同时含 docker-compose.oracle.yml 与 docker-compose.oracle.db.yml"
                failed=1 ;;
        esac
    done

    echo "--- 2. 网络（quant-web 必须同时接入 default 与 internal）---"
    local nets
    nets="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' quant-web 2>/dev/null || true)"
    echo "  quant-web: $nets"
    if printf '%s' "$nets" | grep -q internal; then
        echo "  internal: OK"
    else
        echo "  缺少 internal 网络 —— quant-web 将无法解析 quant-db，全部查询会失败"; failed=1
    fi

    echo "--- 3. 存活与业务探针 ---"
    for probe in "${PROBES[@]}"; do
        local path expected actual
        path="${probe%% *}"; expected="${probe##* }"
        actual="$(curl -sS --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:3111${path}" 2>/dev/null || echo 000)"
        if [ "$actual" = "$expected" ]; then
            printf '  %-16s %s (OK)\n' "$path" "$actual"
        else
            printf '  %-16s %s (期望 %s)\n' "$path" "$actual" "$expected"; failed=1
        fi
    done

    echo "--- 4. 容器状态 ---"
    docker ps --filter name=quant --format '  {{.Names}}\t{{.Status}}'

    if [ "$failed" -ne 0 ]; then
        log "巡检未通过，见上方明细"
        return 1
    fi
    log "巡检全部通过"
    return 0
}

# ------------------------------------------------------------
# 参数解析
# ------------------------------------------------------------
CHECK_ONLY=0
SERVICES=()
for arg in "$@"; do
    case "$arg" in
        --check|-c) CHECK_ONLY=1 ;;
        -*) die "未知参数：$arg（用法：$0 [--check] [服务名...]）" ;;
        *) SERVICES+=("$arg") ;;
    esac
done

if [ "$CHECK_ONLY" = "1" ]; then
    check
    exit $?
fi

# ------------------------------------------------------------
# 前置校验：全部在**动容器之前**完成
# ------------------------------------------------------------
[ -d "$COMPOSE_DIR" ] || die "compose 目录不存在：$COMPOSE_DIR"
[ -f "$ENV_FILE" ]    || die ".env 不存在：$ENV_FILE（重建需要 DB_MODE 与密码）"
cd "$COMPOSE_DIR"

DB_MODE="$(get_env DB_MODE)"
log "=== 手动重建开始（DB_MODE=${DB_MODE:-未设置}）==="

if [ "$DB_MODE" != "selfhost" ]; then
    die "DB_MODE='${DB_MODE:-未设置}'，本脚本只用于 selfhost 模式。
     若确要用 legacy（Supabase），请走 deploy-quant.sh，并注意主 compose 不带 quant-db。"
fi

DATABASE_URL_VALUE="$(get_env DATABASE_URL)"
if ! printf '%s\n' "$DATABASE_URL_VALUE" | grep -Eq '^postgres(ql)?://[^[:space:]]+@quant-db:5432/quant$'; then
    die "DB_MODE=selfhost 但 DATABASE_URL 未指向 quant-db:5432/quant。
     缺 docker-compose.oracle.db.yml 的覆盖时，应用会连错库并整站 500。"
fi

OWNER_PASSWORD="$(get_env PG_OWNER_PASSWORD)"
[ -n "$OWNER_PASSWORD" ] || die ".env 缺少 PG_OWNER_PASSWORD，无法执行 db:setup。
     缺此变量时迁移会被跳过 → 库版本落后 → instrumentation 校验失败 → 整站 500（2026-10-14 事故）。
     请先补齐该变量；确需跳过时手动执行 docker compose，风险自负。"

[ -n "$(get_env PG_APP_PASSWORD)" ] || die ".env 缺少 PG_APP_PASSWORD（应用以 quant_app 连接）"

# ------------------------------------------------------------
# 第 1 步：先迁移（幂等），再重建
# 顺序是关键 —— 先让库到达目标版本，新代码起来时校验必然通过。
# ------------------------------------------------------------
log "第 1 步：应用数据库 schema 迁移（db:setup，幂等）"
docker compose "${COMPOSE_FILES[@]}" exec -T \
    -e "DATABASE_URL=postgresql://quant_owner:${OWNER_PASSWORD}@quant-db:5432/quant" \
    -e DATABASE_SSL_MODE=disable \
    quant-web npm run db:setup 2>&1 | tee -a "$LOG"

# ------------------------------------------------------------
# 第 2 步：重建容器
# ------------------------------------------------------------
if [ "${#SERVICES[@]}" -eq 0 ]; then
    SERVICES=(quant-web quant-backtest)
fi
log "第 2 步：重建容器：${SERVICES[*]}"
docker compose "${COMPOSE_FILES[@]}" up -d --build "${SERVICES[@]}" 2>&1 | tee -a "$LOG"

# ------------------------------------------------------------
# 第 3 步：等健康检查通过再巡检
# ------------------------------------------------------------
log "第 3 步：等待服务就绪"
for _ in $(seq 1 30); do
    if curl -sS --noproxy '*' -o /dev/null --max-time 3 http://127.0.0.1:3111/favicon.ico 2>/dev/null; then break; fi
    sleep 2
done
sleep 3

# ------------------------------------------------------------
# 第 4 步：巡检
# ------------------------------------------------------------
log "第 4 步：巡检"
if check; then
    log "=== 手动重建完成，服务正常 ==="
else
    log "=== 重建后巡检未通过，请查看 ${LOG} 与 docker logs quant-web ==="
    die "重建完成但服务异常"
fi
