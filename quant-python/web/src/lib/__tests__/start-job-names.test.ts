import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  runBridge: vi.fn(),
  listHoldings: vi.fn(),
  getTotalCapital: vi.fn(),
  createJob: vi.fn(),
  updateJob: vi.fn(),
  addOperationLog: vi.fn(),
  buildOverrides: vi.fn(),
}));

vi.mock("@/lib/bridge", () => ({ runBridge: mocks.runBridge }));
vi.mock("@/lib/config", () => ({ buildOverrides: mocks.buildOverrides }));
vi.mock("@/lib/db", () => ({
  addNote: vi.fn(),
  addOperationLog: mocks.addOperationLog,
  createJob: mocks.createJob,
  findNoteByJobAndResult: vi.fn(),
  getJob: vi.fn(),
  getTotalCapital: mocks.getTotalCapital,
  listHoldings: mocks.listHoldings,
  listRecoverableJobs: vi.fn(),
  updateJob: mocks.updateJob,
}));
vi.mock("@/lib/paths", () => ({
  signalSystemDir: "D:/signal",
  bridgeScript: "D:/signal/web_bridge.py",
  configYaml: "D:/signal/config/config.yaml",
  webDataDir: "D:/web/data",
}));

import { startJob } from "../jobs";

function bridgeOk(data: unknown) {
  return Promise.resolve({ ok: true, data, error: undefined, code: 0 });
}

describe("startJob name resolution for manually entered symbols", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mocks.listHoldings.mockResolvedValue([]);
    mocks.getTotalCapital.mockResolvedValue(0);
    mocks.createJob.mockImplementation(async (_kind: unknown, payload: unknown) => {
      capturedPayload = payload as Record<string, unknown>;
      return 42;
    });
    mocks.updateJob.mockResolvedValue(undefined);
    mocks.addOperationLog.mockResolvedValue(undefined);
    mocks.buildOverrides.mockResolvedValue({});
    // 解析名称成功后,实际的 analyze 运行也走 runBridge;这里模拟任务本身成功
    mocks.runBridge.mockImplementation(async (command: string) => {
      if (command === "resolve-names") {
        return bridgeOk({ names: { "600036.SH": "招商银行" } });
      }
      return bridgeOk({});
    });
  });

  let capturedPayload: Record<string, unknown> | null = null;

  it("persists resolved names into the job payload", async () => {
    await startJob("analyze", { symbols: ["600036"] });
    expect(mocks.runBridge).toHaveBeenCalledWith(
      "resolve-names",
      { symbols: ["600036"] },
      { timeoutMs: 20_000 }
    );
    expect(capturedPayload?.symbol_names).toEqual({ "600036.SH": "招商银行" });
    // 运行任务仍使用原始载荷的数据,不把解析结果塞给引擎
    const runCall = mocks.runBridge.mock.calls.find(([command]) => command === "analyze");
    expect(runCall?.[1]).toMatchObject({ symbols: ["600036"] });
  });

  it("skips resolution when symbols are absent (pool-driven analyze)", async () => {
    await startJob("analyze", {});
    expect(mocks.runBridge).not.toHaveBeenCalledWith(
      expect.stringContaining("resolve-names"),
      expect.anything(),
      expect.anything()
    );
    expect(capturedPayload?.symbol_names).toBeUndefined();
  });

  it("keeps the job running when name resolution fails", async () => {
    mocks.runBridge.mockRejectedValueOnce(new Error("bridge down"));
    await startJob("analyze", { symbols: ["600036"] });
    expect(capturedPayload?.symbol_names).toBeUndefined();
    expect(mocks.createJob).toHaveBeenCalled();
  });
});