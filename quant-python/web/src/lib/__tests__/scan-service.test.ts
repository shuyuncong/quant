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
  portfolioSnapshot: vi.fn(),
}));

vi.mock("../bridge", () => ({ runBridge: mocks.runBridge }));
vi.mock("../db", () => ({
  createJob: vi.fn(),
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
vi.mock("../portfolio", () => ({ portfolioSnapshot: mocks.portfolioSnapshot }));
vi.mock("../paths", () => ({ signalSystemDir: "/tmp/quant-scan-test" }));
vi.mock("../time", () => ({ nowIso: () => "2026-10-22T00:00:00" }));
vi.mock("node:fs/promises", () => ({
  default: { mkdir: vi.fn(), writeFile: vi.fn(), rename: vi.fn() },
}));

import { executeScanBatch } from "../scan-service";

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
    mocks.portfolioSnapshot.mockResolvedValue({});
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
});
