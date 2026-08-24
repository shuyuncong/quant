import {
  Pool,
  type PoolClient,
  type QueryResult,
  type QueryResultRow,
} from "pg";
import { nowIso } from "./time";
import type {
  AnalysisNote,
  HoldingRow,
  JobRow,
  ModelProfile,
  OperationLog,
  PendingImport,
  PoolRow,
  ScheduleRow,
} from "./types";

export type { ScheduleRow };

const SCHEMA_VERSION = 1;
const SCHEDULER_LOCK_KEY = 1_907_082_026;
const WATCHLIST_LOCK_KEY = 1_907_082_027;

export interface DbClient {
  query<R extends QueryResultRow = QueryResultRow>(
    text: string,
    values?: readonly unknown[],
  ): Promise<QueryResult<R>>;
}

type DatabaseGlobal = typeof globalThis & {
  __quantPool?: Pool;
  __quantSchemaCheck?: Promise<void>;
};

function safeError(error: unknown): Error {
  const raw = error instanceof Error ? error.message : String(error);
  const message = raw.replace(/postgres(?:ql)?:\/\/[^@\s]+@/gi, "postgresql://***@");
  return new Error(message, { cause: error });
}

function createPool(): Pool {
  const connectionString = process.env.DATABASE_URL?.trim();
  if (!connectionString) {
    throw new Error("DATABASE_URL is required. Configure the server-side Supabase PostgreSQL connection string.");
  }
  let hostname = "";
  try {
    hostname = new URL(connectionString).hostname;
  } catch {
    throw new Error("DATABASE_URL is not a valid PostgreSQL URL.");
  }
  const servername = process.env.DATABASE_SSL_SERVERNAME?.trim();
  const local = hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
  const pool = new Pool({
    connectionString,
    max: Math.max(2, Number(process.env.DATABASE_POOL_MAX ?? 5) || 5),
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 15_000,
    ssl: local && !servername
      ? false
      : {
          rejectUnauthorized: process.env.DATABASE_SSL_REJECT_UNAUTHORIZED !== "false",
          ...(servername ? { servername } : {}),
        },
  });
  pool.on("error", (error) => {
    console.error("[database] idle PostgreSQL client error:", safeError(error).message);
  });
  return pool;
}

async function validateSchema(pool: Pool): Promise<void> {
  try {
    const result = await pool.query<{ version: number }>(
      "SELECT version FROM quant.schema_meta ORDER BY version DESC LIMIT 1",
    );
    if (Number(result.rows[0]?.version) !== SCHEMA_VERSION) {
      throw new Error(
        `Unsupported quant database schema version: ${String(result.rows[0]?.version ?? "missing")}. ` +
          "Run npm run db:setup.",
      );
    }
  } catch (error) {
    const code = (error as { code?: string }).code;
    if (code === "3F000" || code === "42P01") {
      throw new Error("Supabase database is not initialized. Run npm run db:setup or npm run db:migrate.");
    }
    throw safeError(error);
  }
}

async function ensureDefaultSchedules(pool: Pool): Promise<void> {
  await pool.query(
    `INSERT INTO quant.schedule (kind, time, interval_seconds, fixed_times, trading_days_only, enabled, updated_at)
     VALUES
       ('daily_scan', '04:00', 60, '[]', TRUE, TRUE, $1),
       ('monitor_cycle', '09:30', 3600, '["10:30","13:30","14:30"]', TRUE, TRUE, $1)
     ON CONFLICT (kind) DO NOTHING`,
    [nowIso()],
  );
}

export async function getDb(): Promise<Pool> {
  const state = globalThis as DatabaseGlobal;
  state.__quantPool ??= createPool();
  if (!state.__quantSchemaCheck) {
    state.__quantSchemaCheck = validateSchema(state.__quantPool)
      .then(() => ensureDefaultSchedules(state.__quantPool!))
      .catch((error) => {
      state.__quantSchemaCheck = undefined;
      throw error;
    });
  }
  await state.__quantSchemaCheck;
  return state.__quantPool;
}

async function resolveDb(db?: DbClient): Promise<DbClient> {
  return db ?? getDb();
}

