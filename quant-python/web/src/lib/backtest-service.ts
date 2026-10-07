import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { createJob, getJob, updateJob } from "./db";
import { freezeEngineConfig } from "./config";
import type { PoolClient } from "pg";
import { runBridge } from "./bridge";
import { signalSystemDir, resolvePathWithin } from "./paths";
import { normalizeSymbol } from "./symbols";
import type { JobRow } from "./types";
import { nowIso, shanghaiDate } from "./time";

/** Thrown for client-fixable input problems; the API maps it to 422 rather than 500. */
export class BacktestValidationError extends Error {}

/** Backtest jobs carry this version; older rows are local-era tasks that must not be silently rerun. */
export const BACKTEST_VERSION = 2;
export const BACKTEST_DISABLED_MESSAGE = "线上回测未启用（BACKTEST_ENABLED=1）";
export const LEGACY_BACKTEST_MESSAGE = "旧版本地回测任务，请新建线上回测";

export interface BacktestOptions {
  mode: "stock" | "market";
  start: string;
  end: string;
  initial_cash: number;
  max_positions: number;
  position_size_pct: number;
  symbols: string[];
  output_dir: string;
}

export function backtestAvailable(): boolean {
  return process.env.BACKTEST_ENABLED === "1";
}

/** User-facing reason a new run is refused; history stays readable either way. */
export function backtestServiceUnavailable(): string {
  return BACKTEST_DISABLED_MESSAGE;
}

/** Strict calendar validation: rejects 2024-02-30 and other shape-valid but nonexistent dates. */
function isRealDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day;
}

/**
 * Validate a client backtest request. Throws with a user-facing message; the caller maps every
 * thrown error to 422, so this must never be used for internal failures.
 */
export function validateBacktestOptions(input: Record<string, unknown>): BacktestOptions {
  try {
    return parseBacktestOptions(input);
  } catch (error) {
    throw new BacktestValidationError(error instanceof Error ? error.message : String(error));
  }
}

function parseBacktestOptions(input: Record<string, unknown>): BacktestOptions {
  if (input.strategies !== undefined) throw new Error("回测固定比较全部三策略，请勿传 strategies");
  const mode = input.mode;
  if (mode !== "stock" && mode !== "market") throw new Error("请选择个股或全市场回测");
  const start = String(input.start ?? "");
  const end = String(input.end ?? "");
  if (!isRealDate(start) || !isRealDate(end)) throw new Error("请选择有效的起止日期（YYYY-MM-DD）");
  if (start >= end) throw new Error("结束日期必须晚于开始日期");
  if (end > shanghaiDate()) throw new Error("结束日期不能在未来");
  const initial_cash = Number(input.initial_cash ?? 100000);
  const max_positions = Number(input.max_positions ?? 4);
  const position_size_pct = Number(input.position_size_pct ?? 0.25);
  if (!Number.isFinite(initial_cash) || initial_cash < 1000 || initial_cash > 1e10) throw new Error("初始资金应为 1000 至 100 亿");
  if (!Number.isInteger(max_positions) || max_positions < 1 || max_positions > 100) throw new Error("最大持仓数量应为 1-100");
  if (!Number.isFinite(position_size_pct) || position_size_pct <= 0 || position_size_pct > 1) throw new Error("单笔仓位比例应为 0-100%");
  const requested = Array.isArray(input.symbols) ? input.symbols.map((value) => String(value).trim()).filter(Boolean) : [];
  if (mode === "stock" && requested.length !== 1) throw new Error("个股回测需要且只需一个股票代码");
  if (mode === "market" && requested.length) throw new Error("全市场回测不接受股票代码");
  const symbols = requested.map((value) => normalizeSymbol(value));
  if (symbols.some((symbol) => !/^\d{6}\.(SH|SZ|BJ)$/.test(symbol))) throw new Error("股票代码不合法");
  return { mode, start, end, initial_cash, max_positions, position_size_pct, symbols, output_dir: "" };
}

/**
 * Freeze rules, create the job row, and return its id. The worker (not this process) runs the
 * replay, so the HTTP request never waits on market data.
 */
