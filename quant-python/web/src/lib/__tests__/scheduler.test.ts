import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { dailyReportState, estimateNextRun, fallbackCalendar } from "../scheduler";
import type { ScheduleRow } from "../types";

function row(overrides: Partial<ScheduleRow>): ScheduleRow {
  return {
    id: 1,
    kind: "daily_scan",
    time: "15:30",
    interval_seconds: 60,
    fixed_times: [],
    trading_days_only: true,
    enabled: true,
    updated_at: "",
    ...overrides,
  };
}

describe("estimateNextRun", () => {
  it("daily scan runs same day when time not reached", async () => {
    const now = new Date(2026, 7, 14, 10, 0, 0); // 2026-08-14 10:00 Friday
    const next = await estimateNextRun(
      row({ time: "15:30" }),
      { is_trading_day: true, is_trading_session: false, now: now.toISOString() },
      now
    );
    expect(next).toBe("2026-08-14 15:30:00");
  });

  it("daily scan skips weekend when trading_days_only", async () => {
    const now = new Date(2026, 7, 14, 16, 0, 0); // Friday after 15:30
    const next = await estimateNextRun(
      row({ time: "15:30" }),
      { is_trading_day: true, is_trading_session: false, now: now.toISOString() },
      now
    );
    expect(next).toBe("2026-08-17 15:30:00"); // Monday
  });

  it("monitor cycle during session uses interval", async () => {
    const now = new Date(2026, 7, 14, 10, 0, 0);
    const next = await estimateNextRun(
      row({ kind: "monitor_cycle", interval_seconds: 300 }),
      { is_trading_day: true, is_trading_session: true, now: now.toISOString() },
      now
    );
    expect(next).toBe("2026-08-14 10:05:00");
  });

  it("monitor cycle with fixed times returns nearest fixed time", async () => {
    const now = new Date(2026, 7, 14, 10, 0, 0);
    const next = await estimateNextRun(
      row({ kind: "monitor_cycle", interval_seconds: 300, fixed_times: ["10:30", "14:30"] }),
      { is_trading_day: true, is_trading_session: true, now: now.toISOString() },
      now
    );
    expect(next).toBe("2026-08-14 10:30:00");
  });

  it("monitor cycle fixed time rolls to next day when passed", async () => {
    const now = new Date(2026, 7, 14, 15, 0, 0);
    const next = await estimateNextRun(
      row({ kind: "monitor_cycle", interval_seconds: 300, fixed_times: ["10:30", "13:30", "14:30"] }),
      { is_trading_day: true, is_trading_session: true, now: now.toISOString() },
      now
    );
    expect(next).toBe("2026-08-17 10:30:00"); // next Monday
  });

  it("disabled row returns null", async () => {
    const now = new Date(2026, 7, 14, 10, 0, 0);
    const next = await estimateNextRun(
      row({ enabled: false }),
      { is_trading_day: true, is_trading_session: false, now: now.toISOString() },
      now
    );
    expect(next).toBeNull();
  });
});

describe("scheduler recovery", () => {
  it("recomputes the session from a same-day cached trading-day result", () => {
    const cached = {
      is_trading_day: true,
      is_trading_session: true,
      now: "2026-08-17T10:00:00+08:00",
    };

    expect(fallbackCalendar(new Date(2026, 7, 17, 11, 31), cached)).toMatchObject({
      is_trading_day: true,
      is_trading_session: false,
    });
    expect(fallbackCalendar(new Date(2026, 7, 18, 10, 0), cached)).toMatchObject({
      is_trading_day: false,
      is_trading_session: false,
    });
  });

  it("treats missing or damaged daily reports as incomplete", () => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "web-scheduler-report-"));
    const damaged = path.join(tempDir, "damaged.json");
    const complete = path.join(tempDir, "complete.json");
    fs.writeFileSync(damaged, "not-json", "utf8");
    fs.writeFileSync(complete, JSON.stringify({ completed_round: true }), "utf8");

    expect(dailyReportState(path.join(tempDir, "missing.json"))).toBe("incomplete");
    expect(dailyReportState(damaged)).toBe("incomplete");
    expect(dailyReportState(complete)).toBe("complete");
  });
});
