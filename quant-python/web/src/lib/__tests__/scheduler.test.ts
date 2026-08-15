import { describe, expect, it } from "vitest";
import { estimateNextRun } from "../scheduler";
import type { ScheduleRow } from "../types";

function row(overrides: Partial<ScheduleRow>): ScheduleRow {
  return {
    id: 1,
    kind: "daily_scan",
    time: "15:30",
    interval_seconds: 60,
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
    expect(next).toBe(new Date(2026, 7, 14, 15, 30, 0).toISOString());
  });

  it("daily scan skips weekend when trading_days_only", async () => {
    const now = new Date(2026, 7, 14, 16, 0, 0); // Friday after 15:30
    const next = await estimateNextRun(
      row({ time: "15:30" }),
      { is_trading_day: true, is_trading_session: false, now: now.toISOString() },
      now
    );
    expect(next).toBe(new Date(2026, 7, 17, 15, 30, 0).toISOString()); // Monday
  });

  it("monitor cycle during session uses interval", async () => {
    const now = new Date(2026, 7, 14, 10, 0, 0);
    const next = await estimateNextRun(
      row({ kind: "monitor_cycle", interval_seconds: 300 }),
      { is_trading_day: true, is_trading_session: true, now: now.toISOString() },
      now
    );
    expect(next).toBe(new Date(2026, 7, 14, 10, 5, 0).toISOString());
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
