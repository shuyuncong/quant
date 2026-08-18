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
  getTotalCapital,
  listHoldings,
  listRecoverableJobs,
  updateJob,
} from "./db";
import { nowIso } from "./time";
import { interpretReport, pickChatModel } from "./llm";
import { signalSystemDir } from "./paths";
import {
  buildHoldingsContext,
  holdingsFromJobPayload,
  totalCapitalFromJobPayload,
} from "./holdings-context";

export type JobKind =
  | "analyze"
  | "scan"
  | "daily-scan"
  | "monitor-once"
  | "monitor-cycle"
  | "test-notify"
  | "dispatch-outbox";

// job kinds that produce a report file worth interpreting
const AUTO_INTERPRET_KINDS: JobKind[] = [
  "analyze",
  "scan",
  "daily-scan",
  "monitor-once",
  "monitor-cycle",
];

// job kinds whose reports may reference the user's holdings; attach them for AI interpretation.
const PORTFOLIO_KINDS: JobKind[] = [
  "analyze",
  "scan",
  "daily-scan",
  "monitor-once",
  "monitor-cycle",
];

function resolveReportPath(resultPath: string): string {
  return path.isAbsolute(resultPath) ? resultPath : path.resolve(signalSystemDir, resultPath);
}

interface InterpretationPayload {
  parent_job_id: number;
  result_path: string;
  notification_at: string;
}

const activeInterpretationJobs = new Set<number>();

/** Read the generated report, call the chat model, persist the note, and enqueue its notification. */
async function autoInterpret(
  interpretationJobId: number,
  parentJobId: number,
  resultPath: string,
  notificationAt: string
): Promise<void> {
  const existingNote = findNoteByJobAndResult(parentJobId, resultPath);
  const profile = existingNote ? null : pickChatModel();
  if (!existingNote && !profile) {
    addOperationLog({
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
    if (!content) {
      const full = resolveReportPath(resultPath);
      const reportText = fs.readFileSync(full, "utf8");
      const parentPayload = getJob(parentJobId)?.payload;
      const holdings = holdingsFromJobPayload(parentPayload);
      const totalCapital = totalCapitalFromJobPayload(parentPayload);
      const context = buildHoldingsContext(reportText, holdings, totalCapital);
      content = await interpretReport(profile!, reportText, context);
      noteId = addNote({ job_id: parentJobId, result_path: resultPath, content, model: profile!.name });
      addOperationLog({
        job_id: interpretationJobId,
        level: "info",
        module: "auto-interpret",
        message: "自动解读完成",
        detail: `模型 ${profile!.name}，笔记 #${noteId}`,
      });
    }
    const pushOutcome = await runBridge(
      "notify-summary",
      {
        title: `AI自动解读 #${parentJobId}`,
        content,
        report_path: resultPath,
        confirmed_at: notificationAt,
        overrides: buildOverrides(),
      },
      { timeoutMs: 120_000 }
    );
    if (!pushOutcome.ok) {
      addOperationLog({
        job_id: interpretationJobId,
        level: "warning",
        module: "auto-interpret",
        message: "AI 解读已保存，但推送失败",
        detail: pushOutcome.error || "未知推送错误",
      });
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    addOperationLog({
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
  updateJob(jobId, { status: "running", started_at: nowIso(), error: null, finished_at: null });
  try {
    await autoInterpret(
      jobId,
      payload.parent_job_id,
      payload.result_path,
      payload.notification_at
    );
    updateJob(jobId, {
      status: "success",
      result_path: payload.result_path,
      finished_at: nowIso(),
    });
  } catch (error) {
    updateJob(jobId, {
      status: "failed",
      error: error instanceof Error ? error.message : String(error),
      finished_at: nowIso(),
    });
  } finally {
    activeInterpretationJobs.delete(jobId);
  }
}

function createInterpretationJob(parentJobId: number, resultPath: string): {
  jobId: number;
  payload: InterpretationPayload;
} {
  const payload: InterpretationPayload = {
    parent_job_id: parentJobId,
    result_path: resultPath,
    notification_at: nowIso().replace(" ", "T"),
  };
  const jobId = createJob("interpret-report", payload);
  return { jobId, payload };
}

export function resumeInterpretationJobs(): void {
  for (const job of listRecoverableJobs("interpret-report")) {
    try {
      const raw = JSON.parse(job.payload) as Partial<InterpretationPayload>;
      if (!raw.parent_job_id || !raw.result_path) throw new Error("解读任务载荷不完整");
      void executeInterpretationJob(job.id, {
        parent_job_id: Number(raw.parent_job_id),
        result_path: String(raw.result_path),
        notification_at: String(raw.notification_at || job.created_at.replace(" ", "T")),
      });
    } catch (error) {
      updateJob(job.id, {
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

/** Create a job row and run the bridge command in the background. Returns the job id immediately. */
export function startJob(kind: JobKind, payload: Record<string, unknown>): number {
  if (PORTFOLIO_KINDS.includes(kind)) {
    // 报告任务统一携带用户持仓与账户总资金，供引擎报告和 AI 解读（含调度器触发的任务）参考。
    payload.holdings = listHoldings();
    payload.total_capital = getTotalCapital();
  }
  const jobId = createJob(kind, payload);
  updateJob(jobId, { status: "running", started_at: nowIso() });
  addOperationLog({
    job_id: jobId,
    level: "info",
    module: "job",
    message: `任务启动（${kind}）`,
    detail: JSON.stringify(payload),
  });
  const { overrides: extraOverrides, ...rest } = payload;
  const bridgePayload = {
    ...rest,
    overrides: mergeOverrides(buildOverrides(), extraOverrides as Record<string, unknown> | undefined),
  };
  const timeoutMs = kind === "test-notify" ? 60_000 : kind === "dispatch-outbox" ? 120_000 : 0;
  runBridge(KIND_TO_COMMAND[kind], bridgePayload, { timeoutMs })
    .then((outcome) => {
      if (outcome.ok) {
        const report = (outcome.data as { report?: Record<string, unknown> } | undefined)?.report;
        const resultPath = typeof report?.output_file === "string" ? report.output_file : null;
        // Persist the child before marking the parent successful.  A process
        // exit between these writes can then be recovered by the scheduler.
        const interpretation =
          resultPath && shouldAutoInterpret(kind, report)
            ? createInterpretationJob(jobId, resultPath)
            : null;
        updateJob(jobId, {
          status: "success",
          result_path: resultPath,
          finished_at: nowIso(),
        });
        addOperationLog({
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
        updateJob(jobId, { status: "failed", error: outcome.error || "执行失败", finished_at: nowIso() });
        addOperationLog({
          job_id: jobId,
          level: "error",
          module: "job",
          message: `任务失败（${kind}）`,
          detail: outcome.error || "执行失败",
        });
      }
    })
    .catch((error) => {
      updateJob(jobId, { status: "failed", error: String(error), finished_at: nowIso() });
      addOperationLog({
        job_id: jobId,
        level: "error",
        module: "job",
        message: `任务异常（${kind}）`,
        detail: String(error),
      });
    });
  return jobId;
}

export function getJobById(id: number) {
  return getJob(id);
}

export async function runSynchronously(kind: JobKind, payload: Record<string, unknown>, timeoutMs = 60_000) {
  const { overrides: extraOverrides, ...rest } = payload;
  const bridgePayload = {
    ...rest,
    overrides: mergeOverrides(buildOverrides(), extraOverrides as Record<string, unknown> | undefined),
  };
  return runBridge(KIND_TO_COMMAND[kind], bridgePayload, { timeoutMs });
}
