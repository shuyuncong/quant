from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import long_history_yearline_pullback_audit as audit


def _make_report(root: Path, screen_passed: bool | None) -> Path:
    root.mkdir()
    artifacts: dict[str, dict[str, object]] = {}
    for route in ("volume_breakout", "yearline_pullback", "combined"):
        for profile in ("fixed_sl8", "fixed_sl5", "dynamic_sl5_sl8"):
            for split in audit.SPLITS:
                name = f"{route}_{profile}_{split}.jsonl"
                path = root / name
                path.write_text("", encoding="utf-8", newline="\n")
                artifacts[name] = {
                    "path": str(path),
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "rows": 0,
                }
    screen: dict[str, object] = {}
    if screen_passed is not None:
        screen["passes_research_screen"] = screen_passed
    report = {
        "version": audit.INPUT_VERSION,
        "policy": {"holdout_used": False},
        "routes": {"yearline_pullback": {"screen": screen}},
        "artifacts": artifacts,
    }
    (root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8", newline="\n"
    )
    return root


def test_validate_report_allows_failed_research_screen(tmp_path: Path) -> None:
    report, integrity = audit._validate_report(_make_report(tmp_path / "failed", False))

    assert report["routes"]["yearline_pullback"]["screen"][
        "passes_research_screen"
    ] is False
    assert len(integrity["artifacts"]) == 27


def test_validate_report_rejects_malformed_screen_metadata(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="invalid pullback screen metadata"):
        audit._validate_report(_make_report(tmp_path / "malformed", None))
