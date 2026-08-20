import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import Database from "better-sqlite3";
import { Client } from "pg";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const schemaSql = fs.readFileSync(path.join(root, "db", "schema.sql"), "utf8");
const mode = process.argv[2] ?? "";
const connectionString = process.env.DATABASE_URL?.trim();
if (!connectionString) throw new Error("DATABASE_URL is required");
const parsedUrl = new URL(connectionString);
const servername = process.env.DATABASE_SSL_SERVERNAME?.trim();
const local = parsedUrl.hostname === "localhost" || parsedUrl.hostname === "127.0.0.1" || parsedUrl.hostname === "::1";
const ssl = local && !servername
  ? false
  : {
      rejectUnauthorized: process.env.DATABASE_SSL_REJECT_UNAUTHORIZED !== "false",
      ...(servername ? { servername } : {}),
    };

const tables = [
  { name: "settings", key: ["key"], columns: ["key", "value", "updated_at"] },
  { name: "model_profiles", key: ["id"], columns: ["id", "name", "base_url", "model", "api_key", "env_key", "enabled", "vision_supported", "created_at", "updated_at", "proxy"] },
  { name: "stock_pool", key: ["symbol"], columns: ["symbol", "name", "source", "created_at"] },
  { name: "pending_imports", key: ["id"], columns: ["id", "kind", "raw", "candidates", "status", "created_at"] },
  { name: "jobs", key: ["id"], columns: ["id", "kind", "status", "payload", "result_path", "error", "created_at", "started_at", "finished_at"] },
  { name: "analysis_notes", key: ["id"], columns: ["id", "job_id", "symbol", "content", "model", "created_at", "result_path"] },
  { name: "schedule", key: ["id"], columns: ["id", "kind", "time", "interval_seconds", "trading_days_only", "enabled", "updated_at", "fixed_times"] },
  { name: "operation_logs", key: ["id"], columns: ["id", "job_id", "level", "module", "message", "detail", "created_at"] },
  { name: "holdings", key: ["symbol"], columns: ["symbol", "name", "shares", "cost_price", "total_amount", "created_at", "updated_at"] },
];
const serialTables = new Set(["model_profiles", "pending_imports", "jobs", "analysis_notes", "schedule", "operation_logs"]);
const boolColumns = new Set(["enabled", "vision_supported", "trading_days_only"]);
const numericColumns = new Set(["id", "job_id", "interval_seconds", "shares", "cost_price", "total_amount"]);
const timestampColumns = new Set(["created_at", "updated_at", "started_at", "finished_at"]);

function shiftLegacyTimestamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value)) return value;
  const date = new Date(`${value.replace(" ", "T")}Z`);
  date.setUTCHours(date.getUTCHours() + 8);
  const pad = (part) => String(part).padStart(2, "0");
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
}

function sqlitePath() {
  if (process.env.SQLITE_PATH?.trim()) return process.env.SQLITE_PATH.trim();
  const dataDir = process.env.WEB_DATA_DIR?.trim() || path.join(root, "data");
  return path.join(dataDir, "app.db");
}

function normalize(table, column, value) {
  if (value === null || value === undefined) return null;
  if (boolColumns.has(column)) return Boolean(value);
  if (numericColumns.has(column)) return String(Number(value));
  return String(value);
}

function canonical(table, row) {
  return JSON.stringify(Object.fromEntries(table.columns.map((column) => [column, normalize(table.name, column, row[column])] )));
}

