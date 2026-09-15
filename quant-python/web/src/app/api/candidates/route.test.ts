import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/bridge", () => ({
  runBridge: vi.fn(() =>
    Promise.resolve({ ok: true, data: { candidates: [] }, code: 0 })
  ),
}));

import { runBridge } from "@/lib/bridge";
import { GET } from "./route";

const runBridgeMock = vi.mocked(runBridge);

describe("GET /api/candidates", () => {
  beforeEach(() => {
    runBridgeMock.mockClear();
    runBridgeMock.mockResolvedValue({ ok: true, data: { candidates: [] }, code: 0 });
  });

  it("defaults to the MACD pool when pool_type is omitted (old behavior)", async () => {
    const response = await GET(new Request("http://localhost/api/candidates"));
    expect(response.status).toBe(200);
    expect(runBridgeMock).toHaveBeenCalledWith(
      "candidates",
      { pool_type: "macd_zero_axis" },
      expect.anything()
    );
  });

  it("passes pool_type=yearline_pullback through to the bridge", async () => {
    const response = await GET(
      new Request("http://localhost/api/candidates?pool_type=yearline_pullback")
    );
    expect(response.status).toBe(200);
    expect(runBridgeMock).toHaveBeenCalledWith(
      "candidates",
      { pool_type: "yearline_pullback" },
      expect.anything()
    );
  });

  it("passes pool_type=all through to the bridge", async () => {
    const response = await GET(
      new Request("http://localhost/api/candidates?pool_type=all")
    );
    expect(response.status).toBe(200);
    expect(runBridgeMock).toHaveBeenCalledWith(
      "candidates",
      { pool_type: "all" },
      expect.anything()
    );
  });

  it("rejects unknown pool_type with 422", async () => {
    const response = await GET(
      new Request("http://localhost/api/candidates?pool_type=bogus")
    );
    expect(response.status).toBe(422);
    expect(runBridgeMock).not.toHaveBeenCalled();
  });

  it("returns 500 when the bridge call fails", async () => {
    runBridgeMock.mockResolvedValue({ ok: false, error: "boom", code: 1 });
    const response = await GET(new Request("http://localhost/api/candidates"));
    expect(response.status).toBe(500);
    const body = (await response.json()) as { error: string };
    expect(body.error).toContain("boom");
  });
});
