#!/usr/bin/env bash
# ETF 纸面账本每日推进（run_forward_paper.ps1 的 Linux 版，给 cron 用）。
#
# 同步行情 → 逐个交易日补成交/盯市 → 周最后交易日生成下周计划。
# 幂等：重复运行不会重复成交或重复生成计划；漏跑的交易日下次自动补上，
# 每个交易日仍按当日开盘价成交。失败时推送报警（见 ledger_cron.sh）。
#
# crontab（服务器时区 Asia/Shanghai）：
#   30 16 * * 1-5 /home/ubuntu/development/quant/scripts/run_forward_paper.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/ledger_cron.sh"

run_ledger "ETF 纸面账本推进失败" \
  "$SIGNAL_SYSTEM/state/forward_paper_500k" \
  "$SIGNAL_SYSTEM/logs/forward_paper.log" \
  "$PYTHON" forward_paper.py --action cycle --sync --calendar
