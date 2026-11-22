#!/usr/bin/env bash
# 纸面账本 cron 入口的公共部分：加锁、运行、写日志、失败报警。
# 由 run_forward_paper.sh / run_forward_low_open.sh source，不单独运行。
#
#   run_ledger <报警标题> <状态目录> <日志文件> <命令...>
#
# 命令在 signal_system 目录下运行。退出码非 0 时，把输出末尾推送到 Web 设置页里
# 已启用的通知渠道（quant-python/signal_system/ledger_alert.py）。报警发不出去
# 只记日志，不改变退出码。
#
# 管不到的情况：cron 本身没在跑（机器关机、crontab 被清空）时不会有任何报警。

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIGNAL_SYSTEM="$ROOT/quant-python/signal_system"
PYTHON="$ROOT/quant-python/.venv/bin/python"

run_ledger() {
  local title="$1" state_dir="$2" log="$3"
  shift 3

  mkdir -p "$(dirname "$log")" "$state_dir"
  exec 9>"$state_dir/.lock"
  if ! flock -n 9; then
    echo "$(date -Is) SKIP 已有一次运行未结束" >>"$log"
    return 0
  fi

  cd "$SIGNAL_SYSTEM"
  local status=0 output
  output="$("$@" 2>&1)" || status=$?
  {
    echo "$(date -Is) exit=$status"
    echo "$output"
  } >>"$log"
  if [ "$status" -ne 0 ]; then
    {
      echo "$(date -Is) alert"
      printf '%s\n' "$output" | tail -c 1200 | "$PYTHON" ledger_alert.py --title "$title" 2>&1 || true
    } >>"$log"
  fi
  return "$status"
}
