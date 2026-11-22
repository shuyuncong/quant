#!/usr/bin/env bash
# 首板低开纸面账本每日推进（给 cron 用）。
#
# 读生产行情缓存（每个交易日 18:00 开始刷新，约 20:20 刷完）→ 逐个交易日补
# 信号/成交/盯市 → 补股票名称和 15 分钟线卖点诊断。
# 幂等：账本只从 last_session 之后推进；行情没刷到最新交易日或没刷完就报错
# 不动账本，漏跑的交易日下次自动补上（记为 backfilled）。
#
# crontab（服务器时区 Asia/Shanghai）：
#   30 21 * * 1-5 /home/ubuntu/development/quant/scripts/run_forward_low_open.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIGNAL_SYSTEM="$ROOT/quant-python/signal_system"
STATE_DIR="$SIGNAL_SYSTEM/state/forward_low_open_1m"
DATA_DIR="${LOW_OPEN_DATA_DIR:-/opt/docker/quant/cache/daily_history}"
LOG="$SIGNAL_SYSTEM/logs/forward_low_open.log"

mkdir -p "$(dirname "$LOG")" "$STATE_DIR"
exec 9>"$STATE_DIR/.lock"
if ! flock -n 9; then
  echo "$(date -Is) SKIP 已有一次运行未结束" >>"$LOG"
  exit 0
fi

cd "$SIGNAL_SYSTEM"
status=0
output="$("$ROOT/quant-python/.venv/bin/python" -W ignore forward_low_open.py --action cycle --data-dir "$DATA_DIR" 2>&1)" || status=$?
{
  echo "$(date -Is) exit=$status"
  echo "$output"
} >>"$LOG"
exit "$status"
