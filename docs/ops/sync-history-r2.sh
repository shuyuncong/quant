#!/bin/bash
# ============================================================
# 历史行情缓存 <-> Cloudflare R2 同步（让换电脑也能拿到数据）
#
# 为什么需要它：
#   `cache/` 与 `output/` 都被 .gitignore 排除（实测 git ls-files = 0），
#   所以 `git clone` 到新电脑**一个字节行情都没有**；而本会话实测重跑回填要
#   1318 只 / 74.3 分钟。故把"值得留的"那份归档到对象存储，跨机可下载。
#
# 只归档**值得留**的部分（这是本脚本存在的主要理由）：
#   ✅ cache/daily_history/   553M  10016 文件  重跑需 ~74 分钟
#   ✅ cache/index_*.pkl      134K             重跑需切片 + 重登记哈希，很贵
#   ❌ cache/<64位sha256>.pkl 318M  10733 文件  24h TTL 的接口缓存，自动重取
#   ❌ output/backtests/      1.6G             回测可重跑（--with-backtests 才收）
#   默认只归档前两项 ≈ 554M（打包后实测 ≈ 172M），而不是全量 2.6G。
#
# 为什么是 tar 分片而不是逐文件 sync：
#   10016 个小文件逐个 PUT 在 R2 上是 10016 次 Class A 操作且首轮很慢；
#   实测打成 3 个 tar.gz 后总计 172M（压缩到 31%）、本地耗时 42s。
#   分片还能让新电脑"只下需要的分片"（每个 shard 附带 .list 说明含哪些文件）。
#
# 用法（打包机，通常是数据最全的那台）：
#   ./sync-history-r2.sh --pack                 # 只打包 + 生成 MANIFEST
#   ./sync-history-r2.sh --upload               # 打包并上传 R2
#   ./sync-history-r2.sh --check                # 校验本地包与 MANIFEST 是否一致
#
# 用法（新电脑）：
#   ./sync-history-r2.sh --list                 # 看远端有哪些包
#   ./sync-history-r2.sh --fetch /path/to/cache # 下载 + 校验 + 解包到目标 cache
#   ./sync-history-r2.sh --fetch-only           # 只下载校验，不解包
#
# 环境变量：
#   CACHE_DIR   源/目标 cache 目录。默认自动探测（见 detect_cache_dir）
#   R2_REMOTE   目标路径，默认沿用仓库既有备份的远端
#   R2_CONFIG   rclone 配置路径，默认 /home/ubuntu/.config/rclone/rclone.conf
#   RCLONE      rclone 可执行文件，默认取 PATH 上的 rclone
# ============================================================
set -euo pipefail

MODE=""
FETCH_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --pack|--upload|--check|--list|--fetch-only) MODE="${1#--}"; shift ;;
        --fetch) MODE="fetch"; FETCH_DIR="${2:-}"; [ -n "$FETCH_DIR" ] || { echo "ERROR: --fetch 需要目标目录" >&2; exit 2; }; shift 2 ;;
        --with-backtests) WITH_BACKTESTS=1; shift ;;
        -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
        *) echo "ERROR: 未知参数 $1" >&2; exit 2 ;;
    esac
done
WITH_BACKTESTS="${WITH_BACKTESTS:-0}"

# 既有备份的远端与配置路径（docs/ops/backup-quant.sh 已在用，说明这组值是通的）。
R2_REMOTE="${R2_REMOTE:-r2-oracle-backup:oracle-backup/quant-cache/v1}"
R2_CONFIG="${R2_CONFIG:-/home/ubuntu/.config/rclone/rclone.conf}"
RCLONE="${RCLONE:-rclone}"

# 日志目录：默认 /home/ubuntu/ops；不可写则退回 /tmp（与 recreate-quant.sh 同策略）。
LOG_DIR="${QUANT_OPS_LOG_DIR:-/home/ubuntu/ops}"
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="/tmp"
LOG="$LOG_DIR/sync-history-r2.log"
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
die() { log "ERROR: $*"; exit 1; }

PACK_DIR="${PACK_DIR:-$HOME/quant-history-pack}"
# 分片按"压缩后不超过 200MB"控制：rclone 的 S3 后端在 200MiB 以下走单次 PUT，
# 超过则转 multipart。刻意压在阈值下，避免 R2 上出现未完成的 multipart 残片。
SHARD_TARGET_MB="${SHARD_TARGET_MB:-200}"

