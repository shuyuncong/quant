import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ getJob: vi.fn(), existsSync: vi.fn(), statSync: vi.fn(), readFileSync: vi.fn() }));
vi.mock("@/lib/db", () => ({ getJob: mocks.getJob }));
vi.mock("node:fs", () => ({ default: { existsSync: mocks.existsSync, statSync: mocks.statSync, readFileSync: mocks.readFileSync } }));

import { GET } from "./route";

function read(id = "12") {
  return GET(new Request(`http://localhost/api/jobs/${id}/data`), { params: Promise.resolve({ id }) });
}

describe("GET job report data (isolated filesystem and database mocks)", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mocks.getJob.mockResolvedValue({ result_path: "D:/reports/example.json" });
    mocks.existsSync.mockReturnValue(true);
    mocks.statSync.mockReturnValue({ isFile: () => true });
    mocks.readFileSync.mockReturnValue(JSON.stringify({ results: [{ symbol: "000001", timeframes: { "1d": { latest_price: 10, status: "ok" } } }] }));
  });

  it("returns the compact report and only reads the job's saved JSON", async () => {
    const response = await read();
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ results: [{ symbol: "000001", timeframes: [{ timeframe: "1d", latest_price: 10, bars: [], latest_center: null }] }] });
    expect(mocks.getJob).toHaveBeenCalledExactlyOnceWith(12);
    expect(mocks.readFileSync).toHaveBeenCalledExactlyOnceWith("D:/reports/example.json", "utf8");
  });

  it.each(["0", "-1", "1.5", "abc", "NaN", "Infinity"])("rejects invalid id %s before any data access", async (id) => {
    const response = await read(id);
    expect(response.status).toBe(400);
    expect(mocks.getJob).not.toHaveBeenCalled();
    expect(mocks.readFileSync).not.toHaveBeenCalled();
  });

  it("returns 404 for a missing job", async () => {
    mocks.getJob.mockResolvedValue(null);
    expect((await read()).status).toBe(404);
    expect(mocks.existsSync).not.toHaveBeenCalled();
  });

  it("returns 409 when the job has no saved result", async () => {
    mocks.getJob.mockResolvedValue({ result_path: null });
    expect((await read()).status).toBe(409);
    expect(mocks.existsSync).not.toHaveBeenCalled();
  });

  it("returns 404 for missing files without reading", async () => {
    mocks.existsSync.mockReturnValue(false);
    expect((await read()).status).toBe(404);
    expect(mocks.statSync).not.toHaveBeenCalled();
    expect(mocks.readFileSync).not.toHaveBeenCalled();
  });

  it("rejects directories and non-JSON results", async () => {
    mocks.statSync.mockReturnValue({ isFile: () => false });
    expect((await read()).status).toBe(404);
    mocks.getJob.mockResolvedValue({ result_path: "D:/reports/example.pkl" });
    expect((await read()).status).toBe(404);
    expect(mocks.readFileSync).not.toHaveBeenCalled();
  });

  it("returns a controlled error for malformed JSON", async () => {
    mocks.readFileSync.mockReturnValue("{ damaged");
    const response = await read();
    expect(response.status).toBe(500);
    expect((await response.json()).error).toEqual(expect.any(String));
  });

  it("returns a controlled error when reading or looking up a job fails", async () => {
    mocks.readFileSync.mockImplementation(() => { throw new Error("permission denied"); });
    let response = await read();
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: "permission denied" });
    mocks.getJob.mockRejectedValue(new Error("lookup unavailable"));
    response = await read();
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: "lookup unavailable" });
  });
});
