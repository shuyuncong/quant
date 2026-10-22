import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  runBridge: vi.fn(),
  getDb: vi.fn(),
  getJob: vi.fn(),
  updateJob: vi.fn(),
  updateJobPayload: vi.fn(),
  addOperationLog: vi.fn(),
  buildOverrides: vi.fn(),
  freezeEngineConfig: vi.fn(),
  createJob: vi.fn(),
}));

vi.mock("../bridge", () => ({ runBridge: mocks.runBridge }));
vi.mock("../db", () => ({
  createJob: mocks.createJob,
  getJob: mocks.getJob,
  getDb: mocks.getDb,
  updateJob: mocks.updateJob,
  updateJobPayload: mocks.updateJobPayload,
  addOperationLog: mocks.addOperationLog,
}));
vi.mock("../config", () => ({
  buildOverrides: mocks.buildOverrides,
  freezeEngineConfig: mocks.freezeEngineConfig,
}));
vi.mock("../paths", () => ({ signalSystemDir: "/tmp/quant-scan-test" }));
vi.mock("../time", () => ({ nowIso: () => "2026-10-22T00:00:00" }));
vi.mock("node:fs/promises", () => ({
  default: { mkdir: vi.fn(), writeFile: vi.fn(), rename: vi.fn() },
}));

import { executeScanBatch, startScanBatch } from "../scan-service";

function job(id: number, strategy: string, notify: boolean) {
  return {
    id,
    status: "pending",
    started_at: null,
    created_at: "2026-10-22 00:00:00",
    payload: JSON.stringify({
      strategies: [strategy],
      notify,
      frozen_overrides: {},
      progress: {},
    }),
  };
}

describe("scan batch notifications", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    const client = {
      query: vi.fn().mockResolvedValue({ rows: [{ acquired: true }] }),
      release: vi.fn(),
    };
    mocks.getDb.mockResolvedValue({
      connect: vi.fn().mockResolvedValue(client),
    });
    mocks.updateJob.mockResolvedValue(undefined);
    mocks.updateJobPayload.mockResolvedValue(undefined);
    mocks.addOperationLog.mockResolvedValue(undefined);
    mocks.buildOverrides.mockResolvedValue({});
    mocks.freezeEngineConfig.mockResolvedValue({});
    mocks.createJob.mockResolvedValue(7);
    mocks.runBridge.mockResolvedValue({
      ok: true,
      data: { report: { completed_round: true, coverage: 1, candidate_count: 1 } },
    });
  });

  it("sends paper-trade candidate notifications for the divergence pool", async () => {
    mocks.getJob.mockResolvedValue(job(1, "macd_divergence", true));

    await executeScanBatch(1);

    const scanCall = mocks.runBridge.mock.calls.find(([command]) => command === "scan");
    expect(scanCall?.[1]).toMatchObject({ scan_kind: "macd_divergence", notify: true });
  });

  it("keeps other pools on the single summary notification", async () => {
    mocks.getJob.mockResolvedValue(job(2, "macd_zero_axis", true));

    await executeScanBatch(2);

    const scanCall = mocks.runBridge.mock.calls.find(([command]) => command === "scan");
    expect(scanCall?.[1]).toMatchObject({ scan_kind: "macd_zero_axis", notify: false });
  });

  it("keeps scheduled full-market scans rule-only", async () => {
    await startScanBatch("daily-scan", { scan_kind: "all", notify: true });

    const payload = mocks.createJob.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(payload.scan_version).toBe(2);
    expect(payload.strategies).toHaveLength(3);
    // 全市场扫描不生成 AI 解读，也就不需要账户快照。
    expect(payload).not.toHaveProperty("portfolio_context");
  });
});
