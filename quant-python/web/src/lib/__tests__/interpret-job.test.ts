/** AI 解读子任务：复用已有 note、并且只在父任务要求推送时才推送。 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as NodeFs from "node:fs";

const mocks = vi.hoisted(() => ({
  listRecoverableJobs: vi.fn(),
  getJob: vi.fn(),
  findNoteByJobAndResult: vi.fn(),
  addNote: vi.fn(),
  addOperationLog: vi.fn(),
  updateJob: vi.fn(),
  updateJobPayload: vi.fn(),
  createJob: vi.fn(),
  runBridge: vi.fn(),
  pickChatModel: vi.fn(),
  interpretReportWithFallback: vi.fn(),
  buildOverrides: vi.fn(),
}));

vi.mock("../db", () => ({
  listRecoverableJobs: mocks.listRecoverableJobs,
  getJob: mocks.getJob,
  findNoteByJobAndResult: mocks.findNoteByJobAndResult,
  addNote: mocks.addNote,
  addOperationLog: mocks.addOperationLog,
  updateJob: mocks.updateJob,
  updateJobPayload: mocks.updateJobPayload,
  createJob: mocks.createJob,
}));
vi.mock("../bridge", () => ({ runBridge: mocks.runBridge }));
vi.mock("../llm", () => ({
  pickChatModel: mocks.pickChatModel,
  interpretReportWithFallback: mocks.interpretReportWithFallback,
  extractStandpoints: () => [],
}));
vi.mock("../config", () => ({ buildOverrides: mocks.buildOverrides, freezeEngineConfig: async () => ({}) }));
vi.mock("../paths", () => ({ signalSystemDir: process.cwd() }));
vi.mock("node:fs", async () => {
  const actual = await vi.importActual<typeof NodeFs>("node:fs");
  return { ...actual, default: { ...actual, readFileSync: () => '{"results":[{"symbol":"600036"}]}' } };
});

import { resumeInterpretationJobs } from "../jobs";

function childJob(notify: boolean | undefined) {
  return {
    id: 501,
    created_at: "2026-10-05 15:00:00",
    payload: JSON.stringify({
      parent_job_id: 300,
      result_path: "report.json",
      notification_at: "2026-10-05T15:00:00",
      ...(notify === undefined ? {} : { notify }),
    }),
  };
}

/** 子任务是 void 执行的浮动 Promise：等它把 job 标成 success 再断言，避免竞态。 */
async function waitForCompletion() {
  await vi.waitFor(() => expect(mocks.updateJob).toHaveBeenCalledWith(501, expect.objectContaining({ status: "success" })));
}

describe("AI 解读子任务", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mocks.runBridge.mockResolvedValue({ ok: true, data: {} });
    mocks.addNote.mockResolvedValue(1);
    mocks.updateJob.mockResolvedValue(undefined);
    mocks.addOperationLog.mockResolvedValue(undefined);
    mocks.buildOverrides.mockResolvedValue({});
    mocks.pickChatModel.mockResolvedValue({ name: "stub" });
    mocks.interpretReportWithFallback.mockResolvedValue({ content: "新解读", model: { name: "stub" } });
  });

  it("父任务已写出 note 时复用，不再调用模型", async () => {
    mocks.listRecoverableJobs.mockResolvedValue([childJob(true)]);
    mocks.findNoteByJobAndResult.mockResolvedValue({ id: 7, content: "五页签结论", model: "五页签分析" });
    await resumeInterpretationJobs();
    await waitForCompletion();
    expect(mocks.interpretReportWithFallback).not.toHaveBeenCalled();
    expect(mocks.addNote).not.toHaveBeenCalled();
    expect(mocks.runBridge).toHaveBeenCalledTimes(1);
    expect(mocks.runBridge.mock.calls[0][0]).toBe("notify-summary");
  });

  it("父任务没写 note 时才调用模型并落 note", async () => {
    mocks.listRecoverableJobs.mockResolvedValue([childJob(true)]);
    mocks.findNoteByJobAndResult.mockResolvedValue(null);
    mocks.getJob.mockResolvedValue({ payload: JSON.stringify({ notify: true, portfolio_context: { holdings: [] } }) });
    await resumeInterpretationJobs();
    await waitForCompletion();
    expect(mocks.interpretReportWithFallback).toHaveBeenCalledTimes(1);
    expect(mocks.addNote).toHaveBeenCalledTimes(1);
  });

  it("父任务关闭推送时只落 note，不推送", async () => {
    mocks.listRecoverableJobs.mockResolvedValue([childJob(false)]);
    mocks.findNoteByJobAndResult.mockResolvedValue(null);
    mocks.getJob.mockResolvedValue({ payload: JSON.stringify({ notify: false }) });
    await resumeInterpretationJobs();
    await waitForCompletion();
    expect(mocks.addNote).toHaveBeenCalledTimes(1);
    expect(mocks.runBridge).not.toHaveBeenCalled();
  });

  it("历史子任务没有 notify 字段时保持推送（缺省为 true）", async () => {
    mocks.listRecoverableJobs.mockResolvedValue([childJob(undefined)]);
    mocks.findNoteByJobAndResult.mockResolvedValue({ id: 7, content: "旧解读", model: "illsky" });
    await resumeInterpretationJobs();
    await waitForCompletion();
    expect(mocks.runBridge).toHaveBeenCalledTimes(1);
  });
});
