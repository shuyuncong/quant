#!/usr/bin/env node
/**
 * 回填 `quant.stock_analyses.name`（历史上为空的记录）。
 *
 * 为什么需要它：`/api/analyses` 会按 ① 记录自带 name → ② 任务 payload.symbol_names
 * → ③ stock_pool/holdings 的顺序回退。但**已存在的历史记录**两条路都走不通：
 *   - 它们的 name 建记录时就写成了空串（Python 报告的 stock.name 取不到）；
 *   - 它们的任务 payload 里没有 symbol_names（该写入点当时是不可达代码，见 59fc477 之前的修复）。
 * 所以名称表修好了、新任务也会带名字了，**旧记录仍然是代码** —— 除非把 name 补上。
 *
 * 做法：批量调用桥接的 `resolve-names`（其数据源就是 `MarketDataClient.get_stock_list()`），
 * 把结果写回记录。只填空值、不覆盖已有名字。
 *
 * 用法：
 *   node scripts/backfill-analysis-names.mjs            # 试运行，只报告
 *   node scripts/backfill-analysis-names.mjs --apply    # 真正写入
 */
import { Client } from "pg";

const apply = process.argv.includes("--apply");
const connectionString = process.env.DATABASE_URL?.trim();
if (!connectionString) throw new Error("DATABASE_URL is required");

const sslDisabled = process.env.DATABASE_SSL_MODE === "disable";
const ssl = sslDisabled ? false : undefined;
const client = new Client({ connectionString, ssl, connectionTimeoutMillis: 15_000 });

/** 调桥接解析名称；失败返回空表，由调用方决定是否继续。 */
async function resolveNames(symbols) {
  const { spawn } = await import("node:child_process");
  const path = await import("node:path");
  const { fileURLToPath } = await import("node:url");
  const fs = await import("node:fs");
  // 自解析路径：本脚本是 .mjs，不能 import TS 模块（Node 不认 .ts 的类型语法）。
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const signalSystemDir = path.join(root, "..", "signal_system");
  const bridgeScript = path.join(signalSystemDir, "web_bridge.py");
  if (!fs.existsSync(bridgeScript)) {
    throw new Error(`找不到桥接脚本：${bridgeScript}`);
  }
  const payload = JSON.stringify({ symbols });
  const out = await new Promise((resolve) => {
    const child = spawn(process.env.PYTHON_BIN?.trim() || "python", ["-B", bridgeScript, "resolve-names", "--payload", "-"], {
      cwd: signalSystemDir,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    child.stdout.on("data", (d) => { stdout += d; });
    child.on("close", () => resolve(stdout));
    child.on("error", () => resolve(""));
    child.stdin.end(payload);
  });
  try {
    const parsed = JSON.parse(out);
    return parsed?.data?.names ?? {};
  } catch {
    return {};
  }
}

async function main() {
  await client.connect();
  try {
    const { rows } = await client.query(
      `SELECT DISTINCT regexp_replace(regexp_replace(upper(symbol), '^(SH|SZ|BJ)\\.?', ''), '\\.[A-Z]{2,3}$', '') AS code
         FROM quant.stock_analyses
        WHERE name IS NULL OR btrim(name) = ''`,
    );
    const codes = rows.map((r) => r.code).filter(Boolean);
    console.log(JSON.stringify({ mode: apply ? "apply" : "dry-run", blank_records_distinct_codes: codes.length }));
    if (codes.length === 0) return;

    // 分批解析：一次几百只足够，且避免单次桥接超时。
    const names = {};
    const BATCH = 300;
    for (let i = 0; i < codes.length; i += BATCH) {
      const slice = codes.slice(i, i + BATCH);
      const got = await resolveNames(slice);
      Object.assign(names, got);
      console.log(JSON.stringify({ resolved: Object.keys(names).length, of: codes.length }));
    }

    const resolved = Object.keys(names);
    console.log(JSON.stringify({ resolved_total: resolved.length, unresolved: codes.length - resolved.length }));
    if (resolved.length === 0) {
      console.log("没有任何代码解析出名称 —— 名称表可能仍不可用，先确认 get_stock_list() 的规模。");
      return;
    }

    if (!apply) {
      console.log(JSON.stringify({ sample: Object.fromEntries(resolved.slice(0, 8).map((c) => [c, names[c]])) }));
      console.log("试运行结束。确认无误后加 --apply 写入。");
      return;
    }

    // 只填空值，绝不覆盖已有名字；按归一化后的代码匹配。
    const update = await client.query(
      `UPDATE quant.stock_analyses AS s
          SET name = m.name, updated_at = now()
         FROM (
           SELECT * FROM jsonb_each_text($1::jsonb)
         ) AS m(code, name)
        WHERE (s.name IS NULL OR btrim(s.name) = '')
          AND regexp_replace(regexp_replace(upper(s.symbol), '^(SH|SZ|BJ)\\.?', ''), '\\.[A-Z]{2,3}$', '') = m.code`,
      [JSON.stringify(names)],
    );
    console.log(JSON.stringify({ updated_rows: update.rowCount }));
  } finally {
    await client.end().catch(() => undefined);
  }
}

await main();
