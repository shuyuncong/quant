#!/usr/bin/env bash
# ETF 纸面账本每日推进（run_forward_paper.ps1 的 Linux 版，给 cron 用）。
#
# 同步行情 → 逐个交易日补成交/盯市 → 周最后交易日生成下周计划。
# 幂等：重复运行不会重复成交或重复生成计划；漏跑的交易日下次自动补上，
# 每个交易日仍按当日开盘价成交。
#
# crontab（服务器时区 Asia/Shanghai）：
#   30 16 * * 1-5 /home/ubuntu/development/quant/scripts/run_forward_paper.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIGNAL_SYSTEM="$ROOT/quant-python/signal_system"
STATE_DIR="$SIGNAL_SYSTEM/state/forward_paper_500k"
LOG="$SIGNAL_SYSTEM/logs/forward_paper.log"

mkdir -p "$(dirname "$LOG")"
exec 9>"$STATE_DIR/.lock"
if ! flock -n 9; then
  echo "$(date -Is) SKIP 已有一次运行未结束" >>"$LOG"
  exit 0
fi

cd "$SIGNAL_SYSTEM"
status=0
output="$("$ROOT/quant-python/.venv/bin/python" forward_paper.py --action cycle --sync --calendar 2>&1)" || status=$?
{
  echo "$(date -Is) exit=$status"
  echo "$output"
} >>"$LOG"
exit "$status"
