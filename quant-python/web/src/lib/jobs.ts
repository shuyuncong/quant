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

/** Create a job row and run the bridge command in the background. Returns the job id immediately. */
export function startJob(kind: JobKind, payload: Record<string, unknown>): number {
  const jobId = createJob(kind, payload);
  updateJob(jobId, { status: "running", started_at: nowIso() });
  const bridgePayload = { ...payload, overrides: buildOverrides() };
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
  const bridgePayload = { ...payload, overrides: buildOverrides() };
  return runBridge(KIND_TO_COMMAND[kind], bridgePayload, { timeoutMs });
}
