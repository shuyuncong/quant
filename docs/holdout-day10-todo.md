# Holdout 第 10 个交易日执行 TODO

## 状态

- [ ] 待执行
- 计划时间：2026-09-14 19:00（Asia/Shanghai）
- 计算口径：Holdout 从 2026-09-01 开始，预计 2026-09-14 收盘后累计 10 个完整交易日。
- Windows 计划任务：`Quant Holdout Day10 Readiness`

## 目标

统一更新冻结 universe 的本地行情缓存，并运行只读
`holdout_readiness_status.py`。本任务只确认是否达到一次性候选生成的
前置条件，不生成 Holdout 候选。

## 执行步骤

1. 使用本地 HTTP 行情接口更新 5007 只冻结股票的 QFQ/none 日线和上证指数：

   ```powershell
   python D:\tmp\prep_holdout_cache.py `
     --adjust both `
     --workers 1 `
     --limit 800 `
     --interval 3 `
     --retries 8
   ```

2. 运行只读状态检查：

   ```powershell
   python holdout_readiness_status.py
   ```

3. 将日志和 JSON 保存到：

   ```text
   D:\tmp\holdout_day10_execution
   ```

## 决策门

- `state = ready_to_probe_candidates` 且 `holdout_trading_days >= 10`：
  写入 `READY_FOR_MANUAL_CANDIDATE_GENERATION.json`，然后停止，等待主模型复核。
- `state = waiting_for_signal_days`：停止，不运行生成器。
- `state = data_cache_not_ready`：停止，先修复本地缓存。
- `candidates.generated = true`：永久禁止再次运行生成器。
- 任何 seal、freeze 或 config 完整性失败：立即停止。

## 严格禁止

- 不连接生产 PostgreSQL。
- 不修改生产 config、holdout freeze/seal 或冻结代码。
- 不运行 `generate_holdout_candidates.py`。
- 不生成或覆盖 `candidates_holdout.jsonl`、`holdout_candidates.seal`。
- 不运行组合层或最终 Holdout 审计。

## 完成条件

计划任务成功产生 readiness JSON 后，把完整结果交给主模型。只有主模型复核并明确授权后，其他模型才可执行一次性候选生成。