async function inTransaction<T>(db: DbClient | undefined, work: (client: DbClient) => Promise<T>): Promise<T> {
  if (db) return work(db);
  const pool = await getDb();
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const result = await work(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    await client.query("ROLLBACK").catch(() => undefined);
    throw error;
  } finally {
    client.release();
  }
}

export async function tryAcquireSchedulerLeadership(): Promise<PoolClient | null> {
  const pool = await getDb();
  const client = await pool.connect();
  try {
    const result = await client.query<{ acquired: boolean }>(
      "SELECT pg_try_advisory_lock($1) AS acquired",
      [SCHEDULER_LOCK_KEY],
    );
    if (!result.rows[0]?.acquired) {
      client.release();
      return null;
    }
    return client;
  } catch (error) {
    client.release();
    throw error;
  }
}

// ---------- settings ----------

export async function getSetting(key: string, db?: DbClient): Promise<unknown | null> {
  const client = await resolveDb(db);
  const result = await client.query<{ value: string }>(
    "SELECT value FROM quant.settings WHERE key = $1",
    [key],
  );
  if (!result.rows[0]) return null;
  try {
    return JSON.parse(result.rows[0].value) as unknown;
  } catch {
    return null;
  }
}

export async function setSetting(key: string, value: unknown, db?: DbClient): Promise<void> {
  const client = await resolveDb(db);
  await client.query(
    `INSERT INTO quant.settings (key, value, updated_at) VALUES ($1, $2, $3)
     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at`,
    [key, JSON.stringify(value), nowIso()],
  );
}

export async function deleteSetting(key: string, db?: DbClient): Promise<void> {
  const client = await resolveDb(db);
  await client.query("DELETE FROM quant.settings WHERE key = $1", [key]);
}

export async function getAllSettings(db?: DbClient): Promise<Record<string, unknown>> {
  const client = await resolveDb(db);
  const result = await client.query<{ key: string; value: string }>(
    "SELECT key, value FROM quant.settings ORDER BY key",
  );
  const settings: Record<string, unknown> = {};
  for (const row of result.rows) {
    try {
      settings[row.key] = JSON.parse(row.value) as unknown;
    } catch {
      settings[row.key] = row.value;
    }
  }
  return settings;
}

// ---------- model profiles ----------

function rowToModel(row: Record<string, unknown>): ModelProfile {
  return {
    id: Number(row.id),
    name: String(row.name ?? ""),
    base_url: String(row.base_url ?? ""),
    model: String(row.model ?? ""),
    api_key: String(row.api_key ?? ""),
    env_key: String(row.env_key ?? ""),
    proxy: String(row.proxy ?? ""),
    enabled: Boolean(row.enabled),
    vision_supported: Boolean(row.vision_supported),
    created_at: String(row.created_at ?? ""),
    updated_at: String(row.updated_at ?? ""),
  };
}

export async function listModels(db?: DbClient): Promise<ModelProfile[]> {
  const client = await resolveDb(db);
  const result = await client.query("SELECT * FROM quant.model_profiles ORDER BY id");
  return result.rows.map(rowToModel);
}

export async function getModel(id: number, db?: DbClient): Promise<ModelProfile | null> {
  const client = await resolveDb(db);
  const result = await client.query("SELECT * FROM quant.model_profiles WHERE id = $1", [id]);
  return result.rows[0] ? rowToModel(result.rows[0]) : null;
}

export async function createModel(
  input: {
    name: string;
    base_url: string;
    model: string;
    api_key?: string;
    env_key?: string;
    proxy?: string;
    enabled?: boolean;
    vision_supported?: boolean;
  },
  db?: DbClient,
): Promise<number> {
  const client = await resolveDb(db);
  const now = nowIso();
  const result = await client.query<{ id: string | number }>(
    `INSERT INTO quant.model_profiles
       (name, base_url, model, api_key, env_key, proxy, enabled, vision_supported, created_at, updated_at)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9)
     RETURNING id`,
    [
      input.name,
      input.base_url,
      input.model,
      input.api_key ?? "",
      input.env_key ?? "",
      input.proxy ?? "",
      Boolean(input.enabled),
      input.vision_supported !== false,
      now,
    ],
  );
  return Number(result.rows[0].id);
}

