"""审计脚本依赖的源码文件哈希契约 —— **单一事实来源**。

## 为什么有这个模块

原先 9 个审计脚本各自内嵌一份 `DEPENDENCY_EXPECTED_SHA256`（同样的 6-8 个键、
同样的值）。这带来两个问题：

1. **同一份值散落 9 处，改一处就漂**；且没有任何机制保证它们一致。
2. **登记口径是混的**：实测 8 个依赖中，2 个按 LF 登记、4 个按 CRLF 登记、
   2 个与任何历史版本的两种形态都不匹配（见下）。在 Windows 检出下
   "LF 的那两个"不匹配，在 Linux 检出下"CRLF 的那几个"不匹配 ——
   **不存在任何一个平台能让全部常量同时成立**。

## 这次怎么修的

- 哈希一律改为 `source_text_sha256`（`\r\n` 归一为 `\n`）→ 与检出平台无关；
- 值集中到本模块 → 9 个调用方各取所需，不再各自维护；
- 逐项重登记，并对**失联项**记录其失联事实（不假装它们曾经对得上）。

## 失联项说明（重要，勿当成"只是行尾问题"）

`backtest_winrate` 与 `utils_helpers` 的旧值经逐修订扫描核实：
**两种行尾形态与全部历史修订都不匹配**（backtest_winrate 14 个修订、
utils_helpers 18 个修订，另加 7 种常见字节变换）。也就是说那两个值
**并非本仓库历史中任何版本的产物**，属永久性失联 ——
本次按当前 HEAD 重新登记，但**不能**追溯它当初锁定的是哪个版本。

其余 4 个可追溯到具体提交（且多为 CRLF 形态），见各项注释。
"""

from __future__ import annotations

from pathlib import Path

# 依赖源码的基准目录（signal_system/）
BASE_DIR = Path(__file__).resolve().parent

# 依赖名 -> 源码路径。键名与各审计的既有用法保持一致，勿改。
DEPENDENCY_PATHS: dict[str, Path] = {
    "attribution_audit": BASE_DIR / "attribution_audit.py",
    "backtest_winrate": BASE_DIR / "backtest_winrate.py",
    "candidate_integrity": BASE_DIR / "candidate_integrity.py",
    "canonical_replay_semantics_audit": BASE_DIR / "canonical_replay_semantics_audit.py",
    "macd_divergence_audit": BASE_DIR / "macd_divergence_audit.py",
    "market_data": BASE_DIR / "data" / "market_data.py",
    "strategy_chan": BASE_DIR / "strategy" / "chan.py",
    "strategy_macd": BASE_DIR / "strategy" / "macd.py",
    "strategy_market_gate": BASE_DIR / "strategy" / "market_gate.py",
    "strategy_signal_policy": BASE_DIR / "strategy" / "signal_policy.py",
    "utils_helpers": BASE_DIR / "utils" / "helpers.py",
    "volume_unit_continuity_audit": BASE_DIR / "volume_unit_continuity_audit.py",
}

