import { runBridge } from "./bridge";
import { getScheduleRows, jobsRunningSince, listJobs } from "./db";
import type { ScheduleRow } from "./types";
import { startJob } from "./jobs";

const TICK_MS = 15_000;
const CALENDAR_TTL_MS = 60_000;

export interface CalendarInfo {
  is_trading_day: boolean;
  is_trading_session: boolean;
  now: string;
}

export function shanghaiNow(): Date {
  // Build a Date whose local fields are Asia/Shanghai wall clock.
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).formatToParts(new Date());
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? "0";
  return new Date(
    Number(get("year")),
    Number(get("month")) - 1,
    Number(get("day")),
    Number(get("hour")),
    Number(get("minute")),
    Number(get("second"))
  );
}

export function shanghaiDate(now = shanghaiNow()): string {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function shanghaiHhmm(now = shanghaiNow()): string {
  return `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
}

let calendarCache: { at: number; data: CalendarInfo } | null = null;

export async function getCalendar(force = false): Promise<CalendarInfo> {
  const now = Date.now();
  if (!force && calendarCache && now - calendarCache.at < CALENDAR_TTL_MS) {
    return calendarCache.data;
  }
  const outcome = await runBridge("calendar", {}, { timeoutMs: 30_000 });
  if (outcome.ok && outcome.data) {
    const data = outcome.data as CalendarInfo;
    calendarCache = { at: now, data };
    return data;
  }
  const current = shanghaiNow();
  const weekday = current.getDay() >= 1 && current.getDay() <= 5;
  const fallback: CalendarInfo = {
    is_trading_day: weekday,
    is_trading_session: false,
    now: current.toISOString(),
  };
  if (calendarCache) return calendarCache.data;
  return fallback;
}

let lastMonitorRunAt = 0;
let lastDailyDate = "";
let lastFixedMonitorRun = "";

export async function estimateNextRun(
  row: ScheduleRow,
  calendar: CalendarInfo,
  now = shanghaiNow()
): Promise<string | null> {
  if (!row.enabled) return null;
  if (row.kind === "daily_scan") {
    const [hour, minute] = row.time.split(":").map(Number);
    const target = new Date(now);
    target.setHours(hour, minute, 0, 0);
    if (target <= now) target.setDate(target.getDate() + 1);
    if (row.trading_days_only) {
      while (target.getDay() === 0 || target.getDay() === 6) target.setDate(target.getDate() + 1);
    }
    return target.toISOString();
  }
  // monitor_cycle
  let intervalEstimate: string | null = null;
  if (calendar.is_trading_session) {
    intervalEstimate = new Date(now.getTime() + row.interval_seconds * 1000).toISOString();
  } else {
    const next = new Date(now);
    if (now.getHours() < 13) {
      next.setHours(13, 0, 0, 0);
    } else {
      next.setDate(next.getDate() + 1);
      next.setHours(9, 30, 0, 0);
    }
    if (row.trading_days_only) {
      while (next.getDay() === 0 || next.getDay() === 6) next.setDate(next.getDate() + 1);
    }
    intervalEstimate = next.toISOString();
  }
  const fixedTimes = Array.isArray(row.fixed_times) ? row.fixed_times : [];
  if (fixedTimes.length > 0) {
    let earliest: Date | null = null;
    for (const fixed of fixedTimes) {
      const [hour, minute] = fixed.split(":").map(Number);
      if (Number.isNaN(hour) || Number.isNaN(minute)) continue;
      const target = new Date(now);
      target.setHours(hour, minute, 0, 0);
      if (target <= now) target.setDate(target.getDate() + 1);
      if (row.trading_days_only) {
        while (target.getDay() === 0 || target.getDay() === 6) target.setDate(target.getDate() + 1);
      }
      if (!earliest || target.getTime() < earliest.getTime()) earliest = target;
    }
    if (earliest) {
      // 配置了固定时点时，优先展示下一个固定时点（间隔执行仍然生效）
      return earliest.toISOString();
    }
  }
  return intervalEstimate;
}

async function tick(): Promise<void> {
  try {
    const calendar = await getCalendar();
    const now = shanghaiNow();
    const today = shanghaiDate(now);
    const hhmm = shanghaiHhmm(now);
    const rows = getScheduleRows();
    for (const row of rows) {
      if (!row.enabled) continue;
      if (row.kind === "daily_scan") {
        const dayOk = !row.trading_days_only || calendar.is_trading_day;
        if (!dayOk) continue;
        const alreadyRan = jobsRunningSince(today, "daily-scan") || lastDailyDate === today;
        if (hhmm >= row.time && !alreadyRan) {
          lastDailyDate = today;
          startJob("daily-scan", { notify: true });
        }
      } else if (row.kind === "monitor_cycle") {
        if (!calendar.is_trading_session) continue;
        const elapsed = Date.now() - lastMonitorRunAt;
        if (elapsed >= row.interval_seconds * 1000) {
          const running = listJobs(20).some(
            (job) => ["monitor-cycle", "monitor-once", "daily-scan", "scan"].includes(job.kind) && job.status === "running"
          );
          if (!running) {
            lastMonitorRunAt = Date.now();
            startJob("monitor-cycle", { notify: true });
          }
        }
        const dayOk = !row.trading_days_only || calendar.is_trading_day;
        if (dayOk) {
          const fixedTimes = Array.isArray(row.fixed_times) ? row.fixed_times : [];
          for (const fixed of fixedTimes) {
            if (fixed === hhmm) {
              const key = `${today}:${fixed}`;
              if (lastFixedMonitorRun === key) continue;
              const running = listJobs(20).some(
                (job) =>
                  ["monitor-cycle", "monitor-once", "daily-scan", "scan"].includes(job.kind) &&
                  job.status === "running"
              );
              if (!running) {
                lastFixedMonitorRun = key;
                startJob("monitor-cycle", { notify: true });
              }
            }
          }
        }
      }
    }
  } catch {
    /* scheduler must never crash the web process */
  }
}

export interface SchedulerStatus {
  rows: ScheduleRow[];
  calendar: CalendarInfo;
  now: string;
  next_runs: Record<string, string | null>;
}

export async function getSchedulerStatus(): Promise<SchedulerStatus> {
  const rows = getScheduleRows();
  const calendar = await getCalendar();
  const nextRuns: Record<string, string | null> = {};
  for (const row of rows) {
    nextRuns[row.kind] = await estimateNextRun(row, calendar);
  }
  return { rows, calendar, now: new Date().toISOString(), next_runs: nextRuns };
}

export function ensureScheduler(): void {
  const globalState = globalThis as typeof globalThis & { __webSchedulerStarted?: boolean };
  if (globalState.__webSchedulerStarted) return;
  globalState.__webSchedulerStarted = true;
  setInterval(() => {
    void tick();
  }, TICK_MS);
  void tick();
}
