import fs from "node:fs/promises";
import path from "node:path";
import { createJob, getJob, getDb, updateJob, updateJobPayload, addOperationLog } from "./db";
import { buildOverrides, freezeEngineConfig } from "./config";
import { portfolioSnapshot } from "./portfolio";
import type { PoolClient } from "pg";
import { runBridge } from "./bridge";
import { STRATEGIES, type StrategyId } from "./analysis-types";
import { signalSystemDir } from "./paths";
import { nowIso } from "./time";
const running = new Set<number>();
export async function startScanBatch(kind: string, input: Record<string, unknown>): Promise<number> {
  // 与 analyze 批次一致地带上账户快照：AI 解读读的是 portfolio_context，
  // 只写顶层 holdings 会让解读看不到持仓（历史 bug）。
  const payload = { ...input, scan_version: 2, portfolio_context: await portfolioSnapshot(),
    frozen_overrides: await freezeEngineConfig(), strategies: input.scan_kind && input.scan_kind !== "all" ? [input.scan_kind] : STRATEGIES.map(row => row.id), progress: {} };
  const id = typeof input.existing_job_id === "number" ? input.existing_job_id : await createJob(kind, payload);
  if (input.existing_job_id) await updateJobPayload(id, payload);
  void executeScanBatch(id).catch(async error => { await updateJob(id, { status: "failed", error: String(error), finished_at: nowIso() }); });
  return id;
}
export async function executeScanBatch(id: number) {
  if (running.has(id)) return;
  running.add(id);
  let client: PoolClient | undefined;
  try {
    client = await (await getDb()).connect();
    // One owner for the shared Python scan cursors and published candidate pools.
    if (!(await client.query("SELECT pg_try_advisory_lock(920001) AS acquired")).rows[0].acquired) {
      return;
    }
    const job = await getJob(id);
    if (!job) return;
    const payload = JSON.parse(job.payload) as Record<string, unknown>;
    const strategies = payload.strategies as StrategyId[];
    const progress = (payload.progress ?? {}) as Record<string, Record<string, unknown>>;
    await updateJob(id, { status: "running", started_at: job.started_at ?? nowIso(), error: null });
    const errors: string[] = [];
    for (const strategy of strategies) {
      if (progress[strategy]?.completed_round) continue;
      let unchanged = 0;
      let previous = -1;
      for (let batch = 0; batch < 100; batch++) {
        const overrides = { ...await buildOverrides(), ...(payload.frozen_overrides as object ?? {}) };
        const outcome = await runBridge("scan", { scan_kind: strategy, scan_run_id: String(id), notify: false,
          overrides: { ...overrides, macd_divergence: { ...(overrides.macd_divergence as object ?? {}), universe_mode: "all_a" }, scan: { ...(overrides.scan as object ?? {}), universe_mode: "all_a" } } }, { timeoutMs: 3_600_000 });
        if (!outcome.ok) { errors.push(`${strategy}: ${outcome.error}`); break; }
        const report = (outcome.data as { report: Record<string, unknown> }).report;
        progress[strategy] = report;
        // Keep task metadata small; full candidate rows remain in the original report file.
        const summary = Object.fromEntries(Object.entries(report).filter(([key]) => !["candidates", "results"].includes(key)));
        progress[strategy] = summary;
        payload.progress = progress;
        await updateJobPayload(id, payload);
        if (report.completed_round) break;
        const coverage = Number(report.coverage ?? 0);
        unchanged = coverage <= previous ? unchanged+1 : 0;
        previous = coverage;
        if (unchanged >= 3 || batch === 99) { errors.push(`${strategy}: 数据覆盖未继续增长，保留上次完整股票池，可重试`); break; }
      }
    }
    const completed = strategies.every(strategy => progress[strategy]?.completed_round === true);
    const report = { schema_version: 2, mode: "scan", scanned_at: nowIso(), completed_round: completed,
      pools: progress, errors, universe_mode: "all_a" };
    const directory = path.join(signalSystemDir, "output");
    await fs.mkdir(directory, { recursive: true });
    const output = path.join(directory, `scan_v2_job_${id}.json`);
    await fs.writeFile(`${output}.tmp`, JSON.stringify(report, null, 2)); await fs.rename(`${output}.tmp`, output);
    await updateJob(id, { status: completed ? "success" : "failed", result_path: output,
      error: completed ? null : errors.join("\n") || "部分策略未完成", finished_at: nowIso() });
    await addOperationLog({ job_id: id, module: "scan", level: completed ? "info" : "warning", message: `全市场筛选：${strategies.filter(strategy => progress[strategy]?.completed_round).length}/${strategies.length} 个策略完成` });
    if (payload.notify === true) {
      const content = STRATEGIES.filter(strategy => strategies.includes(strategy.id)).map(strategy => {
        const result = progress[strategy.id]; return `- ${strategy.name}：${result?.completed_round ? "完成" : "未完成"}，候选 ${result?.candidate_count ?? "—"} 只，覆盖 ${((Number(result?.coverage) || 0)*100).toFixed(1)}%`;
      }).join("\n");
      await runBridge("notify-summary", { title: "每日三策略筛选汇总", notification_kind: "candidate_pool", content, report_path: output,
        confirmed_at: job.created_at.replace(" ", "T"), overrides: await buildOverrides() }, { timeoutMs: 120_000 });
    }
  } finally { if (client) { await client.query("SELECT pg_advisory_unlock(920001)").catch(() => undefined); client.release(); } running.delete(id); }
}
export async function resumeScanBatches() {
  const jobs = (await (await getDb()).query("SELECT id FROM quant.jobs WHERE status IN ('pending','running') AND payload::jsonb->>'scan_version'='2' ORDER BY id")).rows;
  for (const row of jobs) void executeScanBatch(Number(row.id)).catch(async error => { await updateJob(Number(row.id), { status: "failed", error: String(error), finished_at: nowIso() }); });
}
