import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "pg";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const schemaPath = path.join(root, "db", "schema.sql");
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
try {
  await client.connect();
  await client.query(await fs.readFile(schemaPath, "utf8"));
  const version = await client.query("SELECT version FROM quant.schema_meta ORDER BY version DESC LIMIT 1");
  const exposed = await client.query(
    `SELECT table_name FROM information_schema.tables
     WHERE table_schema = 'quant' AND table_name <> 'schema_meta'
       AND has_table_privilege(current_user, format('%I.%I', table_schema, table_name), 'SELECT')
     ORDER BY table_name`,
  );
  console.log(JSON.stringify({ schema_version: Number(version.rows[0]?.version ?? 0), current_user: "redacted", tables: exposed.rows.map((row) => row.table_name) }));
} finally {
  await client.end().catch(() => undefined);
}
