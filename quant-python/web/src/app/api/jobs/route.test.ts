import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listPool: vi.fn(),
  listJobsWithNote: vi.fn(),
}));
vi.mock("@/lib/db", () => ({
  listPool: mocks.listPool,
  listJobsWithNote: mocks.listJobsWithNote,
}));

import { GET } from "./route";

function job(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    kind: "analyze",
    status: "success",
    payload: JSON.stringify({ symbols: ["600036"] }),
    symbol_names: "",
    result_path: "a.json",
    error: null,
    created_at: "2026-09-02T15:00:00",
    finished_at: "2026-09-02T15:01:00",
    note: null,
    ...overrides,
  };
}

describe("GET /api/jobs symbol_names resolution", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mocks.listPool.mockResolvedValue([]);
    mocks.listJobsWithNote.mockResolvedValue([job()]);
  });

  it("resolves manually entered symbols via payload symbol_names", async () => {
    mocks.listJobsWithNote.mockResolvedValue([
      job({
        payload: JSON.stringify({
          symbols: ["600036"],
          symbol_names: { "600036.SH": "招商银行" },
        }),
      }),
    ]);
    const response = await GET();
    expect(response.status).toBe(200);
    const { jobs } = (await response.json()) as { jobs: Array<{ symbol_names: string }> };
    expect(jobs[0].symbol_names).toBe("招商银行/600036.SH");
  });

  it("falls back to pool names when symbol_names is absent", async () => {
    mocks.listPool.mockResolvedValue([{ symbol: "600036", name: "招商银行" }]);
    const response = await GET();
    const { jobs } = (await response.json()) as { jobs: Array<{ symbol_names: string }> };
    expect(jobs[0].symbol_names).toBe("招商银行/600036.SH");
  });

  it("prefers payload symbol_names over pool names", async () => {
    mocks.listPool.mockResolvedValue([{ symbol: "600036", name: "招商银行" }]);
    mocks.listJobsWithNote.mockResolvedValue([
      job({
        payload: JSON.stringify({
          symbols: ["600036"],
          symbol_names: { "600036.SH": "浦发银行" },
        }),
      }),
    ]);
    const response = await GET();
    const { jobs } = (await response.json()) as { jobs: Array<{ symbol_names: string }> };
    expect(jobs[0].symbol_names).toBe("浦发银行/600036.SH");
  });

  it("shows raw symbol when nothing resolved it", async () => {
    mocks.listJobsWithNote.mockResolvedValue([
      job({
        payload: JSON.stringify({ symbols: ["600036"] }),
      }),
    ]);
    const response = await GET();
    const { jobs } = (await response.json()) as { jobs: Array<{ symbol_names: string }> };
    expect(jobs[0].symbol_names).toBe("600036");
  });

  it("handles unscoped six-digit symbols consistently across sources", async () => {
    mocks.listPool.mockResolvedValue([{ symbol: "000001", name: "平安银行" }]);
    mocks.listJobsWithNote.mockResolvedValue([
      job({
        payload: JSON.stringify({ symbols: ["000001"] }),
      }),
    ]);
    const response = await GET();
    const { jobs } = (await response.json()) as { jobs: Array<{ symbol_names: string }> };
    expect(jobs[0].symbol_names).toBe("平安银行/000001.SZ");
  });

  it("keeps holdings names working as a source", async () => {
    mocks.listJobsWithNote.mockResolvedValue([
      job({
        payload: JSON.stringify({
          symbols: ["600519"],
          holdings: [{ symbol: "600519", name: "贵州茅台" }],
        }),
      }),
    ]);
    const response = await GET();
    const { jobs } = (await response.json()) as { jobs: Array<{ symbol_names: string }> };
    expect(jobs[0].symbol_names).toBe("贵州茅台/600519.SH");
  });

  it("tolerates broken payload JSON", async () => {
    mocks.listJobsWithNote.mockResolvedValue([job({ payload: "{ broken" })]);
    const response = await GET();
    expect(response.status).toBe(200);
  });
});