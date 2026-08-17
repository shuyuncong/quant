import fs from "node:fs";
import path from "node:path";
import { runBridge } from "./bridge";
import { buildOverrides } from "./config";
import {
  addOperationLog,
  failInterruptedJobs,
  getScheduleRows,
  listJobs,
  listJobsByKindSince,
} from "./db";
import type { ScheduleRow } from "./types";
import { resumeInterpretationJobs, startJob } from "./jobs";
import { signalSystemDir } from "./paths";
import { nowIso, shanghaiDate, shanghaiHhmm, shanghaiNow } from "./time";

const TICK_MS = 15_000;
const CALENDAR_TTL_MS = 60_000;
const OUTBOX_TICK_MS = 30_000;
const DAILY_BATCH_COOLDOWN_MS = 30_000;

export interface CalendarInfo {
  is_trading_day: boolean;
  is_trading_session: boolean;
  now: string;
}

let calendarCache: { at: number; data: CalendarInfo } | null = null;

function isTradingSessionAt(current: Date): boolean {
  const hhmm = shanghaiHhmm(current);
  return (hhmm >= "09:30" && hhmm <= "11:30") || (hhmm >= "13:00" && hhmm <= "15:00");
}

export function fallbackCalendar(
  current: Date,
  cached: CalendarInfo | null = null
): CalendarInfo {
  const today = shanghaiDate(current);
  const cacheMatchesToday = cached?.now.slice(0, 10) === today;
  const isTradingDay = Boolean(cacheMatchesToday && cached?.is_trading_day);
  return {
    is_trading_day: isTradingDay,
    is_trading_session: isTradingDay && isTradingSessionAt(current),
    now: `${nowIso(current).replace(" ", "T")}+08:00`,
  };
}

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
  return fallbackCalendar(current, calendarCache?.data ?? null);
}

let lastMonitorRunAt = 0;
let lastDailyStartAt = 0;
let lastFixedMonitorRun = "";
let lastOutboxRunAt = 0;
let outboxRunning = false;

function fixedMonitorAlreadyScheduled(today: string, fixed: string): boolean {
  return listJobsByKindSince("monitor-cycle", today).some((job) =>
    job.created_at.slice(0, 16) === `${today} ${fixed}`
  );
}

function resolveReportPath(resultPath: string): string {
  return path.isAbsolute(resultPath) ? resultPath : path.resolve(signalSystemDir, resultPath);
}

function dailyScanState(today: string): "none" | "running" | "incomplete" | "complete" {
  const jobs = listJobsByKindSince("daily-scan", today);
  if (jobs.some((job) => job.status === "pending" || job.status === "running")) return "running";
  const latestSuccess = jobs.find((job) => job.status === "success");
  if (!latestSuccess) return jobs.length ? "incomplete" : "none";
  if (!latestSuccess.result_path) return "incomplete";
  return dailyReportState(latestSuccess.result_path);
}

export function dailyReportState(resultPath: string): "incomplete" | "complete" {
  try {
    const report = JSON.parse(fs.readFileSync(resolveReportPath(resultPath), "utf8")) as {
      completed_round?: boolean;
    };
    return report.completed_round === true ? "complete" : "incomplete";
  } catch {
    return "incomplete";
  }
}

function dispatchOutboxIfDue(): void {
  if (outboxRunning || Date.now() - lastOutboxRunAt < OUTBOX_TICK_MS) return;
  outboxRunning = true;
  lastOutboxRunAt = Date.now();
  void runBridge(
    "dispatch-outbox",
    { overrides: buildOverrides() },
    { timeoutMs: 120_000 }
  ).finally(() => {
    outboxRunning = false;
  });
}

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
    return nowIso(target);
  }
  // monitor_cycle
  let intervalEstimate: string | null = null;
  if (calendar.is_trading_session) {
    intervalEstimate = nowIso(new Date(now.getTime() + row.interval_seconds * 1000));
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
    intervalEstimate = nowIso(next);
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
      // 配置了固定时点时，按固定时点模式展示下一个执行时点
      return nowIso(earliest);
    }
  }
  return intervalEstimate;
}

async function tick(): Promise<void> {
  try {
    dispatchOutboxIfDue();
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
        const state = dailyScanState(today);
        if (
          hhmm >= row.time &&
          state !== "running" &&
          state !== "complete" &&
          Date.now() - lastDailyStartAt >= DAILY_BATCH_COOLDOWN_MS
        ) {
          lastDailyStartAt = Date.now();
          startJob("daily-scan", { notify: true });
        }
      } else if (row.kind === "monitor_cycle") {
        if (!calendar.is_trading_session) continue;
        const fixedTimes = Array.isArray(row.fixed_times) ? row.fixed_times : [];
        if (fixedTimes.length > 0) {
          // 固定时点模式：只在勾选的时点各执行一次，不再按间隔执行
          const dayOk = !row.trading_days_only || calendar.is_trading_day;
          if (!dayOk) continue;
          for (const fixed of fixedTimes) {
            if (fixed === hhmm) {
              const key = `${today}:${fixed}`;
              if (lastFixedMonitorRun === key || fixedMonitorAlreadyScheduled(today, fixed)) continue;
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
        } else {
          // 间隔模式：交易时段内按 interval_seconds 周期执行
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
  return { rows, calendar, now: nowIso(), next_runs: nextRuns };
}

export function ensureScheduler(): void {
  const globalState = globalThis as typeof globalThis & { __webSchedulerStarted?: boolean };
  if (globalState.__webSchedulerStarted) return;
  globalState.__webSchedulerStarted = true;
  const interruptedJobs = failInterruptedJobs();
  if (interruptedJobs > 0) {
    addOperationLog({
      level: "warning",
      module: "scheduler",
      message: `服务重启后关闭 ${interruptedJobs} 个中断任务`,
      detail: "自动日扫和盘中监控将在下一个可用调度时点补跑",
    });
  }
  resumeInterpretationJobs();
  setInterval(() => {
    void tick();
  }, TICK_MS);
  void tick();
}
