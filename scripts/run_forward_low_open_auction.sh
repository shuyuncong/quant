#!/usr/bin/env bash
# 首板低开 9:25 判信号（给 cron 用）。
#
# 取账本观察名单里每只股票的竞价开盘价 → 开盘 ÷ 昨收落在低开区间的就是当天的信号
# → 连同账本会买的股数推送到手机（每个交易日一条，没有信号也推；休市日不推）
# → 盯信号股到 9:35，记下 9:30 后实际的盘口和成交均价。
# 记录写在 state/forward_low_open_1m/auction/<日期>.json，不改账本。
# 一天只判一次：当天的记录已存在就报错。名单不是给今天用的（前一晚账本没推进）、
# 取不到行情、推送没发出去，都以非 0 退出并报警（见 ledger_cron.sh）。
# 晚上 run_forward_low_open.sh 推进账本之后会拿这份记录和账本对账。
# 限时 15 分钟：正常 9:35 结束，没有信号时几秒钟。
#
# crontab（服务器时区 Asia/Shanghai）：
#   25 9 * * 1-5 /home/ubuntu/development/quant/scripts/run_forward_low_open_auction.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/ledger_cron.sh"

run_ledger "首板低开 9:25 判信号失败" \
  "$SIGNAL_SYSTEM/state/forward_low_open_1m/auction" \
  "$SIGNAL_SYSTEM/logs/forward_low_open_auction.log" \
  timeout --verbose 15m "$PYTHON" -W ignore forward_low_open_auction.py --action check
