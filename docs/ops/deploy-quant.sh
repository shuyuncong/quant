#!/bin/bash
# ============================================================
# quant 服务器自动部署脚本（Oracle Cloud）
# 触发方式：
#   1. GitHub Actions 推送后通过 SSH 执行（推荐）
#   2. 手动执行: /home/ubuntu/ops/deploy-quant.sh
#   3. cron 轮询: 见文档"自动部署"章节
# 说明：构建失败时旧容器不受影响（docker compose up 构建失败即中止）
# ============================================================
set -euo pipefail

LOG=/home/ubuntu/ops/deploy-quant.log
REPO=/opt/docker/quant/quant-python

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

log "=== 部署开始 ==="

cd "$REPO"
git fetch origin master 2>&1 | tee -a "$LOG"

# 无新提交时直接跳过（cron 轮询模式每 5 分钟跑一次，避免空构建）
if git diff --quiet HEAD origin/master; then
    log "无新提交，跳过部署"
    exit 0
fi

git pull --ff-only origin master 2>&1 | tee -a "$LOG"

cd "$REPO/quant-python"
docker compose -f docker-compose.oracle.yml up -d --build 2>&1 | tee -a "$LOG"

# 清理历史构建产生的悬空镜像，避免磁盘膨胀（保留正在使用的）
docker image prune -f >/dev/null 2>&1 || true

log "=== 部署完成 ==="
