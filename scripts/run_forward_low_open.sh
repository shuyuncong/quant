#!/usr/bin/env bash
# 首板低开纸面账本每日推进（给 cron 用）。
#
# 读生产行情缓存（每个交易日 18:00 开始刷新，约 20:20 刷完）→ 逐个交易日补
# 信号/成交/盯市 → 算下一个交易日的观察名单 → 补股票名称和 15 分钟线卖点诊断
# → 把名单和账本写成一页看板：https://quant.illsky.com/research/forward-low-open/
# 幂等：账本只从 last_session 之后推进；行情没刷到最新交易日或没刷完就报错
# 不动账本，漏跑的交易日下次自动补上（记为 backfilled）。失败时推送报警
# （见 ledger_cron.sh）。
# 限时 20 分钟（正常约 1 分钟）：名称和分钟线来自 BaoStock，它的 socket 没有超时，
# 服务端不回包会一直卡住、占着锁，之后每天的运行都会被跳过。账本和看板在取名称
# 之前已经写好，超时只丢当晚的诊断，下次运行自动补。
#
# crontab（服务器时区 Asia/Shanghai）：
#   30 21 * * 1-5 /home/ubuntu/development/quant/scripts/run_forward_low_open.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/ledger_cron.sh"

run_ledger "首板低开纸面账本推进失败" \
  "$SIGNAL_SYSTEM/state/forward_low_open_1m" \
  "$SIGNAL_SYSTEM/logs/forward_low_open.log" \
  timeout --verbose 20m "$PYTHON" -W ignore forward_low_open.py --action cycle \
  --data-dir "${LOW_OPEN_DATA_DIR:-/opt/docker/quant/cache/daily_history}" \
  --page "${RESEARCH_WEB_ROOT:-/var/www/html/quant/research}/forward-low-open/index.html"
