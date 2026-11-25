#!/usr/bin/env bash
# 把 docs/backtest-results 发布到 https://quant.illsky.com/research/。
#
# 那个地址由 nginx 直接读静态目录，不随应用部署更新；新增或改动回测结果后，
# 先重建回测中心，再运行本脚本：
#
#   quant-python/.venv/bin/python scripts/build_backtest_hub.py
#   scripts/publish_research.sh
#
# 线上目录 = docs/backtest-results 的镜像，外加两处：
# - 根页面 index.html 是回测总览（overview.html）加一行 <base>，这样打开
#   /research/ 直接就是总览，页面里的相对链接仍指向回测中心目录；
# - 只存在于线上的内容（_preview/、reference-*.html，以及 run_forward_low_open.sh
#   每晚写的 forward-low-open/ 看板）原样保留。
# 只新增和覆盖，不删除：仓库里删掉的结果目录需要手工从线上目录移除。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$ROOT/docs/backtest-results"
DEST="${RESEARCH_WEB_ROOT:-/var/www/html/quant/research}"
HUB="2026-11-15-backtest-hub"

if [ ! -d "$DEST" ]; then
  echo "发布目录不存在: $DEST" >&2
  exit 1
fi

rsync -rt --exclude='/index.html' "$SOURCE/" "$DEST/"

staged="$(mktemp "$DEST/.index.XXXXXX")"
trap 'rm -f "$staged"' EXIT
sed "0,/<head>/s||<head>\n<base href=\"/research/$HUB/\">|" "$SOURCE/$HUB/overview.html" >"$staged"
if ! grep -q "<base href=\"/research/$HUB/\">" "$staged"; then
  echo "总览页里没有 <head>，根页面没有更新" >&2
  exit 1
fi
chmod 664 "$staged"
mv "$staged" "$DEST/index.html"
trap - EXIT
echo "已发布到 $DEST"
