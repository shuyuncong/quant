#!/usr/bin/env bash
# 家庭服务器（取数发布方）→ Oracle（工作机/接收方）同步研究缓存。
#
# 设计约束：
# 1) 单写者：只有发布方跑回填；接收方不写同一目录。
# 2) 不用 cache_delta：其增量 schema 只含 8 个标准列，会丢掉 turnover_rate
#    （cache_delta.py STANDARD_COLUMNS）。研究缓存必须保留换手率。
# 3) rsync 整文件传输：日线文件被重写时只能整只传，但研究批次不是每日增量，
#    一次全量约 300–500 MB 可接受。
# 4) 先写临时名再 rename 由 rsync 自身保证（-a 默认写临时文件），无需额外处理。
#
# 用法（家庭服务器上）：
#   DEST=agent@oracle:/home/agent/quant/quant-python/signal_system \
#     bash docs/ops/sync-research-cache.sh
#
# 可选环境变量：
#   SRC   发布方 signal_system 根目录（默认 /home/agent/quant/quant-python/signal_system）
#   LOCK  互斥锁文件（默认 /tmp/quant-research-sync.lock）
#   DRY_RUN=1  只打印将传输的内容（rsync -n），首次接入必须先跑这个

set -euo pipefail

SRC="${SRC:-/home/agent/quant/quant-python/signal_system}"
DEST="${DEST:?需要 DEST，例如 agent@oracle:/home/agent/quant/quant-python/signal_system}"
LOCK="${LOCK:-/tmp/quant-research-sync.lock}"

CACHE_REL="cache/baostock_history_20261026"
REPORT_REL="output/backtests/_history_expansion_20261026"

if [[ ! -d "$SRC/$CACHE_REL" ]]; then
  echo "ERROR: 找不到发布方缓存 $SRC/$CACHE_REL" >&2
  exit 2
fi

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "ERROR: 已有同步在运行（锁 $LOCK）" >&2
  exit 3
fi

RSYNC_OPTS=(-a --partial --info=progress2)
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  RSYNC_OPTS+=(-n)
  echo "DRY_RUN：不写入目标"
fi

rsync "${RSYNC_OPTS[@]}" \
  --exclude 'speedtest_*' \
  "$SRC/$CACHE_REL/" "$DEST/$CACHE_REL/"

rsync "${RSYNC_OPTS[@]}" \
  "$SRC/$REPORT_REL/" "$DEST/$REPORT_REL/"

count="$(find "$SRC/$CACHE_REL" -maxdepth 1 -name '*_qfq.pkl' | wc -l)"
echo "OK $(date -Is) qfq_files=$count src=$SRC dest=$DEST"