function readSqlite() {
  const source = sqlitePath();
  if (!fs.existsSync(source)) throw new Error(`SQLite source does not exist: ${source}`);
  const db = new Database(source, { readonly: true, fileMustExist: true });
  db.exec("BEGIN");
  try {
    const result = new Map();
    for (const table of tables) {
      const available = new Set(db.prepare(`PRAGMA table_info(${table.name})`).all().map((column) => column.name));
      const expressions = table.columns.map((column) => {
        if (available.has(column)) return column;
        if (column === "proxy") return "'' AS proxy";
        if (column === "fixed_times") return "'[]' AS fixed_times";
        if (column === "result_path") return "NULL AS result_path";
        throw new Error(`SQLite source table ${table.name} is missing required column ${column}`);
      });
      result.set(table.name, db.prepare(`SELECT ${expressions.join(", ")} FROM ${table.name}`).all());
    }
    const settings = result.get("settings");
    const alreadyBeijing = settings.some((row) => row.key === "meta.beijing_time_migrated");
    if (!alreadyBeijing) {
      for (const table of tables) {
        for (const row of result.get(table.name)) {
          for (const column of table.columns) {
            if (timestampColumns.has(column) && row[column] != null) row[column] = shiftLegacyTimestamp(row[column]);
          }
        }
      }
      const schedule = result.get("schedule");
      for (const row of schedule) {
        if (row.time === "15:20") row.time = "04:00";
        if (Number(row.interval_seconds) === 60 && (row.fixed_times === "[]" || row.fixed_times === "null")) {
          row.interval_seconds = 3600;
          row.fixed_times = '["10:30","13:30","14:30"]';
        }
        row.enabled = 1;
      }
      const migratedAt = shiftLegacyTimestamp(new Date().toISOString().slice(0, 19).replace("T", " "));
      if (!settings.some((row) => row.key === "meta.beijing_time_migrated")) {
        settings.push({ key: "meta.beijing_time_migrated", value: "true", updated_at: migratedAt });
      }
      if (!settings.some((row) => row.key === "meta.scheduler_autostart_v2")) {
        settings.push({ key: "meta.scheduler_autostart_v2", value: "true", updated_at: migratedAt });
      }
    }
    return { db, result, source };
  } catch (error) {
    db.exec("ROLLBACK");
    db.close();
    throw error;
  }
}

async function setup(client) {
  await client.query(schemaSql);
}

async function closePostgres(client) {
  let timeout;
  try {
    await Promise.race([
      client.end(),
      new Promise((resolve) => {
        timeout = setTimeout(() => {
          client.connection?.stream?.destroy();
          resolve();
        }, 2_000);
      }),
    ]);
  } catch {
    client.connection?.stream?.destroy();
  } finally {
    clearTimeout(timeout);
  }
}

async function postgresRows(client, table) {
  const result = await client.query(`SELECT ${table.columns.join(", ")} FROM quant.${table.name}`);
  return result.rows;
}

function keyed(table, rows) {
  return new Map(rows.map((row) => [table.key.map((key) => normalize(table.name, key, row[key])).join("\u001f"), row]));
}

async function migrateSqliteToPostgres() {
  const snapshot = readSqlite();
  const client = new Client({ connectionString, ssl, connectionTimeoutMillis: 15_000 });
  try {
    await client.connect();
    await setup(client);
    await client.query("BEGIN");
    let inserted = 0;
    for (const table of tables) {
      const sourceRows = snapshot.result.get(table.name);
      const targetRows = await postgresRows(client, table);
      const sourceByKey = keyed(table, sourceRows);
      const targetByKey = keyed(table, targetRows);
      if (targetRows.length > 0) {
        if (sourceByKey.size !== targetByKey.size) throw new Error(`Refusing non-equivalent target table quant.${table.name}: row count differs`);
        for (const [key, sourceRow] of sourceByKey) {
          const targetRow = targetByKey.get(key);
          if (!targetRow || canonical(table, sourceRow) !== canonical(table, targetRow)) {
            throw new Error(`Refusing non-equivalent target row quant.${table.name} key ${key}`);
          }
        }
      } else {
        for (const row of sourceRows) {
          const placeholders = table.columns.map((_, index) => `$${index + 1}`).join(", ");
          const values = table.columns.map((column) => {
            if (boolColumns.has(column)) return Boolean(row[column]);
            return row[column] ?? null;
          });
          await client.query(`INSERT INTO quant.${table.name} (${table.columns.join(", ")}) VALUES (${placeholders})`, values);
          inserted += 1;
        }
      }
      if (serialTables.has(table.name)) {
        await client.query(
          `SELECT setval(
             pg_get_serial_sequence('quant.${table.name}', 'id'),
             COALESCE((SELECT MAX(id) FROM quant.${table.name}), 1),
             EXISTS (SELECT 1 FROM quant.${table.name})
           )`,
        );
      }
    }
    await client.query("COMMIT");
    console.log(JSON.stringify({ mode: "sqlite-to-postgres", source: snapshot.source, inserted, tables: Object.fromEntries(tables.map((table) => [table.name, snapshot.result.get(table.name).length])) }));
  } catch (error) {
    await client.query("ROLLBACK").catch(() => undefined);
    throw error;
  } finally {
    await closePostgres(client);
    snapshot.db.exec("ROLLBACK");
    snapshot.db.close();
  }
}