export async function updateModel(
  id: number,
  input: Partial<{
    name: string;
    base_url: string;
    model: string;
    api_key: string;
    env_key: string;
    proxy: string;
    enabled: boolean;
    vision_supported: boolean;
  }>,
  db?: DbClient,
): Promise<void> {
  const allowed = [
    "name",
    "base_url",
    "model",
    "api_key",
    "env_key",
    "proxy",
    "enabled",
    "vision_supported",
  ] as const;
  const fields: string[] = [];
  const values: unknown[] = [];
  for (const key of allowed) {
    if (input[key] === undefined) continue;
    values.push(input[key]);
    fields.push(`${key} = $${values.length}`);
  }
  if (fields.length === 0) return;
  values.push(nowIso(), id);
  const client = await resolveDb(db);
  await client.query(
    `UPDATE quant.model_profiles
     SET ${fields.join(", ")}, updated_at = $${values.length - 1}
     WHERE id = $${values.length}`,
    values,
  );
}

export async function deleteModel(id: number, db?: DbClient): Promise<void> {
  const client = await resolveDb(db);
  await client.query("DELETE FROM quant.model_profiles WHERE id = $1", [id]);
}

// ---------- stock pool ----------

function rowToPool(row: Record<string, unknown>): PoolRow {
  return {
    symbol: String(row.symbol),
    name: String(row.name ?? ""),
    source: String(row.source ?? "manual"),
    created_at: String(row.created_at ?? ""),
  };
}

export async function listPool(db?: DbClient): Promise<PoolRow[]> {
  const client = await resolveDb(db);
  const result = await client.query("SELECT * FROM quant.stock_pool ORDER BY symbol");
  return result.rows.map(rowToPool);
}

export async function syncWatchlistFromPool(db?: DbClient): Promise<string[]> {
  const client = await resolveDb(db);
  const symbols = (await listPool(client)).map((row) => row.symbol);
  await setSetting("monitor.watchlist", symbols, client);
  return symbols;
}

export async function addPoolSymbols(
  items: Array<{ symbol: string; name?: string; source?: string }>,
  db?: DbClient,
): Promise<number> {
  return inTransaction(db, async (client) => {
    await client.query("SELECT pg_advisory_xact_lock($1)", [WATCHLIST_LOCK_KEY]);
    let added = 0;
    const now = nowIso();
    for (const item of items) {
      const symbol = item.symbol?.trim().toUpperCase();
      if (!symbol) continue;
      const result = await client.query(
        `INSERT INTO quant.stock_pool (symbol, name, source, created_at) VALUES ($1, $2, $3, $4)
         ON CONFLICT (symbol) DO UPDATE SET name = EXCLUDED.name, source = EXCLUDED.source`,
        [symbol, item.name ?? "", item.source ?? "manual", now],
      );
      added += result.rowCount ?? 0;
    }
    await syncWatchlistFromPool(client);
    return added;
  });
}

export async function updatePoolSymbol(symbol: string, name: string, db?: DbClient): Promise<void> {
  const client = await resolveDb(db);
  await client.query("UPDATE quant.stock_pool SET name = $1 WHERE symbol = $2", [name, symbol]);
}

export async function removePoolSymbol(symbol: string, db?: DbClient): Promise<void> {
  await inTransaction(db, async (client) => {
    await client.query("SELECT pg_advisory_xact_lock($1)", [WATCHLIST_LOCK_KEY]);
    await client.query("DELETE FROM quant.stock_pool WHERE symbol = $1", [symbol]);
    await syncWatchlistFromPool(client);
  });
}

// ---------- pending imports ----------

function rowToPending(row: Record<string, unknown>): PendingImport {
  return {
    id: Number(row.id),
    kind: String(row.kind) as PendingImport["kind"],
    raw: String(row.raw ?? ""),
    candidates: String(row.candidates ?? "[]"),
    status: String(row.status) as PendingImport["status"],
    created_at: String(row.created_at ?? ""),
  };
}

