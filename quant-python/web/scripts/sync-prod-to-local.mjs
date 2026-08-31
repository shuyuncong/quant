/**
 * One-way data sync: production Supabase (via db-tunnel) -> local Docker Postgres.
 *
 * Direction is DOWNSTREAM ONLY (prod -> local). Local test data must never
 * flow back up. Schema (DDL) is applied via db:setup, not by this script.
 *
 * Requires:
 *   - PROD_DATABASE_URL  (or DATABASE_URL) pointing at the production server
 *   - LOCAL_DATABASE_URL pointing at the local dev Postgres
 *   - PROD_SSL_SERVERNAME / PROD_SSL_REJECT_UNAUTHORIZED for TLS (optional)
 *
 * The local DB is truncated (schema kept) and refilled from prod, then all
 * sequences are reset to MAX(id). Safe to re-run; it is destructive to LOCAL
 * data only.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "pg";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const schemaSql = fs.readFileSync(path.join(root, "db", "schema.sql"), "utf8");

function sslFor(urlString, prefix) {
  const parsed = new URL(urlString);
  const local = parsed.hostname === "localhost" || parsed.hostname === "127.0.0.1" || parsed.hostname === "::1";
  const servername = process.env[`${prefix}_SSL_SERVERNAME`]?.trim();
  if (local && !servername) return false;
  return {
    rejectUnauthorized: process.env[`${prefix}_SSL_REJECT_UNAUTHORIZED`] !== "false",
    ...(servername ? { servername } : {}),
  };
}

const prodUrl = process.env.PROD_DATABASE_URL?.trim();
const localUrl = (process.env.LOCAL_DATABASE_URL ?? process.env.DATABASE_URL)?.trim();
if (!prodUrl) throw new Error("PROD_DATABASE_URL is required (prod is never DATABASE_URL)");
if (!localUrl) throw new Error("LOCAL_DATABASE_URL (or DATABASE_URL) is required");

const prodClient = new Client({ connectionString: prodUrl, ssl: sslFor(prodUrl, "PROD"), connectionTimeoutMillis: 20_000 });
const localClient = new Client({ connectionString: localUrl, ssl: sslFor(localUrl, "LOCAL"), connectionTimeoutMillis: 20_000 });

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

try {
  await prodClient.connect();
  await localClient.connect();
  await localClient.query(schemaSql);
  await localClient.query("BEGIN");
  await localClient.query(`TRUNCATE quant.${tables.map((t) => t.name).join(", quant.")} RESTART IDENTITY CASCADE`);
  const counts = {};
  for (const table of tables) {
    const source = await prodClient.query(`SELECT ${table.columns.join(", ")} FROM quant.${table.name}`);
    counts[table.name] = source.rowCount;
    for (const row of source.rows) {
      const placeholders = table.columns.map((_, i) => `$${i + 1}`).join(", ");
      const values = table.columns.map((column) =>
        boolColumns.has(column) ? Boolean(row[column]) : row[column] ?? null,
      );
      await localClient.query(
        `INSERT INTO quant.${table.name} (${table.columns.join(", ")}) VALUES (${placeholders})`,
        values,
      );
    }
    if (serialTables.has(table.name)) {
      await localClient.query(
        `SELECT setval(
           pg_get_serial_sequence('quant.${table.name}', 'id'),
           COALESCE((SELECT MAX(id) FROM quant.${table.name}), 1),
           EXISTS (SELECT 1 FROM quant.${table.name})
         )`,
      );
    }
  }
  await localClient.query("COMMIT");
  console.log(JSON.stringify({ mode: "prod-to-local", counts }));
} catch (error) {
  await localClient.query("ROLLBACK").catch(() => undefined);
  throw error;
} finally {
  await prodClient.end().catch(() => undefined);
  await localClient.end().catch(() => undefined);
}