export async function startBacktest(input: Record<string, unknown>): Promise<number> {
  const options: BacktestOptions = {
    ...validateBacktestOptions(input),
    output_dir: path.join(signalSystemDir, "output", "backtests", randomUUID()),
  };
  const overrides = await freezeEngineConfig();
  return createJob("backtest", { options, overrides, backtest_version: BACKTEST_VERSION });
}

export interface PublicBacktestJob {
  id: number;
  status: string;
  created_at: string;
  error: string | null;
  options: Record<string, unknown> | null;
}

const PUBLIC_OPTION_KEYS = ["mode", "start", "end", "initial_cash", "max_positions", "position_size_pct", "symbols"] as const;

/**
 * Project one job row for the client: the frozen rule snapshot and the server-side
 * output directory stay internal, only the request that produced the job is echoed.
 */
export function resolvePublicBacktestJob(row: Record<string, unknown> | JobRow): PublicBacktestJob {
  return {
    id: Number(row.id),
    status: String(row.status),
    created_at: String(row.created_at ?? ""),
    error: row.error == null ? null : String(row.error),
    options: publicOptions(row.payload),
  };
}

function publicOptions(payload: unknown): Record<string, unknown> | null {
  if (typeof payload !== "string") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const options = Reflect.get(parsed, "options");
  if (!options || typeof options !== "object") return null;
  const projected: Record<string, unknown> = {};
  for (const key of PUBLIC_OPTION_KEYS) {
    const value = Reflect.get(options, key);
    if (value !== undefined) projected[key] = value;
  }
  return projected;
}

/** Old local-era rows cannot be replayed with the online input format. */
export function isLegacyBacktestPayload(payload: unknown): boolean {
  if (typeof payload !== "string") return true;
  try {
    const parsed: unknown = JSON.parse(payload);
    return parsed === null || typeof parsed !== "object" || Number(Reflect.get(parsed, "backtest_version")) !== BACKTEST_VERSION;
  } catch {
    return true;
  }
}

/** Bridge results cross a process boundary, so the report path is checked rather than asserted. */
export function outputFileFrom(data: unknown): string {
  if (data && typeof data === "object" && "report" in data) {
    const report = data.report;
    if (report && typeof report === "object" && "output_file" in report && typeof report.output_file === "string") {
      return report.output_file;
    }
  }
  throw new Error("回测未返回报告路径");
}

function bridgeEnvironment(): Record<string, string> {
  // Keep each replay single-threaded so one worker cannot monopolise the host's cores.
  return { OMP_NUM_THREADS: "1", OPENBLAS_NUM_THREADS: "1", MKL_NUM_THREADS: "1" };
}

/**
 * Run one queued backtest to completion. Only the worker calls this, and all job state is read and
 * written on the caller's advisory-lock client so a second worker cannot interleave updates.
 */
export async function executeBacktest(id: number, client: PoolClient, signal?: AbortSignal): Promise<void> {
  const claimed = await client.query(
    "UPDATE quant.jobs SET status='running', started_at=COALESCE(started_at, $2), error=NULL WHERE id=$1 AND kind='backtest' AND status='pending' RETURNING id",
    [id, nowIso()]
  );
  if (!claimed.rowCount) return;
  try {
    const job = await getJob(id, client);
    if (!job) throw new Error("回测任务不存在");
    const payload = JSON.parse(job.payload) as { overrides?: Record<string, unknown>; options: Record<string, unknown> };
    const outcome = await runBridge("research-backtest", payload, { env: bridgeEnvironment(), signal });
    if (!outcome.ok) throw new Error(outcome.error ?? "回测失败");
    await updateJob(id, { status: "success", result_path: outputFileFrom(outcome.data), finished_at: nowIso() }, client);
  } catch (error) {
    // An aborted replay stays running so the next worker can resume it from frozen inputs.
    if (signal?.aborted) return;
    await updateJob(id, {
      status: "failed",
      error: error instanceof Error ? error.message : String(error),
      finished_at: nowIso(),
    }, client);
  }
}

export async function readBacktestFile(file: string) {
  const resolved = resolvePathWithin(path.join(signalSystemDir, "output", "backtests"), file);
  if (!resolved) throw new Error("回测文件路径无效");
  return JSON.parse(await fs.readFile(resolved, "utf8"));
}