export async function createPendingImport(
  kind: string,
  raw: string,
  candidates: unknown,
  db?: DbClient,
): Promise<number> {
  const client = await resolveDb(db);
  const result = await client.query<{ id: string | number }>(
    `INSERT INTO quant.pending_imports (kind, raw, candidates, status, created_at)
     VALUES ($1, $2, $3, 'pending', $4) RETURNING id`,
    [kind, raw, JSON.stringify(candidates), nowIso()],
  );
  return Number(result.rows[0].id);
}

export async function listPendingImports(db?: DbClient): Promise<PendingImport[]> {
  const client = await resolveDb(db);
  const result = await client.query(
    "SELECT * FROM quant.pending_imports WHERE status = 'pending' ORDER BY id DESC",
  );
  return result.rows.map(rowToPending);
}

export async function getPendingImport(id: number, db?: DbClient): Promise<PendingImport | null> {
  const client = await resolveDb(db);
  const result = await client.query("SELECT * FROM quant.pending_imports WHERE id = $1", [id]);
  return result.rows[0] ? rowToPending(result.rows[0]) : null;
}

export async function setPendingStatus(
  id: number,
  status: "confirmed" | "cancelled",
  db?: DbClient,
): Promise<void> {
  const client = await resolveDb(db);
  await client.query("UPDATE quant.pending_imports SET status = $1 WHERE id = $2", [status, id]);
}

export async function confirmPendingImport(
  id: number,
  items: Array<{ symbol: string; name?: string; source?: string }>,
  db?: DbClient,
): Promise<number> {
  return inTransaction(db, async (client) => {
    const pending = await client.query(
      "SELECT id FROM quant.pending_imports WHERE id = $1 AND status = 'pending' FOR UPDATE",
      [id],
    );
    if (pending.rowCount !== 1) throw new Error("Pending import is no longer available for confirmation.");
    const added = await addPoolSymbols(items, client);
    await setPendingStatus(id, "confirmed", client);
    return added;
  });
}

// ---------- jobs ----------

function rowToJob(row: Record<string, unknown>): JobRow {
  return {
    id: Number(row.id),
    kind: String(row.kind),
    status: String(row.status) as JobRow["status"],
    payload: String(row.payload ?? "{}"),
    result_path: row.result_path == null ? null : String(row.result_path),
    error: row.error == null ? null : String(row.error),
    created_at: String(row.created_at ?? ""),
    started_at: row.started_at == null ? null : String(row.started_at),
    finished_at: row.finished_at == null ? null : String(row.finished_at),
  };
}

export async function createJob(kind: string, payload: unknown, db?: DbClient): Promise<number> {
  const client = await resolveDb(db);
  const result = await client.query<{ id: string | number }>(
    `INSERT INTO quant.jobs (kind, status, payload, created_at)
     VALUES ($1, 'pending', $2, $3) RETURNING id`,
    [kind, JSON.stringify(payload), nowIso()],
  );
  return Number(result.rows[0].id);
}

export async function updateJob(
  id: number,
  patch: Partial<Pick<JobRow, "status" | "result_path" | "error" | "started_at" | "finished_at">>,
  db?: DbClient,
): Promise<void> {
  const fields: string[] = [];
  const values: unknown[] = [];
  const allowed = ["status", "result_path", "error", "started_at", "finished_at"] as const;
  for (const key of allowed) {
    if (patch[key] === undefined) continue;
    values.push(patch[key]);
    fields.push(`${key} = $${values.length}`);
  }
  if (fields.length === 0) return;
  values.push(id);
  const client = await resolveDb(db);
  await client.query(
    `UPDATE quant.jobs SET ${fields.join(", ")} WHERE id = $${values.length}`,
    values,
  );
}

export async function getJob(id: number, db?: DbClient): Promise<JobRow | null> {
  const client = await resolveDb(db);
  const result = await client.query("SELECT * FROM quant.jobs WHERE id = $1", [id]);
  return result.rows[0] ? rowToJob(result.rows[0]) : null;
}