# 2026-10-17 全量重新登记（旧值见各项注释）。
# 统一口径：source_text_sha256(path) = sha256(read_bytes().replace(b"\r\n", b"\n"))
DEPENDENCY_EXPECTED_SHA256: dict[str, str] = {
    # 旧值 fdb13ef7… == e84aeb0(2026-08-31) 的 LF 形态。
    # 之后该文件新增 4 个 EXIT_REASON_SEMANTICS 映射
    # (weak_market_low_open / profit_lock / atr_trailing) 并补了行尾换行。
    "attribution_audit": "1eeff99e71409101f504ffdd3f16170adb6355dd6d4e12c1741b3b4f3ef0e50a",
    # 2026-10-22: removed an unused prev_close local from backtest_winrate.py;
    # re-registered the intentional source cleanup.
    "backtest_winrate": "3ec71b4d3692108e8544e1a0b336e92002f009c2f6b7159ba63832ad2b566687",
    # 旧值 fb7e197a… == cc33f13(2026-09-02) 的 LF 形态。
    # 2026-10-17 本文件新增 source_text_sha256()（源码行尾归一哈希），故再次变动。
    # 注意：本文件参与自己的依赖契约（自指），任何改动都会改变此值。
    "candidate_integrity": "5ae4af9615ef67a4d3f0943e3f8906d7d6b13f2b679970c5662da2454037f8dc",
    "canonical_replay_semantics_audit": "691535a8854ad1d3dcd61cd669823fac4131344d86b17db6fcb120c796aac0f3",
    "macd_divergence_audit": "ae17dd4906b52fc07058156ab229c3fe264dbde83183c482e5a6fb6ac919ca1e",
    # 2026-10-22: 行情层遵守 data.disable_system_proxy（系统代理导致 501/断连）。
    # 2026-10-23: 收盘快照刷新幂等化（同一交易日重复调用不再重写未变化的
    # pkl）；行情字段、复权口径与策略读取路径未变。
    "market_data": "5660d97781febf934255fdaab0032ac1ba98422ca5f269b0bf68c581f2764724",
    # 旧值 af70eab3… == c02418a(2026-08-14) 的 **CRLF** 形态 → Windows 产物。
    # 2026-10-23: 包含关系合并与笔 MACD 面积改为数组/二分切片，输出经
    # 300 只真实样本逐字段比对完全一致；策略判定逻辑未变。
    "strategy_chan": "c4d8e6b64d38ffae3141aaf8b5433af8082abaa4b14f8bc7aea28b37f5d3d266",
    # 旧值 cb1e5ff3… == abff923(2026-08-26) 的 **CRLF** 形态 → Windows 产物。
    # 之后新增 _unique_confirmation_entries()（同一确认bar/zone去重）。
    # 2026-10-22: 新增 volume_gate_enabled 参数与 summary 字段（零轴金叉成交量硬门槛）。
    # 2026-10-23: 金叉索引扫描向量化，输出索引经 500 只真实样本逐项比对一致。
    "strategy_macd": "49b532841c08d4c0443345dcbd11f6f1849aae1423df72f4be05b075aaba2016",
    # 旧值即当前值（原本就按 LF 登记）。
    "strategy_market_gate": "fa69dbee01817e44b7b4bc73c50c9a37fa5b4f8b4eb08460dc134979d6d7abe6",
    # 旧值即当前值（原本就按 LF 登记）。
    "strategy_signal_policy": "2288fd4492f860a1705d1539d7b1b5b396a44f81c9e333cb6fafdc3ea2b56d34",
    # **永久失联**：旧值 3ec75b77… 与全部 18 个历史修订的 LF/CRLF 形态
    # 及另 5 种常见字节变换均不匹配。本次按当前 HEAD 登记。
    "utils_helpers": "4a224fad302a98f7fbbf69620969bbc168f259ecf47f92159e69bc22071feabc",
    # 2026-10-23: 仅重新登记 market_data 快照哈希（幂等刷新优化），
    # 本审计的成交量口径断言未变。
    "volume_unit_continuity_audit": "d28a5574d2c391abd93e041b4be6c10699c1b6fef311858e94d8a45f98628bcb",
}


def expected_for(names: object) -> dict[str, str]:
    """取出指定依赖名的登记值；缺名即为契约缺口，直接报错（fail closed）。"""
    missing = [str(n) for n in names if str(n) not in DEPENDENCY_EXPECTED_SHA256]
    if missing:
        raise KeyError(
            f"dependency hash contract is missing entries for: {sorted(missing)}"
        )
    return {str(n): DEPENDENCY_EXPECTED_SHA256[str(n)] for n in names}


def protected_sha256(path: Path) -> str:
    """审计快照用的哈希：**源码按行尾归一，数据文件逐字节**。

    快照里混着两类对象 —— 审计依赖的源码（.py）与历史/候选数据
    （.pkl/.json/.jsonl）。前者必须归一（否则哈希随检出平台变），
    后者必须逐字节（归一化会改变内容语义）。
    """
    from candidate_integrity import file_sha256, source_text_sha256

    return source_text_sha256(path) if path.suffix == ".py" else file_sha256(path)
