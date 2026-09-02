"""Holdout 冻结清单生成器 (freeze_holdout.py v1.0.0).

在生成 holdout 候选之前运行，把时间外验证的边界一次性冻住：

- 时间窗口：holdout 从 ``--start``（默认 2026-09-01，2026-09 之后）开始，
  结束开放（随每日数据推进）。
- 股票池：全 A 候选池。候选生成时必须以同一个 manifest 清单为准；
  本脚本记录候选池 hash，候选文件生成后写入候选 hash 以锁定。
- 规则：bear/range 下 macd_near 降级 observe_only 的单一变量规则定义；
  其余入场/退出/费用/滑点/T+1 全部固定为生产语义。
- 配置：记录 config 文件 hash 与生产快照 hash。
- 样本门槛：30 个唯一弱市 near 候选、10 只不同股票、10 个不同信号日。

冻结的目的是「候选生成前先冻结时间边界与规则」：之后任何对规则、
窗口、股票池的改动都会使 hash 不匹配，从而被审计脚本拒绝。
本脚本不修改生产 config，不连接生产库。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from attribution_audit import _config_snapshot
from backtest_winrate import HISTORY_DIR
from utils.helpers import load_config

from macd_near_regime_audit import (
    MIN_WEAK_NEAR_COUNT,
    MIN_WEAK_NEAR_UNIQUE_DAYS,
    MIN_WEAK_NEAR_UNIQUE_SYMBOLS,
    NEAR_SIGNAL,
    WEAK_REGIMES,
)
from holdout_integrity import (
    canonical_json,
    compute_freeze_seal,
    file_sha256,
    runtime_versions,
    sha256_text,
    universe_sha256,
)

FREEZE_VERSION = "holdout_freeze.v2"
DEFAULT_HOLDOUT_START = "2026-09-01"

# Files whose content defines the experiment rules.  Any change after freezing
# breaks the rule hash and the audit script will refuse to run.
_ROOT_RULE_FILES = (
    "macd_near_regime_audit.py",
    "freeze_holdout.py",
    "generate_holdout_candidates.py",
    "holdout_integrity.py",
    "backtest_winrate.py",
    "attribution_audit.py",
)


def _sha256(text: str) -> str:
    return sha256_text(text)


def _file_sha256(path: Path) -> str:
    return file_sha256(path)


def _canonical_json(value: Any) -> str:
    return canonical_json(value)


def _runtime_rule_files() -> tuple[str, ...]:
    """Return the exact runtime dependency bundle frozen for this experiment."""
    paths = [BASE_DIR / name for name in _ROOT_RULE_FILES]
    dependency_roots = (
        BASE_DIR / "strategy",
        BASE_DIR / "utils",
        BASE_DIR.parent / "core" / "selector",
        BASE_DIR.parent / "core" / "strategy",
    )
    for root in dependency_roots:
        if root.exists():
            paths.extend(root.rglob("*.py"))
    names = {
        Path(os.path.relpath(path.resolve(), BASE_DIR)).as_posix()
        for path in paths
        if path.exists() and "__pycache__" not in path.parts
    }
    return tuple(sorted(names))


def _universe_from_cache() -> list[str]:
    """Return the sorted set of symbols that have an adjusted qfq history.

    This is the deterministic local fallback for the candidate pool.  A full
    A-share universe file (--universe-file) is preferred when available.
    """
    symbols = sorted(
        {
            path.name.split("_")[0].zfill(6)
            for path in HISTORY_DIR.glob("*_qfq.pkl")
        }
    )
    return symbols


def _load_universe_file(path: Path) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        for key in ("symbols", "codes", "universe", "stock_list"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            raise ValueError(
                f"universe file {path} must be a list or contain a symbols/codes key"
            )
    if not isinstance(raw, list):
        raise ValueError(f"universe file {path} must contain a JSON list")
    return sorted({str(item).zfill(6) for item in raw if str(item).strip()})


def build_rule_definition() -> dict[str, Any]:
    return {
        "experiment": "bear_range_near_observe_only",
        "signal": NEAR_SIGNAL,
        "weak_regimes": sorted(WEAK_REGIMES),
        "bull_near_unchanged": True,
        "fixed_exit_semantics": "SL8/TP30/T+1/fees/slippage/price-limit/timeout",
        "sample_gates": {
            "min_weak_near_unique_candidates": MIN_WEAK_NEAR_COUNT,
            "min_weak_near_unique_symbols": MIN_WEAK_NEAR_UNIQUE_SYMBOLS,
            "min_weak_near_unique_signal_days": MIN_WEAK_NEAR_UNIQUE_DAYS,
        },
        "collection_end_rule": (
            "first signal_day where all sample_gates are satisfied; "
            "the rule uses entry-time candidate metadata only"
        ),
        "candidate_payload": "entry_time_fields_only_no_outcomes",
        "candidate_generation_once": True,
    }


def freeze(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise RuntimeError(f"config not found: {config_path}")
    config = load_config(str(config_path))
    if config is None:
        raise RuntimeError(f"unable to load config: {args.config}")

    start = date.fromisoformat(args.start)
    if start < date.fromisoformat(DEFAULT_HOLDOUT_START):
        raise ValueError(
            f"holdout start {start} is before the frozen boundary "
            f"{DEFAULT_HOLDOUT_START}; the 2026-09 window is the earliest allowed"
        )

    if args.universe_file:
        universe = _load_universe_file(Path(args.universe_file).expanduser().resolve())
        universe_source = f"file:{args.universe_file}"
    else:
        universe = _universe_from_cache()
        universe_source = f"local_daily_history_cache:{len(universe)}_symbols"

    rules = build_rule_definition()
    rule_hash = _sha256(_canonical_json(rules))
    code_hashes = {
        name: _file_sha256(BASE_DIR / name)
        for name in _runtime_rule_files()
        if (BASE_DIR / name).exists()
    }
    config_snapshot = _config_snapshot(config)
    freeze_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest: dict[str, Any] = {
        "version": FREEZE_VERSION,
        "label": args.label,
        "frozen_at_utc": freeze_time,
        "holdout_window": {
            "start": start.isoformat(),
            "end": None,  # open: advances with each trading day
            "rule": "signal_day >= start; collected before any rule adjustment",
        },
        "universe": {
            "source": universe_source,
            "count": len(universe),
            "manifest_sha256": universe_sha256(universe),
            "symbols": universe,
        },
        "rules": rules,
        "rules_sha256": rule_hash,
        "code_hashes": code_hashes,
        "config": {
            "file": str(config_path),
            "file_sha256": _file_sha256(config_path),
            "snapshot": config_snapshot,
            "snapshot_sha256": _sha256(_canonical_json(config_snapshot)),
        },
        "runtime": runtime_versions(),
        "candidates": {
            "generated": False,
            "candidates_file": None,
            "candidates_manifest_sha256": None,
            "note": "filled by the holdout candidate generation step; "
            "must be written before the audit script accepts the window",
        },
        "seal": None,  # computed below
    }
    manifest["seal"] = compute_freeze_seal(manifest)

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "holdout_freeze.json"
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=1)
    # Deterministic copy for the audit script to consume.
    with (output_dir / "holdout_freeze.seal").open("w", encoding="utf-8") as handle:
        handle.write(manifest["seal"])
    return {
        "manifest_path": str(manifest_path),
        "seal": manifest["seal"],
        "holdout_start": manifest["holdout_window"]["start"],
        "universe_count": len(universe),
        "universe_source": universe_source,
        "rules_sha256": rule_hash,
        "config_file_sha256": manifest["config"]["file_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=r"D:\tmp\holdout")
    parser.add_argument(
        "--start",
        default=DEFAULT_HOLDOUT_START,
        help=f"holdout start date (ISO); default {DEFAULT_HOLDOUT_START}",
    )
    parser.add_argument(
        "--universe-file",
        default=None,
        help="JSON list of candidate-pool symbols (preferred for full A-share)",
    )
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--label", default="macd-near-holdout")
    args = parser.parse_args()
    summary = freeze(args)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