export interface JobWithNote extends JobRow {
  note: AnalysisNote | null;
}

/** 任务列表并附各自最新的 AI 解读（无解读时为 null）。 */
export async function listJobsWithNote(limit = 100, db?: DbClient): Promise<JobWithNote[]> {
  const client = await resolveDb(db);
  const result = await client.query(
    `SELECT j.*, n.id AS note_id, n.content AS note_content, n.model AS note_model,
            n.created_at AS note_created_at
     FROM quant.jobs j
     LEFT JOIN LATERAL (
       SELECT id, content, model, created_at
       FROM quant.analysis_notes
       WHERE job_id = j.id
       ORDER BY id DESC LIMIT 1
     ) n ON true
     ORDER BY j.id DESC LIMIT $1`,
    [limit],
  );
  return result.rows.map((row) => ({
    ...rowToJob(row),
    note:
      row.note_id == null
        ? null
        : {
            id: Number(row.note_id),
            job_id: Number(row.id),
            symbol: "",
            content: String(row.note_content ?? ""),
            model: String(row.note_model ?? ""),
            result_path: null,
            created_at: String(row.note_created_at ?? ""),
          },
  }));
}

export async function listJobs(limit = 100, db?: DbClient): Promise<JobRow[]> {
  const client = await resolveDb(db);
  const result = await client.query("SELECT * FROM quant.jobs ORDER BY id DESC LIMIT $1", [limit]);
  return result.rows.map(rowToJob);
}

export async function listJobsByKindSince(
  kind: string,
  datePrefix: string,
  db?: DbClient,
): Promise<JobRow[]> {
  const client = await resolveDb(db);
  const result = await client.query(
    "SELECT * FROM quant.jobs WHERE kind = $1 AND created_at LIKE $2 ORDER BY id DESC",
    [kind, `${datePrefix}%`],
  );
  return result.rows.map(rowToJob);
}

export async function listRecoverableJobs(kind: string, db?: DbClient): Promise<JobRow[]> {
  const client = await resolveDb(db);
  const result = await client.query(
    "SELECT * FROM quant.jobs WHERE kind = $1 AND status IN ('pending', 'running') ORDER BY id",
    [kind],
  );
  return result.rows.map(rowToJob);
}

export async function failInterruptedJobs(db?: DbClient): Promise<number> {
  const client = await resolveDb(db);
  const result = await client.query(
    `UPDATE quant.jobs
     SET status = 'failed', error = $1, finished_at = $2
     WHERE status IN ('pending', 'running') AND kind <> 'interpret-report'`,
    ["Web service restarted; the previous process was interrupted and scheduled tasks will retry.", nowIso()],
  );
  return result.rowCount ?? 0;
}

export async function jobsRunningSince(
  datePrefix: string,
  kind: string,
  db?: DbClient,
): Promise<boolean> {
  const client = await resolveDb(db);
  const result = await client.query<{ exists: boolean }>(
    `SELECT EXISTS(
       SELECT 1 FROM quant.jobs WHERE kind = $1 AND status = 'success' AND created_at LIKE $2
     ) AS exists`,
    [kind, `${datePrefix}%`],
  );
  return Boolean(result.rows[0]?.exists);
}

// ---------- analysis notes ----------

function rowToNote(row: Record<string, unknown>): AnalysisNote {
  return {
    id: Number(row.id),
    job_id: row.job_id == null ? null : Number(row.job_id),
    symbol: String(row.symbol ?? ""),
    content: String(row.content ?? ""),
    model: String(row.model ?? ""),
    result_path: row.result_path == null ? null : String(row.result_path),
    created_at: String(row.created_at ?? ""),
  };
}

export async function addNote(
  input: {
    job_id: number | null;
    symbol?: string;
    content: string;
    model?: string;
    result_path?: string | null;
  },
  db?: DbClient,
): Promise<number> {
  const client = await resolveDb(db);
  const result = await client.query<{ id: string | number }>(
    `INSERT INTO quant.analysis_notes
       (job_id, symbol, content, model, result_path, created_at)
     VALUES ($1, $2, $3, $4, $5, $6) RETURNING id`,
    [input.job_id, input.symbol ?? "", input.content, input.model ?? "", input.result_path ?? null, nowIso()],
  );
  return Number(result.rows[0].id);
}

