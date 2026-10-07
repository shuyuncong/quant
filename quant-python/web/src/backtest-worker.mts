/**
 * Standalone consumer for queued backtest jobs.
 *
 * Runs as its own container process so a long market-wide replay never blocks
 * the web server and never competes for the analysis/scan advisory lock.  One
 * worker at a time holds advisory lock 920005; extra replicas stay idle.
 *
 * Start from the web directory: `npm run backtest:worker`.
 */
import process from "node:process";
import nextEnv from "@next/env";
import { getDb } from "@/lib/db";
import { nowIso } from "@/lib/time";
import { executeBacktest, backtestAvailable, BACKTEST_VERSION, BACKTEST_DISABLED_MESSAGE, LEGACY_BACKTEST_MESSAGE } from "@/lib/backtest-service";
import { assertLocalDevelopmentDatabase } from "../scripts/db-safety.mjs";

const LOCK_KEY = 920005;
const IDLE_SLEEP_MS = 5000;
const LOG = "[backtest-worker]";

const { loadEnvConfig } = nextEnv;
loadEnvConfig(process.cwd(), process.env.NODE_ENV !== "production");

if (!backtestAvailable()) {
  // Enabled by updating the environment and restarting; idle workers never touch the database.
  console.log(`${LOG} ${BACKTEST_DISABLED_MESSAGE}；worker 保持空闲`);
  await new Promise(() => {});
}

if (process.env.NODE_ENV !== "production") {
  // Development must never point the worker at a production database.
  assertLocalDevelopmentDatabase(process.env.DATABASE_URL ?? "");
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

let stopping = false;
let current: AbortController | null = null;
const stop = (signal: string) => {
  if (stopping) return;
  stopping = true;
  console.log(`${LOG} 收到 ${signal}，停止取任务并取消当前回测`);
  if (current) {
    // The running replay keeps its frozen inputs; the next worker resumes it.
    current.abort();
  } else {
    process.exit(0);
  }
};
process.on("SIGINT", () => stop("SIGINT"));
process.on("SIGTERM", () => stop("SIGTERM"));

const pool = await getDb();
const client = await pool.connect();

let holding = false;
while (!holding && !stopping) {
  const { rows } = await client.query("SELECT pg_try_advisory_lock($1) AS acquired", [LOCK_KEY]);
  holding = rows[0]?.acquired === true;
  if (!holding) {
    console.log(`${LOG} 另一个 worker 持有回测锁，5 秒后重试`);
    await sleep(IDLE_SLEEP_MS);
  }
}

if (holding && !stopping) {
  // A replay interrupted by a restart keeps its frozen inputs, so it is safe to requeue.
  const recovered = await client.query(
    "UPDATE quant.jobs SET status='pending' WHERE kind='backtest' AND status='running' AND payload ~ ('\"backtest_version\"\\s*:\\s*' || $1) RETURNING id",
    [String(BACKTEST_VERSION)]
  );
  // Local-era rows cannot be replayed with the online input format.
  const legacy = await client.query(
    "UPDATE quant.jobs SET status='failed', error=$2, finished_at=$3 WHERE kind='backtest' AND status IN ('pending','running','failed') AND payload !~ ('\"backtest_version\"\\s*:\\s*' || $1) RETURNING id",
    [String(BACKTEST_VERSION), LEGACY_BACKTEST_MESSAGE, nowIso()]
  );
  console.log(`${LOG} 已启动：恢复 ${recovered.rowCount} 个运行中任务，标记 ${legacy.rowCount} 个旧版本任务失败`);
}

// The lock lives on this connection: losing it must stop the worker rather than let it write stale state.
let connectionLost = false;
const onConnectionLost = (error: unknown) => {
  if (connectionLost) return;
  connectionLost = true;
  console.error(`${LOG} 持锁连接中断，退出：`, error);
  stop("connection-loss");
};
client.on("error", onConnectionLost);
client.on("end", () => onConnectionLost("connection ended"));

while (holding && !stopping && !connectionLost) {
  const { rows } = await client.query(
    "SELECT id FROM quant.jobs WHERE kind='backtest' AND status='pending' AND payload ~ ('\"backtest_version\"\\s*:\\s*' || $1) ORDER BY id LIMIT 1",
    [String(BACKTEST_VERSION)]
  );
  if (!rows.length) {
    await sleep(IDLE_SLEEP_MS);
    continue;
  }
  const controller = new AbortController();
  current = controller;
  try {
    await executeBacktest(Number(rows[0].id), client, controller.signal);
  } catch (error) {
    console.error(`${LOG} 任务 ${rows[0].id} 执行异常：`, error);
  } finally {
    current = null;
  }
}

if (holding) {
  await client.query("SELECT pg_advisory_unlock($1)", [LOCK_KEY]).catch(() => undefined);
}
client.release();
await pool.end().catch(() => undefined);
console.log(`${LOG} 已退出`);