async function exportPostgresToSqlite() {
  const output = process.env.ROLLBACK_SQLITE_PATH?.trim() || path.join(root, "data", `app.rollback-${Date.now()}.db`);
  if (fs.existsSync(output)) throw new Error(`Rollback output already exists: ${output}`);
  fs.mkdirSync(path.dirname(output), { recursive: true });
  const client = new Client({ connectionString, ssl, connectionTimeoutMillis: 15_000 });
  const sqlite = new Database(output);
  try {
    await client.connect();
    await client.query("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY");
    sqlite.exec(`
      CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE TABLE model_profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, base_url TEXT NOT NULL, model TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', env_key TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0, vision_supported INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, proxy TEXT NOT NULL DEFAULT '');
      CREATE TABLE stock_pool (symbol TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL);
      CREATE TABLE pending_imports (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, raw TEXT NOT NULL DEFAULT '', candidates TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL);
      CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', payload TEXT NOT NULL DEFAULT '{}', result_path TEXT, error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
      CREATE TABLE analysis_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, symbol TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, result_path TEXT);
      CREATE TABLE schedule (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL UNIQUE, time TEXT NOT NULL DEFAULT '15:20', interval_seconds INTEGER NOT NULL DEFAULT 60, trading_days_only INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, fixed_times TEXT NOT NULL DEFAULT '[]');
      CREATE TABLE operation_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, level TEXT NOT NULL DEFAULT 'info', module TEXT NOT NULL DEFAULT '', message TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL);
      CREATE INDEX idx_operation_logs_created ON operation_logs(created_at);
      CREATE TABLE holdings (symbol TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', shares REAL NOT NULL DEFAULT 0, cost_price REAL NOT NULL DEFAULT 0, total_amount REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    `);
    const snapshots = [];
    for (const table of tables) {
      snapshots.push({ table, result: await client.query(`SELECT ${table.columns.join(", ")} FROM quant.${table.name}`) });
    }
    const insertAll = sqlite.transaction(() => {
      for (const { table, result } of snapshots) {
        const insert = sqlite.prepare(`INSERT INTO ${table.name} (${table.columns.join(", ")}) VALUES (${table.columns.map(() => "?").join(", ")})`);
        for (const row of result.rows) insert.run(table.columns.map((column) => boolColumns.has(column) ? (row[column] ? 1 : 0) : row[column] ?? null));
      }
    });
    insertAll();
    for (const { table, result } of snapshots) {
      const exported = sqlite.prepare(`SELECT ${table.columns.join(", ")} FROM ${table.name}`).all();
      const sourceByKey = keyed(table, result.rows);
      const exportedByKey = keyed(table, exported);
      if (sourceByKey.size !== exportedByKey.size) throw new Error(`Rollback verification failed for ${table.name}: row count differs`);
      for (const [key, sourceRow] of sourceByKey) {
        const exportedRow = exportedByKey.get(key);
        if (!exportedRow || canonical(table, sourceRow) !== canonical(table, exportedRow)) {
          throw new Error(`Rollback verification failed for ${table.name} key ${key}`);
        }
      }
    }
    await client.query("COMMIT");
    sqlite.close();
    console.log(JSON.stringify({ mode: "postgres-to-sqlite", output }));
  } catch (error) {
    await client.query("ROLLBACK").catch(() => undefined);
    sqlite.close();
    fs.rmSync(output, { force: true });
    throw error;
  } finally {
    await closePostgres(client);
  }
}

async function main() {
  if (mode === "sqlite-to-postgres") await migrateSqliteToPostgres();
  else if (mode === "postgres-to-sqlite") await exportPostgresToSqlite();
  else throw new Error("Usage: migrate-database.mjs sqlite-to-postgres|postgres-to-sqlite");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
