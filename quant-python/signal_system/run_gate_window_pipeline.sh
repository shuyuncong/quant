#!/bin/bash
# 2018H2 闸门对照流水线：等回填 → 建样本 → 跑对照。
#
# 为什么要串起来：三步各自 1-2 小时，中间不能断。回填是 nohup 脱离的进程，
# 本脚本用日志里的"完成："标记判断它结束；若回填卡死（进度文件长时间不更新）
# 则主动放弃，避免无限等待。
set -u
cd /d/development/github/quant/quant-python/signal_system || exit 1

LOG=/tmp/backfill2016.log
PROG=cache/daily_history/_backfill_20161201_20230519_progress.json
SAMPLE=output/backtests/_gate_samples/sample-2018-07-01-2018-12-31
OUT="D:/tmp/gate_2018h2.json"

echo "=== 第 1 步：等待个股回填完成 ==="
last_change=$(date +%s)
last_count=-1
while true; do
  if grep -q "完成：" "$LOG" 2>/dev/null; then
    echo "回填已结束：$(grep '完成：' "$LOG" | tail -1)"
    break
  fi
  if [ -f "$PROG" ]; then
    count=$(python -c "import json,sys;print(len(json.load(open(sys.argv[1],encoding='utf-8'))['done']))" "$PROG" 2>/dev/null || echo 0)
    now=$(date +%s)
    if [ "$count" != "$last_count" ]; then
      last_count=$count; last_change=$now
      echo "  进度 $count/1318  ($(date +%H:%M:%S))"
    elif [ $(( now - last_change )) -gt 1800 ]; then
      echo "!! 进度文件 30 分钟未更新（停在 $count），判定回填已卡死，放弃"
      exit 1
    fi
  fi
  sleep 60
done

echo
echo "=== 第 2 步：构建 2018H2 研究样本（6 workers，可续跑）==="
rm -f "$SAMPLE/prepared_metadata.json"
python research_sample_builder.py 2018-07-01 2018-12-31 2>&1 | tail -20
if [ ! -f "$SAMPLE/prepared_metadata.json" ]; then
  echo "!! 样本未生成，停止"; exit 1
fi

echo
echo "=== 第 3 步：跑三策略 × 有/无闸门 对照（live 口径，与生产一致）==="
python market_gate_comparison_study.py \
  --sample "$SAMPLE" \
  --start 2018-07-01 --end 2018-12-31 \
  --out "$OUT" --label 2018H2 --semantics live 2>&1 | tail -12

echo
echo "=== 第 4 步：汇总 ==="
python - <<'PY'
import json
from pathlib import Path
p = Path(r"D:/tmp/gate_2018h2.json")
if not p.exists():
    print("!! 无输出文件"); raise SystemExit(1)
d = json.loads(p.read_text(encoding="utf-8")); c = d.get("cases") or {}
print(f"窗口 {d.get('window')}  闸门开放 {d.get('gate_open_days')}/{d.get('gate_total_days')}")
print(f"{'策略':<20}{'闸门':<6}{'收益':>10}{'回撤':>10}{'笔数':>7}")
for s in ("macd_zero_axis", "yearline_pullback", "macd_divergence"):
    for arm in ("gate", "nogate"):
        v = c.get(f"{s}|{arm}", {})
        print(f"{s:<20}{arm:<6}{v.get('return_pct', float('nan')):>10.2f}"
              f"{v.get('max_drawdown_pct', float('nan')):>10.2f}{v.get('trades', 0):>7}")
print()
for s in ("macd_zero_axis", "yearline_pullback", "macd_divergence"):
    g = c.get(f"{s}|gate", {}).get("return_pct"); n = c.get(f"{s}|nogate", {}).get("return_pct")
    if g is not None and n is not None:
        print(f"Δ({s}) = {g - n:+.2f}pp")
PY
