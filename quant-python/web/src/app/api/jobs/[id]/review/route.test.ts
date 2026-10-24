import type * as NodeFs from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getJob: vi.fn(),
  listNotesByJob: vi.fn(),
  addNote: vi.fn(),
  addOperationLog: vi.fn(),
  pickChatModel: vi.fn(),
  reviewCandidatesWithFallback: vi.fn(),
  buildHoldingsContext: vi.fn(),
  holdingsFromJobPayload: vi.fn(),
  tradesFromJobPayload: vi.fn(),
  totalCapitalFromJobPayload: vi.fn(),
  historyCompleteFromJobPayload: vi.fn(),
}));

vi.mock("@/lib/db", () => ({
  getJob: mocks.getJob,
  listNotesByJob: mocks.listNotesByJob,
  addNote: mocks.addNote,
  addOperationLog: mocks.addOperationLog,
}));
vi.mock("@/lib/llm", () => ({
  pickChatModel: mocks.pickChatModel,
  reviewCandidatesWithFallback: mocks.reviewCandidatesWithFallback,
}));
vi.mock("@/lib/holdings-context", () => ({
  buildHoldingsContext: mocks.buildHoldingsContext,
  holdingsFromJobPayload: mocks.holdingsFromJobPayload,
  tradesFromJobPayload: mocks.tradesFromJobPayload,
  totalCapitalFromJobPayload: mocks.totalCapitalFromJobPayload,
  historyCompleteFromJobPayload: mocks.historyCompleteFromJobPayload,
}));
vi.mock("node:fs", async () => {
  const actual = await vi.importActual<typeof NodeFs>("node:fs");
  return {
    ...actual,
    default: {
      ...actual,
      existsSync: () => true,
      statSync: () => ({ isFile: () => true }),
      readFileSync: () => '{"candidates":[],"completed_round":true}',
    },
  };
});

import { GET, POST } from "./route";

const scanJob = {
  id: 3,
  kind: "scan",
  status: "success",
  result_path: "scan.json",
  payload: "{}",
};

function params(id = "3") {
  return { params: Promise.resolve({ id }) };
}

describe("candidate review route", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mocks.getJob.mockResolvedValue(scanJob);
    mocks.listNotesByJob.mockResolvedValue([]);
    mocks.addNote.mockResolvedValue(42);
    mocks.addOperationLog.mockResolvedValue(undefined);
    mocks.pickChatModel.mockResolvedValue({ name: "stub" });
    mocks.reviewCandidatesWithFallback.mockResolvedValue({
      content: "## 结论\n覆盖完整\n## 风险\n无\n## 数据缺口\n未发现\n## 复核清单\n- [ ] 复核",
      model: { name: "stub" },
    });
    mocks.buildHoldingsContext.mockReturnValue("");
  });

  it("returns an existing review without calling the model", async () => {
    mocks.listNotesByJob.mockResolvedValue([
      { id: 7, content: "## 结论\n已有", model: "stub", created_at: "2026-10-23" },
    ]);
    const response = await GET(new Request("http://localhost/api/jobs/3/review"), params());
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({
      ok: true,
      note: { id: 7, content: "## 结论\n已有" },
    });
    expect(mocks.reviewCandidatesWithFallback).not.toHaveBeenCalled();
  });

  it("generates and stores a candidate_review note", async () => {
    const response = await POST(new Request("http://localhost/api/jobs/3/review", { method: "POST" }), params());
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({ ok: true, note_id: 42, model: "stub" });
    expect(mocks.listNotesByJob).toHaveBeenCalledWith(3, undefined, "candidate_review");
    expect(mocks.addNote).toHaveBeenCalledWith(
      expect.objectContaining({
        job_id: 3,
        kind: "candidate_review",
        model: "stub",
      })
    );
  });

  it("returns the persisted review instead of calling the model again", async () => {
    mocks.listNotesByJob.mockResolvedValue([
      { id: 7, content: "## 结论\n已有", model: "stub", created_at: "2026-10-23" },
    ]);
    const response = await POST(new Request("http://localhost/api/jobs/3/review", { method: "POST" }), params());
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({
      ok: true,
      note_id: 7,
      content: "## 结论\n已有",
      created_at: "2026-10-23",
    });
    expect(mocks.reviewCandidatesWithFallback).not.toHaveBeenCalled();
  });

  it("regenerates when explicitly requested", async () => {
    mocks.listNotesByJob.mockResolvedValue([
      { id: 7, content: "## 结论\n已有", model: "stub", created_at: "2026-10-23" },
    ]);
    const response = await POST(
      new Request("http://localhost/api/jobs/3/review", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ regenerate: true }),
      }),
      params()
    );
    expect(response.status).toBe(200);
    expect(mocks.reviewCandidatesWithFallback).toHaveBeenCalled();
    expect(mocks.addNote).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "candidate_review" })
    );
  });

  it("accepts scheduled daily-scan jobs", async () => {
    mocks.getJob.mockResolvedValue({ ...scanJob, kind: "daily-scan" });
    const response = await POST(new Request("http://localhost/api/jobs/3/review", { method: "POST" }), params());
    expect(response.status).toBe(200);
    expect(mocks.addNote).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "candidate_review" })
    );
  });

  it("rejects non-scan jobs", async () => {
    mocks.getJob.mockResolvedValue({ ...scanJob, kind: "analyze" });
    const response = await POST(new Request("http://localhost/api/jobs/3/review", { method: "POST" }), params());
    expect(response.status).toBe(422);
    expect(mocks.reviewCandidatesWithFallback).not.toHaveBeenCalled();
  });
});
