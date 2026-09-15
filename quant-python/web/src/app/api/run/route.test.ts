import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/jobs", () => ({
  startJob: vi.fn(() => 42),
}));

import { startJob } from "@/lib/jobs";
import { POST } from "./route";

const startJobMock = vi.mocked(startJob);

describe("POST /api/run", () => {
  beforeEach(() => {
    startJobMock.mockClear();
  });

  it("accepts daily-scan for the schedule page manual button", async () => {
    const response = await POST(
      new Request("http://localhost/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "daily-scan", notify: true }),
      })
    );
    expect(response.status).toBe(202);
    const body = (await response.json()) as { ok: boolean; jobId: number };
    expect(body.ok).toBe(true);
    expect(startJobMock).toHaveBeenCalledWith("daily-scan", { notify: true });
  });

  it("rejects unknown job kinds", async () => {
    const response = await POST(
      new Request("http://localhost/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "no-such-kind" }),
      })
    );
    expect(response.status).toBe(422);
    expect(startJobMock).not.toHaveBeenCalled();
  });

  it("requires symbols for analyze", async () => {
    const response = await POST(
      new Request("http://localhost/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "analyze", symbols: [] }),
      })
    );
    expect(response.status).toBe(422);
    expect(startJobMock).not.toHaveBeenCalled();
  });

  it("accepts scan_kind=yearline_pullback and overrides the universe mode", async () => {
    const response = await POST(
      new Request("http://localhost/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: "scan",
          scan_kind: "yearline_pullback",
          universe_mode: "all_a",
          notify: false,
        }),
      })
    );
    expect(response.status).toBe(202);
    expect(startJobMock).toHaveBeenCalledWith("scan", {
      scan_kind: "yearline_pullback",
      notify: false,
      overrides: { scan: { universe_mode: "all_a" } },
    });
  });

  it("defaults scan_kind to macd_zero_axis for old scan requests", async () => {
    const response = await POST(
      new Request("http://localhost/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "scan", notify: true }),
      })
    );
    expect(response.status).toBe(202);
    expect(startJobMock).toHaveBeenCalledWith("scan", {
      scan_kind: "macd_zero_axis",
      notify: true,
    });
  });

  it("rejects unknown scan_kind", async () => {
    const response = await POST(
      new Request("http://localhost/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "scan", scan_kind: "bogus" }),
      })
    );
    expect(response.status).toBe(422);
    expect(startJobMock).not.toHaveBeenCalled();
  });
});
