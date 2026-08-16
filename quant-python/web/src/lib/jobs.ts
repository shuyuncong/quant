import { runBridge } from "./bridge";
import { buildOverrides } from "./config";
import { createJob, getJob, updateJob } from "./db";
import { nowIso } from "./db";

export type JobKind =
  | "analyze"
  | "scan"
  | "daily-scan"
  | "monitor-once"
  | "monitor-cycle"
  | "test-notify"
  | "dispatch-outbox";

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

/** Create a job row and run the bridge command in the background. Returns the job id immediately. */
export function startJob(kind: JobKind, payload: Record<string, unknown>): number {
  const jobId = createJob(kind, payload);
  updateJob(jobId, { status: "running", started_at: nowIso() });
  const { overrides: extraOverrides, ...rest } = payload;
  const bridgePayload = {
    ...rest,
    overrides: mergeOverrides(buildOverrides(), extraOverrides as Record<string, unknown> | undefined),
  };
  const timeoutMs = kind === "test-notify" ? 60_000 : kind === "dispatch-outbox" ? 120_000 : 0;
  runBridge(KIND_TO_COMMAND[kind], bridgePayload, { timeoutMs })
    .then((outcome) => {
      if (outcome.ok) {
        const report = (outcome.data as { report?: { output_file?: string } } | undefined)?.report;
        updateJob(jobId, {
          status: "success",
          result_path: report?.output_file ?? null,
          finished_at: nowIso(),
        });
      } else {
        updateJob(jobId, { status: "failed", error: outcome.error || "执行失败", finished_at: nowIso() });
      }
    })
    .catch((error) => {
      updateJob(jobId, { status: "failed", error: String(error), finished_at: nowIso() });
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