# ------------------------------------------------------------
# 探测 cache 目录：本脚本要在"开发机"和"服务器"两种布局下都能跑
# ------------------------------------------------------------
detect_cache_dir() {
    local candidates=(
        "/opt/docker/quant/cache"                                   # Oracle 宿主（compose bind mount）
        "$(dirname "$0")/../../quant-python/signal_system/cache"    # 仓库内开发布局
    )
    for c in "${candidates[@]}"; do
        [ -d "$c/daily_history" ] && { (cd "$c" && pwd); return 0; }
    done
    return 1
}

if [ -n "${CACHE_DIR:-}" ]; then
    CACHE_DIR="$(cd "$CACHE_DIR" && pwd)" || die "CACHE_DIR 不存在: $CACHE_DIR"
else
    CACHE_DIR="$(detect_cache_dir)" || die "找不到含 daily_history 的 cache 目录，请用 CACHE_DIR=... 指定"
fi
SRC_ROOT="$(dirname "$CACHE_DIR")"   # tar 的 -C 基准，保证包内路径是 signal_system/cache/...
BASE_NAME="$(basename "$CACHE_DIR")"
REL_PREFIX="$(basename "$SRC_ROOT")/$BASE_NAME"   # 例如 cache/daily_history/...

need_rclone() { command -v "$RCLONE" >/dev/null 2>&1 || [ -x "$RCLONE" ] || die "找不到 rclone（可用 RCLONE=/path/to/rclone 指定）"; }

rclone_run() { "$RCLONE" --config "$R2_CONFIG" "$@"; }

# ------------------------------------------------------------
# 打包：按累计原始大小分组 -> 每组一个 tar.gz -> MANIFEST.json
# ------------------------------------------------------------
do_pack() {
    log "=== 打包开始 [$CACHE_DIR] ==="
    command -v tar >/dev/null 2>&1 || die "找不到 tar"

    rm -rf "$PACK_DIR"; mkdir -p "$PACK_DIR"

    # 收集目标（相对 SRC_ROOT 的路径），顺序稳定以保证可复现。
    # `tr -d '\r'` 是必要的：Git for Windows 的 find/read 会带 CR，
    # 不删就变成 "xxx.pkl\r"，tar 报 Cannot stat（2026-10-16 实测踩到）。
    local list_file="$PACK_DIR/_targets.txt"
    : > "$list_file"
    find "$CACHE_DIR/daily_history" -maxdepth 1 -name '*.pkl' -type f | sort | while IFS= read -r f; do
        printf '%s\n' "${f#"$SRC_ROOT"/}"
    done | tr -d '\r' >> "$list_file"
    find "$CACHE_DIR" -maxdepth 1 -name 'index_*.pkl' -type f | sort | while IFS= read -r f; do
        printf '%s\n' "${f#"$SRC_ROOT"/}"
    done | tr -d '\r' >> "$list_file"
    if [ "$WITH_BACKTESTS" = "1" ] && [ -d "$SRC_ROOT/output/backtests" ]; then
        log "含 output/backtests（体积大、可重跑，确认你确实要）"
        find "$SRC_ROOT/output/backtests" -type f | sort | while IFS= read -r f; do
            printf '%s\n' "${f#"$SRC_ROOT"/}"
        done | tr -d '\r' >> "$list_file"
    fi

    local total_files
    total_files="$(wc -l < "$list_file" | tr -d ' ')"
    [ "$total_files" -gt 0 ] || die "没有可打包的文件（$CACHE_DIR 是空的？）"
    log "待打包 $total_files 个文件"

    # 分组：累计原始大小超过阈值就切下一组
    python3 - "$SRC_ROOT" "$list_file" "$PACK_DIR" "$SHARD_TARGET_MB" <<'PYEOF'
import pathlib, sys
src, lst, pack, limit_mb = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), float(sys.argv[4])
limit = limit_mb * 1024 * 1024
groups, cur, cursz = [], [], 0
for line in lst.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    sz = (src / line).stat().st_size
    if cur and cursz + sz > limit:
        groups.append(cur); cur, cursz = [], 0
    cur.append(line); cursz += sz
if cur:
    groups.append(cur)

