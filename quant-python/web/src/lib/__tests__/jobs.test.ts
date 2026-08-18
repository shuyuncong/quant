import Database from "better-sqlite3";
import { beforeEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

vi.mock("../bridge", () => ({
  runBridge: vi.fn(() =>
    Promise.resolve({ ok: true, code: 0, data: { report: { output_file: "out/report.json" } } })
  ),
}));

import { failInterruptedJobs, getJob, setTotalCapital, upsertHolding } from "../db";
import { shouldAutoInterpret, startJob } from "../jobs";

describe("job restart recovery", () => {
  it("fails interrupted bridge jobs but preserves recoverable interpretation jobs", () => {
    const db = new Database(":memory:");
    db.exec(`
      CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        result_path TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
      )
    `);
    const insert = db.prepare(
      "INSERT INTO jobs (kind, status, created_at) VALUES (?, ?, '2026-08-17 10:00:00')"
    );
    insert.run("daily-scan", "running");
    insert.run("monitor-cycle", "pending");
    insert.run("interpret-report", "running");

    expect(failInterruptedJobs(db)).toBe(2);
    const rows = db.prepare("SELECT kind, status, error FROM jobs ORDER BY id").all() as Array<{
      kind: string;
      status: string;
      error: string | null;
    }>;
    expect(rows[0]).toMatchObject({ kind: "daily-scan", status: "failed" });
    expect(rows[0].error).toContain("服务重启");
    expect(rows[1]).toMatchObject({ kind: "monitor-cycle", status: "failed" });
    expect(rows[2]).toMatchObject({ kind: "interpret-report", status: "running" });
    db.close();
  });
});

describe("shouldAutoInterpret", () => {
  it("always interprets user-triggered kinds", () => {
    expect(shouldAutoInterpret("analyze", undefined)).toBe(true);
    expect(shouldAutoInterpret("scan", { completed_round: false, new_events: 0 })).toBe(true);
    expect(shouldAutoInterpret("monitor-once", { new_events: 0 })).toBe(true);
  });

  it("interprets daily-scan only after the full round completes", () => {
    expect(shouldAutoInterpret("daily-scan", { completed_round: true, candidate_count: 3 })).toBe(true);
    expect(shouldAutoInterpret("daily-scan", { completed_round: false, candidate_count: 3 })).toBe(false);
    expect(shouldAutoInterpret("daily-scan", { completed_round: true, candidate_count: 0 })).toBe(false);
    expect(shouldAutoInterpret("daily-scan", undefined)).toBe(false);
  });

  it("keeps monitor-cycle interpretation gated on new events", () => {
    expect(shouldAutoInterpret("monitor-cycle", { new_events: 1 })).toBe(true);
    expect(shouldAutoInterpret("monitor-cycle", { new_events: 0 })).toBe(false);
  });
});

describe("startJob portfolio attach", () => {
  beforeEach(() => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "web-jobs-test-"));
    process.env.WEB_DATA_DIR = tempDir;
  });

  it("attaches holdings and total capital to report job payloads", () => {
    upsertHolding({
      symbol: "600036.SH",
      name: "招商银行",
      shares: 1000,
      cost_price: 30,
      total_amount: 0,
    });
    setTotalCapital(100000);
    const jobId = startJob("scan", { notify: true });
    const payload = JSON.parse(getJob(jobId)!.payload) as {
      holdings?: Array<{ symbol: string }>;
      total_capital?: number;
    };
    expect(payload.holdings).toHaveLength(1);
    expect(payload.holdings![0].symbol).toBe("600036.SH");
    expect(payload.total_capital).toBe(100000);
  });

  it("does not attach portfolio to notification-only kinds", () => {
    setTotalCapital(100000);
    const jobId = startJob("test-notify", {});
    const payload = JSON.parse(getJob(jobId)!.payload) as Record<string, unknown>;
    expect(payload.holdings).toBeUndefined();
    expect(payload.total_capital).toBeUndefined();
  });
});
