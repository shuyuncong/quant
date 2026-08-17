import Database from "better-sqlite3";
import fs from "node:fs";
import path from "node:path";
import { webDataDir } from "./paths";
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

function migrate(db: Database.Database): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS settings (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS model_profiles (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      base_url TEXT NOT NULL,
      model TEXT NOT NULL,
      api_key TEXT NOT NULL DEFAULT '',
      env_key TEXT NOT NULL DEFAULT '',
      enabled INTEGER NOT NULL DEFAULT 0,
      vision_supported INTEGER NOT NULL DEFAULT 1,
      proxy TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS stock_pool (
      symbol TEXT PRIMARY KEY,
      name TEXT NOT NULL DEFAULT '',
      source TEXT NOT NULL DEFAULT 'manual',
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS pending_imports (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL,
      raw TEXT NOT NULL DEFAULT '',
      candidates TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      payload TEXT NOT NULL DEFAULT '{}',
      result_path TEXT,
      error TEXT,
      created_at TEXT NOT NULL,
      started_at TEXT,
      finished_at TEXT
    );
    CREATE TABLE IF NOT EXISTS analysis_notes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      job_id INTEGER,
      symbol TEXT NOT NULL DEFAULT '',
      content TEXT NOT NULL,
      model TEXT NOT NULL DEFAULT '',
      result_path TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS schedule (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL UNIQUE,
      time TEXT NOT NULL DEFAULT '15:20',
      interval_seconds INTEGER NOT NULL DEFAULT 60,
      fixed_times TEXT NOT NULL DEFAULT '[]',
      trading_days_only INTEGER NOT NULL DEFAULT 1,
      enabled INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS operation_logs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      job_id INTEGER,
      level TEXT NOT NULL DEFAULT 'info',
      module TEXT NOT NULL DEFAULT '',
      message TEXT NOT NULL,
      detail TEXT,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_operation_logs_created ON operation_logs(created_at);
    CREATE TABLE IF NOT EXISTS holdings (
      symbol TEXT PRIMARY KEY,
      name TEXT NOT NULL DEFAULT '',
      shares REAL NOT NULL DEFAULT 0,
      cost_price REAL NOT NULL DEFAULT 0,
      total_amount REAL NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
  `);
  try {
    db.exec("ALTER TABLE model_profiles ADD COLUMN proxy TEXT NOT NULL DEFAULT ''");
  } catch {
    /* column already exists on databases created with the new schema */
  }
  try {
    db.exec("ALTER TABLE schedule ADD COLUMN fixed_times TEXT NOT NULL DEFAULT '[]'");
  } catch {
    /* column already exists on databases created with the new schema */
  }
  try {
    db.exec("ALTER TABLE analysis_notes ADD COLUMN result_path TEXT");
  } catch {
    /* column already exists on databases created with the new schema */
  }
  // One-time migration: timestamps written before this change were UTC (nowIso used
  // toISOString()); shift them to Asia/Shanghai so the whole history is Beijing time.
  const timezoneMigrated = db
    .prepare("SELECT value FROM settings WHERE key = 'meta.beijing_time_migrated'")
    .get() as { value: string } | undefined;
  if (!timezoneMigrated) {
    db.exec(`
      UPDATE jobs SET
        created_at = datetime(created_at, '+8 hours'),
        started_at = datetime(started_at, '+8 hours'),
        finished_at = datetime(finished_at, '+8 hours');
      UPDATE analysis_notes SET created_at = datetime(created_at, '+8 hours');
      UPDATE model_profiles SET created_at = datetime(created_at, '+8 hours'), updated_at = datetime(updated_at, '+8 hours');
      UPDATE stock_pool SET created_at = datetime(created_at, '+8 hours');
      UPDATE pending_imports SET created_at = datetime(created_at, '+8 hours');
      UPDATE schedule SET updated_at = datetime(updated_at, '+8 hours');
      UPDATE settings SET updated_at = datetime(updated_at, '+8 hours');
    `);
    db.prepare("INSERT INTO settings (key, value, updated_at) VALUES ('meta.beijing_time_migrated', 'true', ?)").run(nowIso());
  }
  const count = db.prepare("SELECT COUNT(*) AS count FROM schedule").get() as { count: number };
  if (count.count === 0) {
    const now = nowIso();
    const insert = db.prepare(
      "INSERT INTO schedule (kind, time, interval_seconds, trading_days_only, enabled, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
    );
    insert.run("daily_scan", "15:20", 60, 1, 1, now);
    insert.run("monitor_cycle", "09:30", 60, 1, 1, now);
  }
  const schedulerAutostartMigrated = db
    .prepare("SELECT value FROM settings WHERE key = 'meta.scheduler_autostart_v2'")
    .get() as { value: string } | undefined;
  if (!schedulerAutostartMigrated) {
    db.prepare("UPDATE schedule SET enabled = 1, updated_at = ?").run(nowIso());
    db.prepare("INSERT INTO settings (key, value, updated_at) VALUES ('meta.scheduler_autostart_v2', 'true', ?)")
      .run(nowIso());
  }
}

export function openDb(dbPath: string): Database.Database {
  fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  const db = new Database(dbPath);
  db.pragma("journal_mode = WAL");
  migrate(db);
  return db;
}

const singleton: { db: Database.Database | null; path: string } = { db: null, path: "" };

export function getDb(): Database.Database {
  const dbPath = process.env.WEB_DATA_DIR
    ? path.join(process.env.WEB_DATA_DIR, "app.db")
    : path.join(webDataDir, "app.db");
  if (!singleton.db || singleton.path !== dbPath) {
    singleton.db = openDb(dbPath);
    singleton.path = dbPath;
  }
  return singleton.db;
}

// ---------- settings ----------
export function getSetting(key: string, db = getDb()): unknown | null {
  const row = db.prepare("SELECT value FROM settings WHERE key = ?").get(key) as
    | { value: string }
    | undefined;
  if (!row) return null;
  try {
    return JSON.parse(row.value) as unknown;
  } catch {
    return null;
  }
}

export function setSetting(key: string, value: unknown, db = getDb()): void {
  db.prepare(
    `INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
     ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`
  ).run(key, JSON.stringify(value), nowIso());
}

export function deleteSetting(key: string, db = getDb()): void {
  db.prepare("DELETE FROM settings WHERE key = ?").run(key);
}

export function getAllSettings(db = getDb()): Record<string, unknown> {
  const rows = db.prepare("SELECT key, value FROM settings").all() as Array<{ key: string; value: string }>;
  const result: Record<string, unknown> = {};
  for (const row of rows) {
    try {
      result[row.key] = JSON.parse(row.value) as unknown;
    } catch {
      result[row.key] = row.value;
    }
  }
  return result;
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

export function listModels(db = getDb()): ModelProfile[] {
  const rows = db.prepare("SELECT * FROM model_profiles ORDER BY id").all() as Array<Record<string, unknown>>;
  return rows.map(rowToModel);
}

export function getModel(id: number, db = getDb()): ModelProfile | null {
  const row = db.prepare("SELECT * FROM model_profiles WHERE id = ?").get(id) as Record<string, unknown> | undefined;
  return row ? rowToModel(row) : null;
}

export function createModel(
  input: { name: string; base_url: string; model: string; api_key?: string; env_key?: string; proxy?: string; enabled?: boolean; vision_supported?: boolean },
  db = getDb()
): number {
  const now = nowIso();
  const result = db
    .prepare(
      `INSERT INTO model_profiles (name, base_url, model, api_key, env_key, proxy, enabled, vision_supported, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
    .run(
      input.name,
      input.base_url,
      input.model,
      input.api_key ?? "",
      input.env_key ?? "",
      input.proxy ?? "",
      input.enabled ? 1 : 0,
      input.vision_supported !== false ? 1 : 0,
      now,
      now
    );
  return Number(result.lastInsertRowid);
}

export function updateModel(
  id: number,
  input: Partial<{ name: string; base_url: string; model: string; api_key: string; env_key: string; proxy: string; enabled: boolean; vision_supported: boolean }>,
  db = getDb()
): void {
  const current = getModel(id, db);
  if (!current) return;
  const merged = { ...current, ...input, updated_at: nowIso() };
  db.prepare(
    `UPDATE model_profiles SET name=?, base_url=?, model=?, api_key=?, env_key=?, proxy=?, enabled=?, vision_supported=?, updated_at=? WHERE id=?`
  ).run(
    merged.name,
    merged.base_url,
    merged.model,
    merged.api_key,
    merged.env_key,
    merged.proxy,
    merged.enabled ? 1 : 0,
    merged.vision_supported ? 1 : 0,
    merged.updated_at,
    id
  );
}

export function deleteModel(id: number, db = getDb()): void {
  db.prepare("DELETE FROM model_profiles WHERE id = ?").run(id);
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

export function listPool(db = getDb()): PoolRow[] {
  const rows = db.prepare("SELECT * FROM stock_pool ORDER BY symbol").all() as Array<Record<string, unknown>>;
  return rows.map(rowToPool);
}

export function addPoolSymbols(
  items: Array<{ symbol: string; name?: string; source?: string }>,
  db = getDb()
): number {
  const insert = db.prepare(
    `INSERT INTO stock_pool (symbol, name, source, created_at) VALUES (?, ?, ?, ?)
     ON CONFLICT(symbol) DO UPDATE SET name = excluded.name, source = excluded.source`
  );
  let added = 0;
  const now = nowIso();
  for (const item of items) {
    const symbol = item.symbol?.trim().toUpperCase();
    if (!symbol) continue;
    const result = insert.run(symbol, item.name ?? "", item.source ?? "manual", now);
    if (result.changes > 0) added += 1;
  }
  syncWatchlistFromPool(db);
  return added;
}

export function updatePoolSymbol(symbol: string, name: string, db = getDb()): void {
  db.prepare("UPDATE stock_pool SET name = ? WHERE symbol = ?").run(name, symbol);
}

export function removePoolSymbol(symbol: string, db = getDb()): void {
  db.prepare("DELETE FROM stock_pool WHERE symbol = ?").run(symbol);
  syncWatchlistFromPool(db);
}

/** 自选股票池是引擎 watchlist 的唯一来源，增删后同步到配置。 */
export function syncWatchlistFromPool(db = getDb()): string[] {
  const symbols = listPool(db).map((row) => row.symbol);
  setSetting("monitor.watchlist", symbols, db);
  return symbols;
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

export function createPendingImport(kind: string, raw: string, candidates: unknown, db = getDb()): number {
  const result = db
    .prepare("INSERT INTO pending_imports (kind, raw, candidates, status, created_at) VALUES (?, ?, ?, 'pending', ?)")
    .run(kind, raw, JSON.stringify(candidates), nowIso());
  return Number(result.lastInsertRowid);
}

export function listPendingImports(db = getDb()): PendingImport[] {
  const rows = db
    .prepare("SELECT * FROM pending_imports WHERE status = 'pending' ORDER BY id DESC")
    .all() as Array<Record<string, unknown>>;
  return rows.map(rowToPending);
}

export function getPendingImport(id: number, db = getDb()): PendingImport | null {
  const row = db.prepare("SELECT * FROM pending_imports WHERE id = ?").get(id) as Record<string, unknown> | undefined;
  return row ? rowToPending(row) : null;
}

export function setPendingStatus(id: number, status: "confirmed" | "cancelled", db = getDb()): void {
  db.prepare("UPDATE pending_imports SET status = ? WHERE id = ?").run(status, id);
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

export function createJob(kind: string, payload: unknown, db = getDb()): number {
  const result = db
    .prepare("INSERT INTO jobs (kind, status, payload, created_at) VALUES (?, 'pending', ?, ?)")
    .run(kind, JSON.stringify(payload), nowIso());
  return Number(result.lastInsertRowid);
}

export function updateJob(
  id: number,
  patch: Partial<Pick<JobRow, "status" | "result_path" | "error" | "started_at" | "finished_at">>,
  db = getDb()
): void {
  const fields: string[] = [];
  const values: unknown[] = [];
  if (patch.status !== undefined) { fields.push("status = ?"); values.push(patch.status); }
  if (patch.result_path !== undefined) { fields.push("result_path = ?"); values.push(patch.result_path); }
  if (patch.error !== undefined) { fields.push("error = ?"); values.push(patch.error); }
  if (patch.started_at !== undefined) { fields.push("started_at = ?"); values.push(patch.started_at); }
  if (patch.finished_at !== undefined) { fields.push("finished_at = ?"); values.push(patch.finished_at); }
  if (fields.length === 0) return;
  values.push(id);
  db.prepare(`UPDATE jobs SET ${fields.join(", ")} WHERE id = ?`).run(...values);
}

export function getJob(id: number, db = getDb()): JobRow | null {
  const row = db.prepare("SELECT * FROM jobs WHERE id = ?").get(id) as Record<string, unknown> | undefined;
  return row ? rowToJob(row) : null;
}

export function listJobs(limit = 100, db = getDb()): JobRow[] {
  const rows = db
    .prepare("SELECT * FROM jobs ORDER BY id DESC LIMIT ?")
    .all(limit) as Array<Record<string, unknown>>;
  return rows.map(rowToJob);
}

export function listJobsByKindSince(kind: string, datePrefix: string, db = getDb()): JobRow[] {
  const rows = db
    .prepare("SELECT * FROM jobs WHERE kind = ? AND created_at LIKE ? ORDER BY id DESC")
    .all(kind, `${datePrefix}%`) as Array<Record<string, unknown>>;
  return rows.map(rowToJob);
}

export function listRecoverableJobs(kind: string, db = getDb()): JobRow[] {
  const rows = db
    .prepare("SELECT * FROM jobs WHERE kind = ? AND status IN ('pending', 'running') ORDER BY id")
    .all(kind) as Array<Record<string, unknown>>;
  return rows.map(rowToJob);
}

export function failInterruptedJobs(db = getDb()): number {
  const result = db
    .prepare(
      `UPDATE jobs
       SET status = 'failed', error = ?, finished_at = ?
       WHERE status IN ('pending', 'running') AND kind <> 'interpret-report'`
    )
    .run("Web 服务重启，原任务进程已中断；定时任务将按计划补跑", nowIso());
  return result.changes;
}

export function jobsRunningSince(datePrefix: string, kind: string, db = getDb()): boolean {
  const row = db
    .prepare("SELECT COUNT(*) AS count FROM jobs WHERE kind = ? AND status = 'success' AND created_at LIKE ?")
    .get(kind, `${datePrefix}%`) as { count: number };
  return Number(row.count) > 0;
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

export function addNote(
  input: { job_id: number | null; symbol?: string; content: string; model?: string; result_path?: string | null },
  db = getDb()
): number {
  const result = db
    .prepare("INSERT INTO analysis_notes (job_id, symbol, content, model, result_path, created_at) VALUES (?, ?, ?, ?, ?, ?)")
    .run(input.job_id, input.symbol ?? "", input.content, input.model ?? "", input.result_path ?? null, nowIso());
  return Number(result.lastInsertRowid);
}

export function findNoteByJobAndResult(
  jobId: number,
  resultPath: string,
  db = getDb()
): AnalysisNote | null {
  const row = db
    .prepare("SELECT * FROM analysis_notes WHERE job_id = ? AND result_path = ? ORDER BY id DESC LIMIT 1")
    .get(jobId, resultPath) as Record<string, unknown> | undefined;
  return row ? rowToNote(row) : null;
}

export function listNotesByJob(jobId: number, db = getDb()): AnalysisNote[] {
  const rows = db
    .prepare("SELECT * FROM analysis_notes WHERE job_id = ? ORDER BY id DESC")
    .all(jobId) as Array<Record<string, unknown>>;
  return rows.map(rowToNote);
}

export interface NoteWithJob extends AnalysisNote {
  job_kind: string | null;
}

export function listNotes(limit = 200, db = getDb()): NoteWithJob[] {
  const rows = db
    .prepare(
      `SELECT n.*, j.kind AS job_kind
       FROM analysis_notes n
       LEFT JOIN jobs j ON j.id = n.job_id
       ORDER BY n.id DESC LIMIT ?`
    )
    .all(limit) as Array<Record<string, unknown>>;
  return rows.map((row) => ({
    ...rowToNote(row),
    job_kind: row.job_kind == null ? null : String(row.job_kind),
  }));
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

export function getScheduleRows(db = getDb()): ScheduleRow[] {
  const rows = db.prepare("SELECT * FROM schedule ORDER BY id").all() as Array<Record<string, unknown>>;
  return rows.map(rowToSchedule);
}

export function upsertScheduleRow(
  kind: "daily_scan" | "monitor_cycle",
  input: Partial<{
    time: string;
    interval_seconds: number;
    fixed_times: string[];
    trading_days_only: boolean;
    enabled: boolean;
  }>,
  db = getDb()
): void {
  const current = getScheduleRows(db).find((row) => row.kind === kind);
  const now = nowIso();
  if (!current) {
    db.prepare(
      `INSERT INTO schedule (kind, time, interval_seconds, fixed_times, trading_days_only, enabled, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    ).run(
      kind,
      input.time ?? "15:20",
      input.interval_seconds ?? 60,
      JSON.stringify(input.fixed_times ?? []),
      input.trading_days_only !== false ? 1 : 0,
      input.enabled ? 1 : 0,
      now
    );
    return;
  }
  db.prepare(
    `UPDATE schedule SET time=?, interval_seconds=?, fixed_times=?, trading_days_only=?, enabled=?, updated_at=? WHERE kind=?`
  ).run(
    input.time ?? current.time,
    input.interval_seconds ?? current.interval_seconds,
    JSON.stringify(input.fixed_times ?? current.fixed_times),
    input.trading_days_only !== undefined ? (input.trading_days_only ? 1 : 0) : (current.trading_days_only ? 1 : 0),
    input.enabled !== undefined ? (input.enabled ? 1 : 0) : (current.enabled ? 1 : 0),
    now,
    kind
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

export function addOperationLog(
  input: {
    job_id?: number | null;
    level: "info" | "warning" | "error";
    module: string;
    message: string;
    detail?: string | null;
  },
  db = getDb()
): number {
  const result = db
    .prepare(
      "INSERT INTO operation_logs (job_id, level, module, message, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)"
    )
    .run(input.job_id ?? null, input.level, input.module, input.message, input.detail ?? null, nowIso());
  return Number(result.lastInsertRowid);
}

export function listOperationLogs(
  limit = 300,
  level?: "info" | "warning" | "error",
  db = getDb()
): OperationLog[] {
  const rows = level
    ? db
        .prepare("SELECT * FROM operation_logs WHERE level = ? ORDER BY id DESC LIMIT ?")
        .all(level, limit)
    : db.prepare("SELECT * FROM operation_logs ORDER BY id DESC LIMIT ?").all(limit);
  return (rows as Array<Record<string, unknown>>).map(rowToOperationLog);
}

export function clearOperationLogs(db = getDb()): number {
  const result = db.prepare("DELETE FROM operation_logs").run();
  return Number(result.changes);
}

// ---------- holdings ----------
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

export function listHoldings(db = getDb()): HoldingRow[] {
  const rows = db.prepare("SELECT * FROM holdings ORDER BY symbol").all() as Array<Record<string, unknown>>;
  return rows.map(rowToHolding);
}

/** Create or update a holding. When total_amount is empty and shares/cost are valid, it is computed automatically. */
export function upsertHolding(
  input: { symbol: string; name?: string; shares?: number; cost_price?: number; total_amount?: number },
  db = getDb()
): HoldingRow {
  const symbol = input.symbol.trim().toUpperCase();
  const shares = Number(input.shares ?? 0);
  const costPrice = Number(input.cost_price ?? 0);
  let totalAmount = Number(input.total_amount ?? 0);
  if (totalAmount <= 0 && shares > 0 && costPrice > 0) {
    totalAmount = Math.round(shares * costPrice * 100) / 100;
  }
  const now = nowIso();
  db.prepare(
    `INSERT INTO holdings (symbol, name, shares, cost_price, total_amount, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(symbol) DO UPDATE SET
       name = excluded.name,
       shares = excluded.shares,
       cost_price = excluded.cost_price,
       total_amount = excluded.total_amount,
       updated_at = excluded.updated_at`
  ).run(symbol, input.name ?? "", shares, costPrice, totalAmount, now, now);
  return {
    symbol,
    name: input.name ?? "",
    shares,
    cost_price: costPrice,
    total_amount: totalAmount,
    created_at: now,
    updated_at: now,
  };
}

export function removeHolding(symbol: string, db = getDb()): void {
  db.prepare("DELETE FROM holdings WHERE symbol = ?").run(symbol.trim().toUpperCase());
}
