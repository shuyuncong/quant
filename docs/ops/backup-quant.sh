#!/bin/bash
# 每日备份：quant-db 逻辑备份 + quant-web 应用目录 + rclone 上传 R2
# 用法：先 chmod +x；cron 每日 03:00。
# 依赖：.env 中的 PG_BACKUP_PASSWORD / DEPLOY_PAUSED（本脚本自行加载，不依赖调用者环境）
set -euo pipefail
umask 077

COMPOSE_DIR="/opt/docker/quant/quant-python/quant-python"
RCLONE_REMOTE="r2-oracle-backup:oracle-backup"
# 独立备份目录：⚠ 勿与旧通用备份脚本共用 /home/ubuntu/backups（旧脚本会清空该目录、
# 锁不共用，可能互删）。本脚本专用 /home/ubuntu/quant-backups。
BACKUP_ROOT="$HOME/quant-backups"
LOG_DIR="$HOME/logs"
LOG="$LOG_DIR/quant-backup.log"
LOCK_FILE="$HOME/quant-backup.lock"
MAINTENANCE_LOCK="$HOME/quant-maintenance.lock"
RETENTION=14d
COMPOSE="-f $COMPOSE_DIR/docker-compose.oracle.yml -f $COMPOSE_DIR/docker-compose.oracle.db.yml"

# cron 环境 PATH 极简，先补齐
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# 日志目录必须可写（cron 下 /var/log 通常 root 独占；用 $HOME/logs）
mkdir -p "$LOG_DIR" "$BACKUP_ROOT"
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

# 从 .env 安全提取所需键：⚠ 勿 source 整文件（dotenv 非 shell 语法，特殊字符可能被
# 解析甚至执行）；仅用 sed 提取目标键的值
get_env() {
  [ -f "$COMPOSE_DIR/.env" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\([^#[:space:]]*\).*/\1/p" "$COMPOSE_DIR/.env" | tail -n1
}
PG_BACKUP_PASSWORD="$(get_env PG_BACKUP_PASSWORD)"

# 校验必填变量（set -u 下未定义即退，这里给出明确报错）
: "${PG_BACKUP_PASSWORD:?PG_BACKUP_PASSWORD 未在 .env 中定义}"

# 与迁移/恢复共用维护锁，消除“检查暂停后维护才开始”的竞态。
exec 8>"$MAINTENANCE_LOCK"
flock -n 8 || { log "迁移、恢复或其他备份正在运行，本轮备份跳过"; exit 0; }

# 拿到锁后再次读取暂停值；维护窗口内 cron 不得触碰容器状态。
if [ "$(get_env DEPLOY_PAUSED)" = "1" ]; then
  log "DEPLOY_PAUSED=1，处于维护窗口，本轮备份跳过"
  exit 0
fi

# 互斥锁：防止前一次备份未结束又起新任务
exec 9>"$LOCK_FILE"
flock -n 9 || { log "已有备份在运行，退出"; exit 1; }

DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_ROOT"
log "=== 备份开始 [$DATE] ==="

# 1) 先停 quant-web：冻结业务写入（调度器、Web 写库随停），
#    使随后的 DB dump 与目录打包处于同一业务时间点
WEB_WAS_RUNNING=0
if [ "$(docker inspect --format '{{.State.Running}}' quant-web 2>/dev/null || true)" = "true" ]; then
  WEB_WAS_RUNNING=1
  log "停止 quant-web ..."
  docker stop quant-web
else
  log "quant-web 原本未运行，保持停止状态"
fi
restore_web_state() {
  if [ "$WEB_WAS_RUNNING" = "1" ]; then
    docker start quant-web
  fi
}
trap 'restore_web_state || log "ERROR: quant-web 自动恢复失败，需立即人工处理"' EXIT

# 2) 数据库逻辑备份（写宿主机 stdout，不停 quant-db）
log "pg_dump quant-db ..."
docker compose $COMPOSE exec -T quant-db \
  env PGPASSWORD="${PG_BACKUP_PASSWORD}" \
  pg_dump -U quant_backup -h 127.0.0.1 -d quant --format=custom --no-owner --no-acl \
  > "$BACKUP_ROOT/quant-db_$DATE.dump"
# 校验 dump 真实可恢复（pg_restore --list 能列出归档内容才算有效，ls -lh 不能证明）
docker compose $COMPOSE exec -T quant-db pg_restore --list \
  < "$BACKUP_ROOT/quant-db_$DATE.dump" >/dev/null

# 3) 应用目录物理备份（quant-web 已停，三个目录一次打包）
log "打包 quant-web 数据目录 ..."
tar -czf "$BACKUP_ROOT/quant-web_$DATE.tar.gz" \
  -C /opt/docker/quant data state output

if ! restore_web_state; then
  log "ERROR: quant-web 恢复失败，不上传或清理本地备份"
  exit 1
fi
trap - EXIT
if [ "$WEB_WAS_RUNNING" = "1" ]; then
  log "quant-web 已恢复运行，目录打包完成"
else
  log "quant-web 保持备份前的停止状态，目录打包完成"
fi

# 3) 上传 R2；失败不清本地
log "上传 R2 ..."
if ! rclone copy "$BACKUP_ROOT" "$RCLONE_REMOTE/quant/$DATE" \
     --config /home/ubuntu/.config/rclone/rclone.conf; then
  log "ERROR: rclone 上传失败，保留本地备份，待人工处理"
  exit 1
fi

# 4) 清理：上传成功才清本地 + 清 R2 旧数据
rm -rf "$BACKUP_ROOT"/*
rclone delete "$RCLONE_REMOTE/quant" --min-age "$RETENTION" --rmdirs \
  --config /home/ubuntu/.config/rclone/rclone.conf || log "warn: R2 清理失败（不影响本次备份）"

log "=== 备份完成 [$DATE] ==="
