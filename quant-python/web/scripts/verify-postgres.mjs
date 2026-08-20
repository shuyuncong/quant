import { Client } from "pg";

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
const client = new Client({
  connectionString,
  ssl,
  connectionTimeoutMillis: 15_000,
});
const tables = ["settings", "model_profiles", "stock_pool", "pending_imports", "jobs", "analysis_notes", "schedule", "operation_logs", "holdings"];
const serialTables = ["model_profiles", "pending_imports", "jobs", "analysis_notes", "schedule", "operation_logs"];
try {
  await client.connect();
  const version = await client.query("SHOW server_version");
  const counts = {};
  for (const table of tables) {
    const result = await client.query(`SELECT count(*)::integer AS count FROM quant.${table}`);
    counts[table] = result.rows[0].count;
  }
  const sequences = {};
  for (const table of serialTables) {
    const result = await client.query(
      `SELECT
         COALESCE((SELECT MAX(id) FROM quant.${table}), 0)::bigint AS max_id,
         last_value::bigint AS last_value
       FROM pg_sequences
       WHERE schemaname = 'quant' AND sequencename = $1`,
      [`${table}_id_seq`],
    );
    const maxId = Number(result.rows[0]?.max_id ?? 0);
    const lastValue = Number(result.rows[0]?.last_value ?? 0);
    if (!result.rows[0] || lastValue < maxId) {
      throw new Error(`Sequence quant.${table}_id_seq is behind table max id (${lastValue} < ${maxId})`);
    }
    sequences[table] = { max_id: maxId, last_value: lastValue };
  }
  await client.query("BEGIN");
  const marker = `verify-${Date.now()}`;
  await client.query("INSERT INTO quant.settings (key, value, updated_at) VALUES ($1, $2, $3)", [marker, JSON.stringify(true), "2099-01-01 00:00:00"]);
  const read = await client.query("SELECT value FROM quant.settings WHERE key = $1", [marker]);
  await client.query("ROLLBACK");
  const privilege = await client.query("SELECT has_schema_privilege('public', 'quant', 'USAGE') AS usage");
  console.log(JSON.stringify({ server_version: version.rows[0].server_version, counts, sequences, transaction_round_trip: read.rows[0]?.value === "true", public_schema_usage: privilege.rows[0]?.usage }));
} finally {
  await client.end().catch(() => undefined);
}