export async function findNoteByJobAndResult(
  jobId: number,
  resultPath: string,
  db?: DbClient,
): Promise<AnalysisNote | null> {
  const client = await resolveDb(db);
  const result = await client.query(
    `SELECT * FROM quant.analysis_notes
     WHERE job_id = $1 AND result_path = $2 ORDER BY id DESC LIMIT 1`,
    [jobId, resultPath],
  );
  return result.rows[0] ? rowToNote(result.rows[0]) : null;
}

export async function listNotesByJob(jobId: number, db?: DbClient): Promise<AnalysisNote[]> {
  const client = await resolveDb(db);
  const result = await client.query(
    "SELECT * FROM quant.analysis_notes WHERE job_id = $1 ORDER BY id DESC",
    [jobId],
  );
  return result.rows.map(rowToNote);
}

// ---------- schedule ----------

function rowToSchedule(row: Record<string, unknown>): ScheduleRow {
  let fixedTimes: string[] = [];
  try {
    const parsed = JSON.parse(String(row.fixed_times ?? "[]"));
    if (Array.isArray(parsed)) fixedTimes = parsed.filter((item) => typeof item === "string");
  } catch {
    fixedTimes = [];
  }
  return {
    id: Number(row.id),
    kind: String(row.kind) as ScheduleRow["kind"],
    time: String(row.time ?? "15:20"),
    interval_seconds: Number(row.interval_seconds ?? 60),
    fixed_times: fixedTimes,
    trading_days_only: Boolean(row.trading_days_only),
    enabled: Boolean(row.enabled),
    updated_at: String(row.updated_at ?? ""),
  };
}

export async function getScheduleRows(db?: DbClient): Promise<ScheduleRow[]> {
  const client = await resolveDb(db);
  const result = await client.query("SELECT * FROM quant.schedule ORDER BY id");
  return result.rows.map(rowToSchedule);
}

export async function upsertScheduleRow(
  kind: ScheduleRow["kind"],
  input: Partial<Omit<ScheduleRow, "id" | "kind" | "updated_at">>,
  db?: DbClient,
): Promise<void> {
  const client = await resolveDb(db);
  await client.query(
    `INSERT INTO quant.schedule
       (kind, time, interval_seconds, fixed_times, trading_days_only, enabled, updated_at)
     VALUES (
       $1,
       COALESCE($2::text, '15:20'),
       COALESCE($3::integer, 60),
       COALESCE($4::text, '[]'),
       COALESCE($5::boolean, TRUE),
       COALESCE($6::boolean, FALSE),
       $7
     )
     ON CONFLICT (kind) DO UPDATE SET
       time = COALESCE($2::text, quant.schedule.time),
       interval_seconds = COALESCE($3::integer, quant.schedule.interval_seconds),
       fixed_times = COALESCE($4::text, quant.schedule.fixed_times),
       trading_days_only = COALESCE($5::boolean, quant.schedule.trading_days_only),
       enabled = COALESCE($6::boolean, quant.schedule.enabled),
       updated_at = $7`,
    [
      kind,
      input.time ?? null,
      input.interval_seconds ?? null,
      input.fixed_times === undefined ? null : JSON.stringify(input.fixed_times),
      input.trading_days_only ?? null,
      input.enabled ?? null,
      nowIso(),
    ],
  );
}

// ---------- operation logs ----------

function rowToOperationLog(row: Record<string, unknown>): OperationLog {
  return {
    id: Number(row.id),
    job_id: row.job_id == null ? null : Number(row.job_id),
    level: String(row.level ?? "info") as OperationLog["level"],
    module: String(row.module ?? ""),
    message: String(row.message ?? ""),
    detail: row.detail == null ? null : String(row.detail),
    created_at: String(row.created_at ?? ""),
  };
}

