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
});