# ⚠ 两个都必须显式 newline="\n"：
#   ① Windows 上 Path.write_text 默认把 \n 翻成 \r\n，会让 tar 读到 "xxx.pkl\r"
#      -> Cannot stat（2026-10-16 实测）；
#   ② 结尾必须补 \n，否则 bash 的 `wc -l` 少算 1、且 `while read` 会**整组丢掉**
#      最后一行 -> 分片少一个而 MANIFEST 仍报全量（静默丢数据的形状）。
with open(pack / "_groups.txt", "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join("|".join(g) for g in groups) + "\n")
with open(pack / "_groupcount.txt", "w", encoding="utf-8", newline="\n") as f:
    f.write(f"{len(groups)}\n")
print(f"分成 {len(groups)} 组")
PYEOF

    local groups_total
    groups_total="$(tr -d '\r' < "$PACK_DIR/_groupcount.txt")"
    [ -n "$groups_total" ] || die "分组数读取失败"

    local idx=0
    # `|| [ -n "$group" ]`：文件末尾若无换行，read 返回非 0 但仍赋值，
    # 只写 `while read` 会漏掉最后一行（见上面 ② 的说明）。
    while IFS= read -r group || [ -n "$group" ]; do
        group="${group%$'\r'}"   # 防御：Windows 上若写出 CRLF，这里兜住
        [ -n "$group" ] || continue
        idx=$((idx + 1))
        local padded
        printf -v padded '%03d' "$idx" 2>/dev/null || padded="$idx"
        local shard="signal_system-cache.${padded}-of-$(printf '%03d' "$groups_total").tar.gz"
        printf '%s\n' "$group" | tr '|' '\n' > "$PACK_DIR/${shard%.tar.gz}.list"
        tar -czf "$PACK_DIR/$shard" -C "$SRC_ROOT" -T "$PACK_DIR/${shard%.tar.gz}.list"
        log "  $shard  $(du -h "$PACK_DIR/$shard" | cut -f1)"
    done < "$PACK_DIR/_groups.txt"

    [ "$idx" -eq "$groups_total" ] || die "只写出 $idx 个分片，应为 $groups_total 个（拒绝产出残缺包）"

    # 覆盖性断言 + MANIFEST：把每个分片的真实成员摊开，与目标清单逐一对齐。
    # 不做这一步的话，"分片少一个"这类错误对外只表现为"少了一些股票"，
    # 极难发现 —— 所以宁可在这里 fail。
    python3 - "$PACK_DIR" "$list_file" "$SRC_ROOT" <<'PYEOF'
import hashlib, json, pathlib, sys, tarfile
pack, lst, src = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])

targets = {l.strip() for l in lst.read_text(encoding="utf-8").splitlines() if l.strip()}
parts, seen = [], {}
for p in sorted(pack.glob("*.tar.gz")):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    with tarfile.open(p) as tf:
        members = [m.name.lstrip("./") for m in tf.getmembers() if m.isfile()]
    for m in members:
        seen.setdefault(m, []).append(p.name)
    parts.append({"name": p.name, "bytes": p.stat().st_size, "sha256": h.hexdigest(),
                  "members": len(members)})

missing = sorted(targets - seen.keys())
extra = sorted(seen.keys() - targets)
dupes = {k: v for k, v in seen.items() if len(v) > 1}
print(f"目标 {len(targets)} 个文件；分片实际含 {len(seen)} 个")
if missing:
    print(f"❌ 缺失 {len(missing)} 个，例：{missing[:3]}")
if extra:
    print(f"❌ 多余 {len(extra)} 个，例：{extra[:3]}")
if dupes:
    print(f"❌ 重复 {len(dupes)} 个，例：{list(dupes)[:3]}")
if missing or extra or dupes:
    sys.exit(1)

with open(pack / "MANIFEST.json", "w", encoding="utf-8", newline="\n") as f:
    json.dump({"format": "quant-cache-archive/v1",
               "files": len(targets),
               "chunks": len(parts),
               "parts": parts}, f, ensure_ascii=False, indent=2)
print(f"MANIFEST.json: {len(parts)} 分片 / {len(targets)} 文件（覆盖性已校验）")
PYEOF

    rm -f "$PACK_DIR/_groups.txt" "$PACK_DIR/_targets.txt" "$PACK_DIR/_groupcount.txt"
    log "打包完成：$PACK_DIR"
}

# ------------------------------------------------------------
# 校验：逐个分片重算 sha256 与 MANIFEST 比对
# ------------------------------------------------------------
do_check() {
    [ -f "$PACK_DIR/MANIFEST.json" ] || die "找不到 $PACK_DIR/MANIFEST.json，先跑 --pack"
    python3 - "$PACK_DIR" <<'PYEOF'
import hashlib, json, pathlib, sys
pack = pathlib.Path(sys.argv[1])
man = json.loads((pack / "MANIFEST.json").read_text(encoding="utf-8"))
bad = 0
for part in man["parts"]:
    p = pack / part["name"]
    if not p.exists():
        print(f"缺失 {part['name']}"); bad += 1; continue
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    if h.hexdigest() != part["sha256"]:
        print(f"校验失败 {part['name']}"); bad += 1
    else:
        print(f"OK  {part['name']}  {part['bytes']/1024/1024:.1f} MB")
print(f"\n{len(man['parts'])-bad}/{len(man['parts'])} 通过")
sys.exit(1 if bad else 0)
PYEOF
}