export async function addOperationLog(
  input: {
    job_id?: number | null;
    level?: OperationLog["level"];
    module?: string;
    message: string;
    detail?: string | null;
  },
  db?: DbClient,
): Promise<number> {
  const client = await resolveDb(db);
  const result = await client.query<{ id: string | number }>(
    `INSERT INTO quant.operation_logs
       (job_id, level, module, message, detail, created_at)
     VALUES ($1, $2, $3, $4, $5, $6) RETURNING id`,
    [input.job_id ?? null, input.level ?? "info", input.module ?? "", input.message, input.detail ?? null, nowIso()],
  );
  return Number(result.rows[0].id);
}

export async function listOperationLogs(
  limit = 300,
  level?: OperationLog["level"],
  db?: DbClient,
): Promise<OperationLog[]> {
  const client = await resolveDb(db);
  const result = level
    ? await client.query(
        "SELECT * FROM quant.operation_logs WHERE level = $1 ORDER BY id DESC LIMIT $2",
        [level, limit],
      )
    : await client.query("SELECT * FROM quant.operation_logs ORDER BY id DESC LIMIT $1", [limit]);
  return result.rows.map(rowToOperationLog);
}

export async function clearOperationLogs(db?: DbClient): Promise<number> {
  const client = await resolveDb(db);
  const result = await client.query("DELETE FROM quant.operation_logs");
  return result.rowCount ?? 0;
}

// ---------- holdings ----------

export const TOTAL_CAPITAL_KEY = "holdings.total_capital";

export async function getTotalCapital(db?: DbClient): Promise<number> {
  const value = Number(await getSetting(TOTAL_CAPITAL_KEY, db));
  return Number.isFinite(value) && value > 0 ? value : 0;
}

export async function setTotalCapital(value: number, db?: DbClient): Promise<void> {
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0) {
    throw new Error("Account total capital must be a finite non-negative number.");
  }
  await setSetting(TOTAL_CAPITAL_KEY, amount > 0 ? amount : 0, db);
}

function rowToHolding(row: Record<string, unknown>): HoldingRow {
  return {
    symbol: String(row.symbol),
    name: String(row.name ?? ""),
    shares: Number(row.shares ?? 0),
    cost_price: Number(row.cost_price ?? 0),
    total_amount: Number(row.total_amount ?? 0),
    created_at: String(row.created_at ?? ""),
    updated_at: String(row.updated_at ?? ""),
  };
}

export async function listHoldings(db?: DbClient): Promise<HoldingRow[]> {
  const client = await resolveDb(db);
  const result = await client.query("SELECT * FROM quant.holdings ORDER BY symbol");
  return result.rows.map(rowToHolding);
}

export async function upsertHolding(
  input: { symbol: string; name?: string; shares?: number; cost_price?: number; total_amount?: number },
  db?: DbClient,
): Promise<HoldingRow> {
  const symbol = input.symbol.trim().toUpperCase();
  const shares = Number(input.shares ?? 0);
  const costPrice = Number(input.cost_price ?? 0);
  let totalAmount = Number(input.total_amount ?? 0);
  if (totalAmount <= 0 && shares > 0 && costPrice > 0) {
    totalAmount = Math.round(shares * costPrice * 100) / 100;
  }
  const now = nowIso();
  const client = await resolveDb(db);
  const result = await client.query(
    `INSERT INTO quant.holdings
       (symbol, name, shares, cost_price, total_amount, created_at, updated_at)
     VALUES ($1, $2, $3, $4, $5, $6, $6)
     ON CONFLICT (symbol) DO UPDATE SET
       name = EXCLUDED.name,
       shares = EXCLUDED.shares,
       cost_price = EXCLUDED.cost_price,
       total_amount = EXCLUDED.total_amount,
       updated_at = EXCLUDED.updated_at
     RETURNING *`,
    [symbol, input.name ?? "", shares, costPrice, totalAmount, now],
  );
  return rowToHolding(result.rows[0]);
}

export async function removeHolding(symbol: string, db?: DbClient): Promise<void> {
  const client = await resolveDb(db);
  await client.query("DELETE FROM quant.holdings WHERE symbol = $1", [symbol.trim().toUpperCase()]);
}
