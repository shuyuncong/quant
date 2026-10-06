import fs from "node:fs";
import path from "node:path";
import { runBridge } from "./bridge";
import { buildOverrides } from "./config";
import {
  addNote,
  addOperationLog,
  createJob,
  findNoteByJobAndResult,
  getJob,
  listRecoverableJobs,
  updateJob,
  updateJobPayload,
} from "./db";
import { nowIso } from "./time";
import { interpretReportWithFallback, pickChatModel, extractStandpoints } from "./llm";
import { signalSystemDir } from "./paths";
import {
  buildHoldingsContext,
  historyCompleteFromJobPayload,
  holdingsFromJobPayload,
  totalCapitalFromJobPayload,
  tradesFromJobPayload,
} from "./holdings-context";

export type JobKind =
  | "analyze"
  | "scan"
  | "daily-scan"
  | "monitor-once"
  | "monitor-cycle"
  | "test-notify"
  | "dispatch-outbox";

// 需要单独起一份 AI 解读的任务。
//
// 只列"自己不产出 AI 结论"的 kind：analyze / monitor-once / monitor-cycle 走五页签
// 批次，会自己写出 technical + synthesis 并落一条 note，再无条件起子任务解读就是
// 同一份内容读第二遍、再推一条一模一样的消息（实测每个 analyze 任务推两次）。
// scan / daily-scan 的"汇总"是候选数统计而不是 AI 结论，所以仍然需要这一步。
const AUTO_INTERPRET_KINDS: JobKind[] = [
  "scan",
  "daily-scan",
];

function resolveReportPath(resultPath: string): string {
  return path.isAbsolute(resultPath) ? resultPath : path.resolve(signalSystemDir, resultPath);
}

interface InterpretationPayload {
  parent_job_id: number;
  result_path: string;
  notification_at: string;
  /** 父任务是否要求推送。历史任务是 true（那时无条件推送），缺省按 true 处理。 */
  notify?: boolean;
}

const activeInterpretationJobs = new Set<number>();

