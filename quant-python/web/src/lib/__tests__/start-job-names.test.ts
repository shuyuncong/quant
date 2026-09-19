import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  runBridge: vi.fn(),
  listHoldings: vi.fn(),
  getTotalCapital: vi.fn(),
  createJob: vi.fn(),
  updateJob: vi.fn(),
  updateJobPayload: vi.fn(),
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
  updateJobPayload: mocks.updateJobPayload,
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
    mocks.createJob.mockResolvedValue(42);
    mocks.updateJob.mockResolvedValue(undefined);
    mocks.updateJobPayload.mockResolvedValue(undefined);
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

  it("returns before name resolution and writes the names back afterwards", async () => {
    const resolution = Promise.withResolvers<{ ok: boolean; data?: unknown; code: number }>();
    const payloadWrite = Promise.withResolvers<[number, Record<string, unknown>]>();
    mocks.runBridge.mockImplementation((command: string) =>
      command === "resolve-names" ? resolution.promise : bridgeOk({})
    );
    mocks.updateJobPayload.mockImplementation(async (id: number, payload: Record<string, unknown>) => {
      payloadWrite.resolve([id, payload]);
    });

    await startJob("analyze", { symbols: ["600036"] });
    // 名称解析还在飞，任务行已经建好并进入 running，启动接口不必等 Python 子进程
    expect(mocks.createJob).toHaveBeenCalledWith(
      "analyze",
      expect.objectContaining({ symbols: ["600036"] })
    );
    expect(mocks.updateJob).toHaveBeenCalledWith(42, expect.objectContaining({ status: "running" }));
    expect(mocks.updateJobPayload).not.toHaveBeenCalled();

    resolution.resolve({ ok: true, data: { names: { "600036.SH": "招商银行" } }, code: 0 });
    const [jobId, payload] = await payloadWrite.promise;
    expect(jobId).toBe(42);
    expect(payload).toMatchObject({ symbols: ["600036"], symbol_names: { "600036.SH": "招商银行" } });
    expect(mocks.runBridge).toHaveBeenCalledWith(
      "resolve-names",
      { symbols: ["600036"] },
      { timeoutMs: 20_000 }
    );
    // 运行任务仍使用原始载荷的数据,不把解析结果塞给引擎
    const runCall = mocks.runBridge.mock.calls.find(([command]) => command === "analyze");
    expect(runCall?.[1]).toMatchObject({ symbols: ["600036"] });
  });

  it("skips resolution when symbols are absent (pool-driven analyze)", async () => {
    await expect(startJob("analyze", {})).resolves.toBe(42);
    expect(mocks.runBridge).not.toHaveBeenCalledWith(
      "resolve-names",
      expect.anything(),
      expect.anything()
    );
    expect(mocks.updateJobPayload).not.toHaveBeenCalled();
  });

  it("keeps the job running when name resolution fails", async () => {
    mocks.runBridge.mockImplementation((command: string) =>
      command === "resolve-names" ? Promise.reject(new Error("bridge down")) : bridgeOk({})
    );

    await expect(startJob("analyze", { symbols: ["600036"] })).resolves.toBe(42);
    expect(mocks.updateJobPayload).not.toHaveBeenCalled();
    expect(mocks.runBridge).toHaveBeenCalledWith(
      "analyze",
      expect.objectContaining({ symbols: ["600036"] }),
      expect.anything()
    );
  });
});
