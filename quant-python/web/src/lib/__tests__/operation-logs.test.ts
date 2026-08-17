import { beforeEach, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { addOperationLog, clearOperationLogs, listOperationLogs, openDb } from "../db";
import { nowIso } from "../time";

describe("operation logs", () => {
  beforeEach(() => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "web-logs-test-"));
    process.env.WEB_DATA_DIR = tempDir;
  });

  it("adds and lists logs newest first", () => {
    addOperationLog({ job_id: 1, level: "info", module: "job", message: "任务启动（analyze）" });
    addOperationLog({
      job_id: 1,
      level: "error",
      module: "auto-interpret",
      message: "自动解读失败",
      detail: "fetch failed",
    });
    const logs = listOperationLogs();
    expect(logs).toHaveLength(2);
    expect(logs[0].message).toBe("自动解读失败");
    expect(logs[0].detail).toBe("fetch failed");
    expect(logs[0].created_at).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });

  it("filters by level and clears", () => {
    addOperationLog({ level: "info", module: "job", message: "a" });
    addOperationLog({ level: "error", module: "job", message: "b" });
    expect(listOperationLogs(100, "error")).toHaveLength(1);
    const cleared = clearOperationLogs();
    expect(cleared).toBe(2);
    expect(listOperationLogs()).toHaveLength(0);
  });
});

describe("nowIso", () => {
  it("returns Beijing wall-clock format", () => {
    expect(nowIso()).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });
});

describe("beijing time migration", () => {
  it("shifts legacy UTC timestamps by +8 hours on open", () => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "web-tz-migrate-"));
    const dbPath = path.join(tempDir, "app.db");
    const raw = new Database(dbPath);
    raw.exec(
      "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, status TEXT, payload TEXT, created_at TEXT, started_at TEXT, finished_at TEXT)"
    );
    raw
      .prepare("INSERT INTO jobs (kind, status, payload, created_at) VALUES ('analyze', 'success', '{}', '2026-08-17 02:00:00')")
      .run();
    raw.close();

    const db = openDb(dbPath);
    const row = db.prepare("SELECT created_at FROM jobs WHERE id = 1").get() as { created_at: string };
    expect(row.created_at).toBe("2026-08-17 10:00:00");
    const flag = db.prepare("SELECT value FROM settings WHERE key = 'meta.beijing_time_migrated'").get() as {
      value: string;
    };
    expect(flag.value).toBe("true");
    db.close();
  });

  it("enables legacy default schedules once for automatic monitoring", () => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "web-schedule-migrate-"));
    const dbPath = path.join(tempDir, "app.db");
    const raw = new Database(dbPath);
    raw.exec(
      "CREATE TABLE schedule (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT UNIQUE, time TEXT, interval_seconds INTEGER, fixed_times TEXT DEFAULT '[]', trading_days_only INTEGER, enabled INTEGER, updated_at TEXT)"
    );
    raw.prepare(
      "INSERT INTO schedule (kind, time, interval_seconds, trading_days_only, enabled, updated_at) VALUES ('daily_scan', '15:20', 60, 1, 0, '2026-08-17 10:00:00')"
    ).run();
    raw.close();

    const db = openDb(dbPath);
    const row = db.prepare("SELECT enabled FROM schedule WHERE kind = 'daily_scan'").get() as { enabled: number };
    expect(row.enabled).toBe(1);
    db.close();
  });
});
