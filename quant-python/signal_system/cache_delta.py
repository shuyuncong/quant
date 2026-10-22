"""按日增量同步 cache/daily_history：

为什么需要它：日线缓存的更新形态是「追加 1 行、整个 .pkl 重写」，所以过一个交易日
1 万多个文件**全部变字节**，`rclone sync` 无法只传差异，只能重传约 173 MB（压缩后）。
而一个交易日的真实增量是 10,016 行 ≈ 195 KB（gzip），比值约 900:1。

本脚本只处理「本地目录 → 本地目录」的增量，**不负责传输**：
导出目录交给 rclone / U 盘 / 任意通道搬运，命令本身跨 Windows/macOS/Linux。

四个子命令：

  baseline  把当前 cache 记为「已发布基线」，只写状态、不产出任何数据。
            一次性操作：你已经有基线包（sync-history-r2.sh 的分片）时用它起头。

  export    对比 cache 与状态，产出：
              delta.csv.gz     仅新增的 bar（按 symbol 分组）
              full/<sym>.pkl   被复权重述或首次出现的标的（整只替换）
              manifest.json    sha256 + 计数 + 本次状态
            尾部追加 → 走 delta；历史行被改写（除权）→ 走 full。

  apply     把 export 的产物应用到目标 cache。幂等：
            已存在且值相同的行跳过，已存在但值不同的行**报错**（那是未声明的重述）。

  verify    只校验 manifest 里声明的 sha256，不碰 cache。

安全约束（都是踩过的坑）：
  * `pd.concat` / `reset_index` 会丢掉 DataFrame.attrs，而下游审计用
    `frame.attrs["adjust"] == "qfq"` 判口径。应用增量后必须显式还原，否则整批缓存
    会被判为「非 qfq」而拒绝。
  * 目标机没有该标的文件时**绝不**只写当天这一行：1 行历史会被当成有效缓存，
    必须报错并要求先落地基线包。
  * delta 与 full 按复权口径分目录；`none` 与 `qfq` 混用不会报错但会让回测算错。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import pickle
import shutil
import sys
import tempfile
from pathlib import Path

BASE = Path(os.path.abspath(__file__)).parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402

STANDARD_COLUMNS = ["datetime", "open", "high", "low", "close", "volume", "amount", "is_closed"]
PRICE_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]
DEFAULT_CACHE = BASE / "cache" / "daily_history"


# ---------------------------------------------------------------- helpers

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_frame(path: Path) -> pd.DataFrame:
    with path.open("rb") as handle:
        frame = pickle.load(handle)
    if "datetime" not in frame.columns:
        raise ValueError(f"{path.name}: 缺少 datetime 列")
    dates = pd.to_datetime(frame["datetime"])
    if not dates.is_monotonic_increasing:
        raise ValueError(f"{path.name}: datetime 不是递增的，拒绝按「尾部追加」处理")
    if not dates.is_unique:
        raise ValueError(f"{path.name}: datetime 有重复")
    return frame


def write_frame(path: Path, frame: pd.DataFrame) -> None:
    """原子写入，保留 attrs —— save_daily_history 只做 pickle.dump，不碰 attrs。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=str(path.parent), delete=False, suffix=".tmp")
    try:
        with os.fdopen(handle.fileno(), "wb") as fh:
            pickle.dump(frame, fh)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def prefix_digest(frame: pd.DataFrame, count: int) -> str:
    """前 count 行的内容摘要，用于判断「历史是否被改写」。

    只存 64 字符摘要而不是整段数值：一只股票可能有 2000+ 行，全存下来状态文件会到
    几百 MB。摘要按固定 6 位小数归一，保证同一份历史每次得到相同结果。
    """
    head = frame.iloc[:count]
    dates = pd.to_datetime(head["datetime"]).dt.strftime("%Y-%m-%d").tolist()
    columns = head[PRICE_COLUMNS].astype(float).round(6)
    lines = [
        f"{day}|" + "|".join(f"{value:.6f}" for value in values)
        for day, values in zip(dates, columns.itertuples(index=False, name=None))
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def expected_attrs(variant: str) -> dict:
    return {
        "timeframe": "1d",
        "adjust": variant,
        "timezone": "Asia/Shanghai",
        "timestamp_semantics": "end",
    }


def state_path(state_dir: Path, variant: str) -> Path:
    return state_dir / f"{variant}.json"


def load_state(state_dir: Path, variant: str) -> dict:
    path = state_path(state_dir, variant)
    if not path.exists():
        return {"variant": variant, "symbols": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state_dir: Path, variant: str, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_path(state_dir, variant)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def cache_files(cache_dir: Path, variant: str) -> list[Path]:
    return sorted(cache_dir.glob(f"*_{variant}.pkl"))


def symbol_of(path: Path, variant: str) -> str:
    return path.name[: -len(f"_{variant}.pkl")]


# ---------------------------------------------------------------- baseline

def cmd_baseline(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache)
    files = cache_files(cache_dir, args.variant)
    if not files:
        print(f"ERROR: {cache_dir} 下没有 *_{args.variant}.pkl", file=sys.stderr)
        return 2

    state = {"variant": args.variant, "symbols": {}}
    for index, path in enumerate(files, 1):
        frame = read_frame(path)
        rows = int(len(frame))
        state["symbols"][symbol_of(path, args.variant)] = {
            "sha256": sha256_file(path),
            "rows": rows,
            "last": pd.to_datetime(frame["datetime"]).iloc[-1].strftime("%Y-%m-%d"),
            "prefix": prefix_digest(frame, rows),
        }
        if index % 2000 == 0:
            print(f"  ... {index}/{len(files)}", flush=True)

    save_state(Path(args.state), args.variant, state)
    print(f"基线已记录：{len(state['symbols'])} 只 {args.variant} → {state_path(Path(args.state), args.variant)}")
    print("注意：只写状态，没有产出数据。基线包需另行提供（例如 sync-history-r2.sh 的分片）。")
    return 0


# ---------------------------------------------------------------- export

def cmd_export(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache)
    state_dir = Path(args.state)
    variant = args.variant
    files = cache_files(cache_dir, variant)
    if not files:
        print(f"ERROR: {cache_dir} 下没有 *_{variant}.pkl", file=sys.stderr)
        return 2

    state = load_state(state_dir, variant)
    known: dict = state.get("symbols", {})

    out_root = Path(args.out)
    date_tag = args.date
    out_dir = out_root / variant / date_tag
    full_dir = out_dir / "full"
    if out_dir.exists():
        if not args.force:
            print(f"ERROR: {out_dir} 已存在（换 --date 或加 --force）", file=sys.stderr)
            return 2
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    delta_rows: list[dict] = []
    full_records: list[dict] = []
    unchanged = 0
    new_state = {"variant": variant, "symbols": dict(known)}

    for path in files:
        symbol = symbol_of(path, variant)
        current_sha = sha256_file(path)
        previous = known.get(symbol)

        if previous and previous.get("sha256") == current_sha:
            unchanged += 1
            continue

        frame = read_frame(path)
        current_rows = int(len(frame))
        current_last = pd.to_datetime(frame["datetime"]).iloc[-1].strftime("%Y-%m-%d")

        # 首次出现，或行数没增长 ⇒ 无法用「尾部追加」表达，整只走 full。
        appended = False
        if previous:
            prev_rows = int(previous.get("rows", 0))
            if current_rows > prev_rows and prefix_digest(frame, prev_rows) == previous.get("prefix"):
                tail = frame.iloc[prev_rows:]
                for _, row in tail.iterrows():
                    delta_rows.append(
                        {
                            "symbol": symbol,
                            "datetime": pd.to_datetime(row["datetime"]).strftime("%Y-%m-%d"),
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": float(row["close"]),
                            "volume": float(row["volume"]),
                            "amount": float(row["amount"]),
                            "is_closed": bool(row.get("is_closed", True)),
                        }
                    )
                appended = True

        if not appended:
            reason = "new" if not previous else "restated"
            full_dir.mkdir(parents=True, exist_ok=True)
            target = full_dir / path.name
            shutil.copy2(path, target)
            full_records.append(
                {"symbol": symbol, "file": f"full/{path.name}", "reason": reason,
                 "sha256": sha256_file(target), "rows": current_rows}
            )

        # 状态一律推进到当前文件的真实值；prefix 供下次判定历史是否被改写。
        new_state["symbols"][symbol] = {
            "sha256": current_sha,
            "rows": current_rows,
            "last": current_last,
            "prefix": prefix_digest(frame, current_rows),
        }

    delta_path = out_dir / "delta.csv.gz"
    payload = pd.DataFrame(delta_rows, columns=["symbol", *STANDARD_COLUMNS])
    with gzip.open(delta_path, "wt", encoding="utf-8", newline="") as handle:
        payload.to_csv(handle, index=False)

    manifest = {
        "format": "quant-cache-delta/v1",
        "variant": variant,
        "date": date_tag,
        "cache_dir": str(cache_dir),
        "files_scanned": len(files),
        "unchanged": unchanged,
        "delta_rows": int(len(delta_rows)),
        "delta_symbols": int(payload["symbol"].nunique()) if len(payload) else 0,
        "full_files": full_records,
        "delta_file": "delta.csv.gz",
        "delta_sha256": sha256_file(delta_path),
        "delta_bytes": delta_path.stat().st_size,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.advance_state:
        save_state(state_dir, variant, new_state)

    print(f"扫描 {len(files)} 只 {variant}：未变 {unchanged}，增量 {len(delta_rows)} 行"
          f"（{len(payload['symbol'].unique()) if len(payload) else 0} 只），整只替换 {len(full_records)} 只")
    print(f"产物 {out_dir}   delta {manifest['delta_bytes']/1024:.1f} KB")
    if not args.advance_state:
        print("（未推进状态；确认无误后重跑并加 --advance-state 才会记账）")
    return 0


# ---------------------------------------------------------------- apply

def cmd_apply(args: argparse.Namespace) -> int:
    delta_dir = Path(args.delta)
    cache_dir = Path(args.cache)
    manifest_path = delta_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: 缺 {manifest_path}", file=sys.stderr)
        return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "quant-cache-delta/v1":
        print(f"ERROR: 未知格式 {manifest.get('format')!r}", file=sys.stderr)
        return 2
    variant = manifest["variant"]

    delta_path = delta_dir / manifest["delta_file"]
    actual = sha256_file(delta_path)
    if actual != manifest["delta_sha256"]:
        print(f"ERROR: delta.csv.gz 校验失败\n  期望 {manifest['delta_sha256']}\n  实际 {actual}", file=sys.stderr)
        return 3

    for record in manifest["full_files"]:
        target = delta_dir / record["file"]
        if sha256_file(target) != record["sha256"]:
            print(f"ERROR: {record['file']} 校验失败", file=sys.stderr)
            return 3

    stats = {"full": 0, "appended": 0, "skipped": 0, "conflict": 0}
    conflicts: list[str] = []

    # 1) 整只替换（复权重述 / 新标的）
    for record in manifest["full_files"]:
        source = delta_dir / record["file"]
        destination = cache_dir / source.name
        if args.dry_run:
            print(f"  [dry-run] full  {record['symbol']} ({record['reason']})")
            stats["full"] += 1
            continue
        frame = read_frame(source)
        frame.attrs.update(expected_attrs(variant))
        write_frame(destination, frame)
        stats["full"] += 1

    # 2) 尾部追加（当天新增 bar）
    if manifest["delta_rows"]:
        table = pd.read_csv(delta_path, dtype={"symbol": str})
        for symbol, group in table.groupby("symbol", sort=True):
            path = cache_dir / f"{symbol}_{variant}.pkl"
            if not path.exists():
                conflicts.append(f"{symbol}: 目标机没有该标的缓存，拒绝只写当天这一行（先落地基线包）")
                stats["conflict"] += 1
                continue

            existing = read_frame(path)
            attrs = dict(existing.attrs)
            indexed = existing.assign(_d=pd.to_datetime(existing["datetime"]).dt.strftime("%Y-%m-%d")).set_index("_d")

            new_rows: list[dict] = []
            for _, row in group.iterrows():
                day = str(row["datetime"])
                record = {
                    "datetime": pd.Timestamp(day),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "amount": float(row["amount"]),
                    "is_closed": bool(row["is_closed"]),
                }
                if day in indexed.index:
                    same = all(abs(float(indexed.loc[day, col]) - record[col]) < 1e-9 for col in PRICE_COLUMNS)
                    if not same:
                        conflicts.append(f"{symbol} {day}: 该日已存在但数值不同（未声明的重述）")
                        stats["conflict"] += 1
                    else:
                        stats["skipped"] += 1
                    continue
                new_rows.append(record)

            if not new_rows:
                continue
            if args.dry_run:
                print(f"  [dry-run] append {symbol}: +{len(new_rows)}")
                stats["appended"] += 1
                continue

            combined = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True)
            combined = combined.drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
            # concat/reset_index 会丢 attrs；下游审计靠 attrs["adjust"] 判口径，必须还原。
            combined.attrs = attrs
            for key, value in expected_attrs(variant).items():
                combined.attrs.setdefault(key, value)
            write_frame(path, combined)
            stats["appended"] += 1

    label = "[dry-run] " if args.dry_run else ""
    print(f"{label}整只替换 {stats['full']}，追加 {stats['appended']}，"
          f"跳过 {stats['skipped']}，冲突 {stats['conflict']}")
    if conflicts:
        for item in conflicts[:20]:
            print(f"  CONFLICT {item}", file=sys.stderr)
        if len(conflicts) > 20:
            print(f"  ... 另有 {len(conflicts)-20} 条", file=sys.stderr)
        return 4
    return 0


# ---------------------------------------------------------------- verify

def cmd_verify(args: argparse.Namespace) -> int:
    delta_dir = Path(args.delta)
    manifest = json.loads((delta_dir / "manifest.json").read_text(encoding="utf-8"))
    problems = 0
    delta_path = delta_dir / manifest["delta_file"]
    actual = sha256_file(delta_path)
    status = "OK" if actual == manifest["delta_sha256"] else "MISMATCH"
    if status != "OK":
        problems += 1
    print(f"  {manifest['delta_file']:<20} {status}")
    for record in manifest["full_files"]:
        target = delta_dir / record["file"]
        ok = target.exists() and sha256_file(target) == record["sha256"]
        if not ok:
            problems += 1
        print(f"  {record['file']:<20} {'OK' if ok else 'MISMATCH'}")
    print(f"{'通过' if problems == 0 else '失败'}：{problems} 个问题；"
          f"增量 {manifest['delta_rows']} 行 / 整只 {len(manifest['full_files'])}")
    return 0 if problems == 0 else 3


# ---------------------------------------------------------------- cli

def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--variant", default="qfq", choices=("qfq", "none"),
                        help="复权口径；delta 与 full 都按口径分目录，混用不会报错但会让回测算错")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE),
                        help=f"daily_history 目录（默认 {DEFAULT_CACHE}）")
    parser.add_argument("--state", default=str(BASE / ".cache-delta-state"),
                        help="发布状态目录，记录「已导出到哪」")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="cache/daily_history 按日增量导出/应用")
    sub = parser.add_subparsers(dest="command", required=True)

    p_base = sub.add_parser("baseline", help="把当前 cache 记为已发布基线（只写状态）")
    add_common(p_base)
    p_base.set_defaults(func=cmd_baseline)

    p_export = sub.add_parser("export", help="对比状态导出增量")
    add_common(p_export)
    p_export.add_argument("--out", required=True, help="产物根目录")
    p_export.add_argument("--date", required=True, help="本次标签，如 2026-10-20")
    p_export.add_argument("--force", action="store_true", help="覆盖已存在的同名产物")
    p_export.add_argument("--advance-state", action="store_true",
                          help="确认无误后推进状态；不加则只演练")
    p_export.set_defaults(func=cmd_export)

    p_apply = sub.add_parser("apply", help="把产物应用到目标 cache（幂等）")
    p_apply.add_argument("--delta", required=True, help="某个 <out>/<variant>/<date> 目录")
    p_apply.add_argument("--cache", default=str(DEFAULT_CACHE))
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.set_defaults(func=cmd_apply)

    p_verify = sub.add_parser("verify", help="只校验产物 sha256")
    p_verify.add_argument("--delta", required=True, help="某个 <out>/<variant>/<date> 目录")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
