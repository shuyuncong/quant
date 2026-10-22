import fs from "node:fs";
import path from "node:path";
import { runBridge } from "./bridge";
import { buildOverrides } from "./config";
import {
  addNote,
  addOperationLog,
  createJob,
  findNoteByJobAndResult,
  getDb,
  getJob,
  listRecoverableJobs,
  updateJob,
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

// 全市场扫描只落候选池和汇总通知，不调用 LLM；个股分析的 AI 结论由
// analysis-service 自己生成。历史遗留的 interpret-report 子任务仍由
// resumeInterpretationJobs 恢复执行，因此这里不能再创建新的自动解读。

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

/**
 * 后台解析标的名称并回写任务 payload，供「个股分析记录」列表回填显示名。
 *
 * 两个关键细节：
 *   1. **必须用 SQL 端合并**（`payload::jsonb || $2::jsonb`）而不是把整个 payload 覆盖回去。
 *      分析/扫描批次会并发地往同一个 payload 里写 `progress`（见 analysis-service /
 *      scan-service），整体覆盖会把并发的进度写丢掉。
 *   2. 不传 `payload.symbols` 时改为读取任务当前的标的目标，避免调用方还要自己取一遍。
 *
 * 名称解析失败只影响列表展示，绝不影响任务执行 —— 列表会回退到代码本身。
 */
export async function persistSymbolNames(jobId: number, symbols?: string[]): Promise<void> {
  try {
    let targets = symbols;
    if (!targets || targets.length === 0) {
      const job = await getJob(jobId);
      if (!job) return;
      const parsed = JSON.parse(job.payload) as Record<string, unknown>;
      targets = Array.isArray(parsed.symbols) ? parsed.symbols.map(String) : [];
    }
    if (targets.length === 0) return;
    const outcome = await runBridge("resolve-names", { symbols: targets }, { timeoutMs: 20_000 });
    const data = outcome.data;
    if (!outcome.ok || !data || typeof data !== "object" || !("names" in data)) return;
    const names = data.names;
    if (!names || typeof names !== "object" || Array.isArray(names)) return;
    await (await getDb()).query(
      "UPDATE quant.jobs SET payload=(payload::jsonb || $2::jsonb)::text WHERE id=$1",
      [jobId, JSON.stringify({ symbol_names: names })],
    );
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