/** Read the generated report, call the chat model, persist the note, and enqueue its notification. */
async function autoInterpret(
  interpretationJobId: number,
  parentJobId: number,
  resultPath: string,
  notificationAt: string,
  notify: boolean
): Promise<void> {
  const existingNote = await findNoteByJobAndResult(parentJobId, resultPath);
  const profile = existingNote ? null : await pickChatModel();
  if (!existingNote && !profile) {
    await addOperationLog({
      job_id: interpretationJobId,
      level: "warning",
      module: "auto-interpret",
      message: "自动解读跳过：未启用或未配置 API Key 的模型",
    });
    return;
  }
  try {
    let content = existingNote?.content ?? "";
    let noteId = existingNote?.id ?? 0;
    let modelName = existingNote?.model ?? "";
    if (!content) {
      const full = resolveReportPath(resultPath);
      const reportText = fs.readFileSync(full, "utf8");
      const parentPayload = (await getJob(parentJobId))?.payload;
      const context = buildHoldingsContext(reportText, {
        holdings: holdingsFromJobPayload(parentPayload),
        trades: tradesFromJobPayload(parentPayload),
        totalCapital: totalCapitalFromJobPayload(parentPayload),
        historyComplete: historyCompleteFromJobPayload(parentPayload),
      });
      const interpreted = await interpretReportWithFallback(reportText, context);
      content = interpreted.content;
      modelName = interpreted.model.name;
      noteId = await addNote({
        job_id: parentJobId,
        result_path: resultPath,
        content,
        model: modelName,
      });
    }
    const standpoints = extractStandpoints(content);
    if (!existingNote) {
      await addOperationLog({
        job_id: interpretationJobId,
        level: "info",
        module: "auto-interpret",
        message: "自动解读完成",
        detail:
          `模型 ${modelName}，笔记 #${noteId}` +
          (standpoints.length > 0 ? `\n操作主张：${standpoints.join("；")}` : ""),
      });
    }
    if (!notify) return;
    const pushOutcome = await runBridge(
      "notify-summary",
      {
        title: `AI自动解读 #${parentJobId}`,
        content,
        report_path: resultPath,
        confirmed_at: notificationAt,
        action_summary: standpoints.join("；"),
        overrides: await buildOverrides(),
      },
      { timeoutMs: 120_000 }
    );
    if (!pushOutcome.ok) {
      await addOperationLog({
        job_id: interpretationJobId,
        level: "warning",
        module: "auto-interpret",
        message: "AI 解读已保存，但推送失败",
        detail: pushOutcome.error || "未知推送错误",
      });
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await addOperationLog({
      job_id: interpretationJobId,
      level: "error",
      module: "auto-interpret",
      message: "自动解读失败",
      detail: message,
    });
    console.error(`[auto-interpret] job #${interpretationJobId} failed:`, error);
    throw error;
  }
}

async function executeInterpretationJob(jobId: number, payload: InterpretationPayload): Promise<void> {
  if (activeInterpretationJobs.has(jobId)) return;
  activeInterpretationJobs.add(jobId);
  await updateJob(jobId, { status: "running", started_at: nowIso(), error: null, finished_at: null });
  try {
    await autoInterpret(
      jobId,
      payload.parent_job_id,
      payload.result_path,
      payload.notification_at,
      payload.notify !== false
    );
    await updateJob(jobId, {
      status: "success",
      result_path: payload.result_path,
      finished_at: nowIso(),
    });
  } catch (error) {
    await updateJob(jobId, {
      status: "failed",
      error: error instanceof Error ? error.message : String(error),
      finished_at: nowIso(),
    });
  } finally {
    activeInterpretationJobs.delete(jobId);
  }
}

async function createInterpretationJob(parentJobId: number, resultPath: string, notify: boolean): Promise<{
  jobId: number;
  payload: InterpretationPayload;
}> {
  const payload: InterpretationPayload = {
    parent_job_id: parentJobId,
    result_path: resultPath,
    notification_at: nowIso().replace(" ", "T"),
    notify,
  };
  const jobId = await createJob("interpret-report", payload);
  return { jobId, payload };
}

export async function resumeInterpretationJobs(): Promise<void> {
  for (const job of await listRecoverableJobs("interpret-report")) {
    try {
      const raw = JSON.parse(job.payload) as Partial<InterpretationPayload>;
      if (!raw.parent_job_id || !raw.result_path) throw new Error("解读任务载荷不完整");
      void executeInterpretationJob(job.id, {
        parent_job_id: Number(raw.parent_job_id),
        result_path: String(raw.result_path),
        notification_at: String(raw.notification_at || job.created_at.replace(" ", "T")),
        notify: raw.notify,
      });
    } catch (error) {
      await updateJob(job.id, {
        status: "failed",
        error: error instanceof Error ? error.message : String(error),
        finished_at: nowIso(),
      });
    }
  }
}

const KIND_TO_COMMAND: Record<JobKind, string> = {
  analyze: "analyze",
  scan: "scan",
  "daily-scan": "scan",
  "monitor-once": "monitor-once",
  "monitor-cycle": "monitor-once",
  "test-notify": "test-notify",
  "dispatch-outbox": "dispatch-outbox",
};

export function shouldAutoInterpret(kind: JobKind, report: Record<string, unknown> | undefined): boolean {
  if (!AUTO_INTERPRET_KINDS.includes(kind)) return false;
  // 年线 / 底背离扫描是研究展示池, 不进入生产信号链路, 也不做 AI 自动解读
  if (report?.pool_type === "yearline_pullback") return false;
  if (report?.pool_type === "macd_divergence") return false;
  if (["analyze", "scan", "monitor-once"].includes(kind)) return true;
  if (kind === "daily-scan") {
    return report?.completed_round === true && Number(report?.candidate_count ?? 0) > 0;
  }
  return Number(report?.new_events ?? 0) > 0;
}

/** 浅合并 override：extra 优先，顶层对象键递归一层合并（如 scan.universe_mode）。 */
function mergeOverrides(
  base: Record<string, unknown>,
  extra?: Record<string, unknown>
): Record<string, unknown> {
  const result: Record<string, unknown> = { ...base };
  if (!extra) return result;
  for (const [key, value] of Object.entries(extra)) {
    const existing = result[key];
    if (
      value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      existing &&
      typeof existing === "object" &&
      !Array.isArray(existing)
    ) {
      result[key] = { ...(existing as Record<string, unknown>), ...(value as Record<string, unknown>) };
    } else {
      result[key] = value;
    }
  }
  return result;
}

/** 后台解析手动输入代码的名称并回写 payload；失败只影响列表展示，不影响任务执行。 */
async function persistSymbolNames(jobId: number, payload: Record<string, unknown>): Promise<void> {
  try {
    const outcome = await runBridge("resolve-names", { symbols: payload.symbols }, { timeoutMs: 20_000 });
    const data = outcome.data;
    if (!outcome.ok || !data || typeof data !== "object" || !("names" in data)) return;
    const names = data.names;
    if (!names || typeof names !== "object") return;
    await updateJobPayload(jobId, { ...payload, symbol_names: names });
  } catch {
    /* 名称解析失败时保留原始 payload，列表展示回退到代码本身 */
  }
}

/** Create a job row and run the bridge command in the background. Returns the job id immediately. */
export async function startJob(kind: JobKind, payload: Record<string, unknown>): Promise<number> {
  if (["scan", "daily-scan"].includes(kind)) {
    const { startScanBatch } = await import("./scan-service");
    return startScanBatch(kind, payload);
  }
  if (["analyze", "monitor-once", "monitor-cycle"].includes(kind)) {
    const { startAnalysisBatch } = await import("./analysis-service");
    return startAnalysisBatch(kind, payload);
  }
  const jobId = await createJob(kind, payload);
  await updateJob(jobId, { status: "running", started_at: nowIso() });
  if (kind === "analyze" && Array.isArray(payload.symbols) && payload.symbols.length > 0) {
    // 手动输入的代码往往不在股票池/持仓，最近任务列表会缺失名称；解析放在后台并回写 payload，
    // 否则每次点击都要等一个 Python 子进程（约 2s 起）才拿到 202，用户会以为没反应而连点。
    void persistSymbolNames(jobId, payload);
  }
  await addOperationLog({
    job_id: jobId,
    level: "info",
    module: "job",
    message: `任务启动（${kind}）`,
    detail: JSON.stringify(payload),
  });
  const { overrides: extraOverrides, ...rest } = payload;
  const bridgePayload = {
    ...rest,
    overrides: mergeOverrides(await buildOverrides(), extraOverrides as Record<string, unknown> | undefined),
  };
  const timeoutMs = kind === "test-notify" ? 60_000 : kind === "dispatch-outbox" ? 120_000 : 0;
  runBridge(KIND_TO_COMMAND[kind], bridgePayload, { timeoutMs })
    .then(async (outcome) => {
      if (outcome.ok) {
        const report = (outcome.data as { report?: Record<string, unknown> } | undefined)?.report;
        const resultPath = typeof report?.output_file === "string" ? report.output_file : null;
        // Persist the child before marking the parent successful.  A process
        // exit between these writes can then be recovered by the scheduler.
        const interpretation =
          resultPath && shouldAutoInterpret(kind, report)
            ? await createInterpretationJob(jobId, resultPath, payload.notify !== false)
            : null;
        await updateJob(jobId, {
          status: "success",
          result_path: resultPath,
          finished_at: nowIso(),
        });
        await addOperationLog({
          job_id: jobId,
          level: "info",
          module: "job",
          message: `任务完成（${kind}）`,
          detail: resultPath ?? undefined,
        });
        if (interpretation) {
          void executeInterpretationJob(interpretation.jobId, interpretation.payload);
        }
      } else {
        await updateJob(jobId, { status: "failed", error: outcome.error || "执行失败", finished_at: nowIso() });
        await addOperationLog({
          job_id: jobId,
          level: "error",
          module: "job",
          message: `任务失败（${kind}）`,
          detail: outcome.error || "执行失败",
        });
      }
    })
    .catch(async (error) => {
      await updateJob(jobId, { status: "failed", error: String(error), finished_at: nowIso() });
      await addOperationLog({
        job_id: jobId,
        level: "error",
        module: "job",
        message: `任务异常（${kind}）`,
        detail: String(error),
      });
    });
  return jobId;
}

export async function getJobById(id: number) {
  return getJob(id);
}

export async function runSynchronously(kind: JobKind, payload: Record<string, unknown>, timeoutMs = 60_000) {
  const { overrides: extraOverrides, ...rest } = payload;
  const bridgePayload = {
    ...rest,
    overrides: mergeOverrides(await buildOverrides(), extraOverrides as Record<string, unknown> | undefined),
  };
  return runBridge(KIND_TO_COMMAND[kind], bridgePayload, { timeoutMs });
}
