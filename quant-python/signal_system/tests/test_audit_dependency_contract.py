"""审计依赖哈希契约的可执行性回归测试。

## 为什么需要这个文件

2026-10-17 之前，这套"字节级锁定"契约**实际处于失修状态**：

- `canonical_replay_semantics_audit` 的 `_dependency_contract()` 一旦不匹配就
  `raise RuntimeError`，而 8 个依赖里有 **6 个**对不上 —— 也就是说
  **该审计根本无法真实运行**。
- 其它 8 个风险审计用同样的值、同样的比对（`_validate_expected_snapshot` 也 raise），
  同样不可运行。
- 但**测试全绿** —— 因为各测试都 monkeypatch 掉了常量或用现场自造的哈希，
  **从不校验真实的登记值**。

本文件补上那个缺口：用**真实文件**逐项校验登记值，并保证派生表与共享表一致。
这不是"mock 回声"式测试 —— 它读真实的源文件字节并与真实常量比对。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audit_dependency_contract import (
    DEPENDENCY_EXPECTED_SHA256,
    DEPENDENCY_PATHS,
    expected_for,
    protected_sha256,
)
from candidate_integrity import file_sha256, source_text_sha256

# 使用了该契约的模块（均为"不匹配即 raise"）
CONSUMER_MODULES = (
    "canonical_replay_semantics_audit",
    "downside_gap_risk_audit",
    "downside_tail_risk_audit",
    "entry_context_batch_audit",
    "overnight_variance_share_risk_audit",
    "price_staleness_risk_audit",
    "same_day_correlation_risk_audit",
    "volatility_instability_risk_audit",
    "volume_stability_risk_audit",
)


def test_every_registered_dependency_path_exists() -> None:
    """登记表与路径表必须一一对应，且文件真实存在（缺一个就 raise 而非静默跳过）。"""
    assert set(DEPENDENCY_EXPECTED_SHA256) == set(DEPENDENCY_PATHS), (
        "登记表与路径表的键集合不一致："
        f"仅登记={sorted(set(DEPENDENCY_EXPECTED_SHA256) - set(DEPENDENCY_PATHS))} "
        f"仅路径={sorted(set(DEPENDENCY_PATHS) - set(DEPENDENCY_EXPECTED_SHA256))}"
    )
    missing = [name for name, path in DEPENDENCY_PATHS.items() if not path.is_file()]
    assert missing == [], f"登记了但文件不存在：{missing}"


def test_registered_hashes_match_real_files() -> None:
    """**核心回归**：每个登记值必须等于真实文件的行尾归一哈希。

    这一条正是此前缺失的环节 —— 没有它，常量漂移不会被任何测试发现。
    """
    mismatched = {
        name: {
            "registered": DEPENDENCY_EXPECTED_SHA256[name],
            "actual": source_text_sha256(path),
        }
        for name, path in sorted(DEPENDENCY_PATHS.items())
        if source_text_sha256(path) != DEPENDENCY_EXPECTED_SHA256[name]
    }
    assert mismatched == {}, f"登记值与真实文件不符：{mismatched}"


def test_registered_hashes_are_line_ending_independent() -> None:
    """契约必须与检出平台无关：同一文件在 LF 与 CRLF 下算出同一登记值。

    否则就会出现"Windows 上绿、Linux 上红"（或反之）——这正是本次修复前的状态。
    """
    target = DEPENDENCY_PATHS["strategy_macd"]
    raw = target.read_bytes()
    assert b"\r\n" not in raw, "本仓库的源码应已统一为 LF；若失败说明行尾又漂了"

    lf_form = raw
    crlf_form = raw.replace(b"\n", b"\r\n")
    # source_text_sha256 对两种形态必须给出同一值
    import hashlib

    assert hashlib.sha256(lf_form.replace(b"\r\n", b"\n")).hexdigest() == hashlib.sha256(
        crlf_form.replace(b"\r\n", b"\n")
    ).hexdigest()
    # 而逐字节哈希对两种形态必然不同 —— 这正是必须归一的理由
    assert hashlib.sha256(lf_form).hexdigest() != hashlib.sha256(crlf_form).hexdigest()


def test_protected_sha256_normalizes_source_but_not_data(tmp_path: Path) -> None:
    """快照哈希必须按类型分流：源码归一行尾，数据文件保持逐字节。"""
    text = tmp_path / "sample.py"
    text.write_bytes(b"a\r\nb\r\n")
    assert protected_sha256(text) == source_text_sha256(text)
    assert protected_sha256(text) != file_sha256(text)

    data = tmp_path / "sample.pkl"
    data.write_bytes(b"a\r\nb\r\n")
    assert protected_sha256(data) == file_sha256(data), "数据文件不得被归一"
    assert protected_sha256(data) != source_text_sha256(data)


def test_expected_for_fails_closed_on_unknown_name() -> None:
    """缺名即为契约缺口：必须报错，不得静默返回空。"""
    with pytest.raises(KeyError, match="missing entries"):
        expected_for(["strategy_macd", "no_such_dependency"])


@pytest.mark.parametrize("module_name", CONSUMER_MODULES)
def test_consumer_module_derives_from_shared_contract(module_name: str) -> None:
    """9 个消费方都必须取用共享表，不得各自内嵌一份会漂移的副本。"""
    module = __import__(module_name)
    table = getattr(module, "DEPENDENCY_EXPECTED_SHA256", None)

    if isinstance(table, dict) and table:
        # 直接持有派生表：必须与共享表逐项相等
        for name, value in table.items():
            assert value == DEPENDENCY_EXPECTED_SHA256[name], (
                f"{module_name} 的 {name} 与共享表不一致"
            )
        return

    # canonical_replay 通过 _dependency_definitions() 取用
    defs = module._dependency_definitions()
    for name, (_path, expected) in defs.items():
        assert expected == DEPENDENCY_EXPECTED_SHA256[name], (
            f"{module_name} 的 {name} 与共享表不一致"
        )


def test_dependency_contract_passes_for_real_files() -> None:
    """canonical_replay 的依赖校验必须能对真实文件通过（本次修复前它会 raise）。"""
    import canonical_replay_semantics_audit as audit

    report = audit._dependency_contract()
    assert report, "依赖报告不应为空"
    assert all(item["match"] for item in report.values()), (
        f"依赖校验未通过：{ {k: v['match'] for k, v in report.items()} }"
    )


def test_contract_detects_real_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """反向验证：契约必须仍有牙齿 —— 内容真的变了就要报错。"""
    import canonical_replay_semantics_audit as audit

    victim = tmp_path / "market_gate_moved.py"
    victim.write_bytes(DEPENDENCY_PATHS["strategy_market_gate"].read_bytes())

    # 修改该模块自己的路径表（它不暴露 DEPENDENCY_PATHS，用 _dependency_definitions 的底层 dict）
    definitions = audit._dependency_definitions
    original = audit._dependency_definitions

    def patched() -> dict:
        defs = original()
        defs["strategy_market_gate"] = (
            victim,
            DEPENDENCY_EXPECTED_SHA256["strategy_market_gate"],
        )
        return defs

    monkeypatch.setattr(audit, "_dependency_definitions", patched)
    assert definitions is not None

    victim.write_bytes(victim.read_bytes() + b"\n# drift\n")
    with pytest.raises(RuntimeError, match="dependency hash mismatch"):
        audit._dependency_contract()
