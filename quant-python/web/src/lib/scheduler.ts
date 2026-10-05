import fs from "node:fs";
import path from "node:path";
import { runBridge } from "./bridge";
import { buildOverrides } from "./config";
import {
  addOperationLog,
  failInterruptedJobs,
  getScheduleRows,
  tryAcquireSchedulerLeadership,
} from "./db";
import type { ScheduleRow } from "./types";
import type { PoolClient } from "pg";
import { resumeInterpretationJobs } from "./jobs";
import { signalSystemDir } from "./paths";
import { nowIso, shanghaiDate, shanghaiHhmm, shanghaiNow } from "./time";

const TICK_MS = 15_000;
const CALENDAR_TTL_MS = 60_000;
const OUTBOX_TICK_MS = 30_000;

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
  const outcome = await runBridge("calendar", { strict: true }, { timeoutMs: 30_000 });
  if (outcome.ok && outcome.data) {
    const data = outcome.data as CalendarInfo;
    calendarCache = { at: now, data };
    return data;
  }
  const current = shanghaiNow();
  return fallbackCalendar(current, calendarCache?.data ?? null);
}

let lastOutboxRunAt = 0;
let outboxRunning = false;

function resolveReportPath(resultPath: string): string {
  return path.isAbsolute(resultPath) ? resultPath : path.resolve(signalSystemDir, resultPath);
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

async function dispatchOutboxIfDue(): Promise<void> {
  if (outboxRunning || Date.now() - lastOutboxRunAt < OUTBOX_TICK_MS) return;
  outboxRunning = true;
  lastOutboxRunAt = Date.now();
  try {
    const overrides = await buildOverrides();
    void runBridge(
      "dispatch-outbox",
      { overrides },
      { timeoutMs: 120_000 }
    ).finally(() => {
      outboxRunning = false;
    });
  } catch (error) {
    outboxRunning = false;
    throw error;
  }
}

export async function estimateNextRun(
  row: ScheduleRow,
  calendar: CalendarInfo,
  now = shanghaiNow()
): Promise<string | null> {
  if (!row.enabled || (row.kind === "monitor_fixed" && !row.fixed_times.length)) return null;
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
    await dispatchOutboxIfDue();
    const calendar = await getCalendar();
    const rows = await getScheduleRows();
    const { runScheduleTick } = await import("./schedule-runs");
    await runScheduleTick(rows, calendar, shanghaiNow());
    const { resumeScanBatches } = await import("./scan-service");
    await resumeScanBatches();
    const { resumeBacktests } = await import("./backtest-service");
    await resumeBacktests();
    const { resumeAnalysisBatches } = await import("./analysis-service");
    await resumeAnalysisBatches();
  } catch (error) {
    console.error("[scheduler]", error);
  }
}

export interface SchedulerStatus {
  rows: ScheduleRow[];
  calendar: CalendarInfo;
  now: string;
  next_runs: Record<string, string | null>;
}

export async function getSchedulerStatus(): Promise<SchedulerStatus> {
  const rows = await getScheduleRows();
  const calendar = await getCalendar();
  const nextRuns: Record<string, string | null> = {};
  for (const row of rows) {
    nextRuns[row.kind] = await estimateNextRun(row, calendar);
  }
  return { rows, calendar, now: nowIso(), next_runs: nextRuns };
}

export async function ensureScheduler(): Promise<void> {
  if (process.env.SCHEDULER_DISABLED) {
    return;
  }
  const globalState = globalThis as typeof globalThis & {
    __webSchedulerStarted?: boolean;
    __webSchedulerLeader?: PoolClient;
    __webSchedulerInterval?: ReturnType<typeof setInterval>;
    __webSchedulerRetry?: ReturnType<typeof setTimeout>;
  };
  if (globalState.__webSchedulerStarted) return;
  const leader = await tryAcquireSchedulerLeadership();
  if (!leader) {
    globalState.__webSchedulerRetry ??= setTimeout(() => {
      globalState.__webSchedulerRetry = undefined;
      void ensureScheduler();
    }, 15_000);
    return;
  }
  globalState.__webSchedulerLeader = leader;
  globalState.__webSchedulerStarted = true;
  leader.once("error", () => {
    if (globalState.__webSchedulerInterval) clearInterval(globalState.__webSchedulerInterval);
    globalState.__webSchedulerInterval = undefined;
    globalState.__webSchedulerLeader = undefined;
    globalState.__webSchedulerStarted = false;
    globalState.__webSchedulerRetry ??= setTimeout(() => {
      globalState.__webSchedulerRetry = undefined;
      void ensureScheduler();
    }, 5_000);
  });
  const interruptedJobs = await failInterruptedJobs();
  if (interruptedJobs > 0) {
    await addOperationLog({
      level: "warning",
      module: "scheduler",
      message: `服务重启后关闭 ${interruptedJobs} 个中断任务`,
      detail: "自动日扫和盘中监控将在下一个可用调度时点补跑",
    });
  }
  await resumeInterpretationJobs();
  const { resumeAnalysisBatches } = await import("./analysis-service");
  await resumeAnalysisBatches();
  globalState.__webSchedulerInterval = setInterval(() => {
    void tick();
  }, TICK_MS);
  void tick();
}