do_list() {
    need_rclone
    rclone_run ls -l "$R2_REMOTE" | tee -a "$LOG"
}

do_upload() {
    need_rclone
    do_pack
    do_check
    log "上传 $R2_REMOTE ..."
    # 与 backup-quant.sh 同形：失败即报错、绝不当成功。本地包保留不删。
    rclone_run copy "$PACK_DIR" "$R2_REMOTE" --transfers 4 --checkers 8 || die "rclone 上传失败，本地包保留在 $PACK_DIR"
    log "上传完成"
    rclone_run ls -l "$R2_REMOTE" | tee -a "$LOG"
}

do_fetch() {
    need_rclone
    local dest="$FETCH_DIR"
    [ -n "$dest" ] || die "--fetch 需要目标目录"
    local dl="$PACK_DIR"
    rm -rf "$dl"; mkdir -p "$dl"

    # 先取 MANIFEST：没它就无从校验，绝不先下几百 MB
    rclone_run copy "$R2_REMOTE/MANIFEST.json" "$dl" || die "拉取 MANIFEST.json 失败（远端路径对吗？用 --list 看）"
    python3 - "$dl" <<'PYEOF'
import json, pathlib, sys
man = json.loads((pathlib.Path(sys.argv[1]) / "MANIFEST.json").read_text(encoding="utf-8"))
print(f"远端包: {man['chunks']} 分片 / {man['files']} 文件")
for p in man["parts"]:
    print(f"  {p['name']}  {p['bytes']/1024/1024:.1f} MB")
PYEOF

    # `tr -d '\r'`：Windows 上 Python 的 **stdout** 同样是文本模式、行尾是 \r\n，
    # 不删就会得到 "xxx.tar.gz\r"，tar 报 Cannot open（2026-10-16 实测踩到）。
    local names
    mapfile -t names < <(python3 -c "import json,sys;print('\n'.join(p['name'] for p in json.load(open(sys.argv[1]))['parts']))" "$dl/MANIFEST.json" | tr -d '\r')
    [ "${#names[@]}" -gt 0 ] || die "MANIFEST.json 里没有分片记录"
    rclone_run copy "$R2_REMOTE" "$dl" --include "*.tar.gz" --transfers 4 || die "下载分片失败"

    PACK_DIR="$dl" do_check || die "校验失败：分片损坏，重下"

    [ "$MODE" = "fetch-only" ] && { log "仅下载校验完成：$dl"; return 0; }

    # 包内路径形如 `cache/daily_history/xxx.pkl`、`cache/index_000001_sh.pkl`
    # （打包时 tar -C 到 signal_system/），所以 dest 应为**包含 cache 的那一层**，
    # 即 signal_system/ 目录本身 —— 与 config 里的 `cache_dir: ./cache` 对得上。
    mkdir -p "$dest"
    log "解包到 $dest（将生成 $dest/cache/...）"
    for n in "${names[@]}"; do
        tar -xzf "$dl/$n" -C "$dest"
        log "  已解 $n"
    done

    # 收尾断言：解出来的文件数必须等于 MANIFEST 声明的数。
    # 少了就说明"分片不全"或"解包被中断"，绝不能当成成功。
    python3 - "$dl" "$dest" <<'PYEOF'
import json, pathlib, sys
man = json.loads((pathlib.Path(sys.argv[1]) / "MANIFEST.json").read_text(encoding="utf-8"))
dest = pathlib.Path(sys.argv[2])
got = sum(1 for p in (dest / "cache").rglob("*.pkl") if p.is_file())
want = man["files"]
print(f"解出 {got} 个 pkl，MANIFEST 声明 {want} 个")
if got != want:
    print(f"❌ 数量不符，差 {want-got}（分片不全或解包中断）")
    sys.exit(1)
print("✅ 覆盖性一致")
PYEOF
    log "完成：$dest/cache"
}

case "$MODE" in
    pack)       do_pack ;;
    check)      do_check ;;
    list)       do_list ;;
    upload)     do_upload ;;
    fetch|fetch-only) do_fetch ;;
    *)          die "请指定 --pack / --upload / --check / --list / --fetch <dir>（-h 看用法）" ;;
esac
