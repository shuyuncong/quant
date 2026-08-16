import Database from "better-sqlite3";
import fs from "node:fs";
import path from "node:path";
import { webDataDir } from "./paths";
import type {
  AnalysisNote,
  JobRow,
  ModelProfile,
  PendingImport,
  PoolRow,
  ScheduleRow,
} from "./types";

export type { ScheduleRow };

export function nowIso(): string {
  return new Date().toISOString().replace("T", " ").slice(0, 19);
}

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
  const count = db.prepare("SELECT COUNT(*) AS count FROM schedule").get() as { count: number };
  if (count.count === 0) {
    const now = nowIso();
    const insert = db.prepare(
      "INSERT INTO schedule (kind, time, interval_seconds, trading_days_only, enabled, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
    );
    insert.run("daily_scan", "15:20", 60, 1, 0, now);
    insert.run("monitor_cycle", "09:30", 60, 1, 0, now);
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
    created_at: String(row.created_at ?? ""),
  };
}

export function addNote(input: { job_id: number | null; symbol?: string; content: string; model?: string }, db = getDb()): number {
  const result = db
    .prepare("INSERT INTO analysis_notes (job_id, symbol, content, model, created_at) VALUES (?, ?, ?, ?, ?)")
    .run(input.job_id, input.symbol ?? "", input.content, input.model ?? "", nowIso());
  return Number(result.lastInsertRowid);
}

export function listNotesByJob(jobId: number, db = getDb()): AnalysisNote[] {
  const rows = db
    .prepare("SELECT * FROM analysis_notes WHERE job_id = ? ORDER BY id DESC")
    .all(jobId) as Array<Record<string, unknown>>;
  return rows.map(rowToNote);
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
