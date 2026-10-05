import { describe, expect, it, vi } from "vitest";
vi.mock("../db", () => ({}));
vi.mock("../jobs", () => ({}));
import { dueSlots } from "../schedule-runs";
import type { ScheduleRow } from "../types";
const base: ScheduleRow = { id: 1, kind: "daily_scan", time: "15:20", interval_seconds: 300, fixed_times: [], enabled: true, trading_days_only: true, updated_at: "" };
describe("schedule time slots", () => {
  it("daily scan uses same identity after restart and throughout the day", () => {
    const calendar = { is_trading_day: true, is_trading_session: false };
    expect(dueSlots(base, calendar, new Date(2026,9,9,15,20))[0].key).toBe(dueSlots(base, calendar, new Date(2026,9,9,18,0))[0].key);
  });
  it("fixed analysis can run after market close but marks missed slots", () => {
    const row = { ...base, kind: "monitor_fixed" as const, fixed_times: ["10:30", "15:20"] };
    const slots = dueSlots(row, { is_trading_day: true, is_trading_session: false }, new Date(2026,9,9,15,22));
    expect(slots.map(slot => slot.missed)).toEqual([true,false]);
  });
  it("intervals pause at lunch and all schedules pause on holidays", () => {
    expect(dueSlots({ ...base, kind: "monitor_cycle" }, { is_trading_day: true, is_trading_session: false }, new Date(2026,9,9,12,0))).toEqual([]);
    expect(dueSlots(base, { is_trading_day: false, is_trading_session: false }, new Date(2026,9,1,16,0))).toEqual([]);
  });
});
