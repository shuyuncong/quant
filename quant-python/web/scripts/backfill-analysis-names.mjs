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
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "pg";

const apply = process.argv.includes("--apply");

/** 建库连接。延迟到 main —— 模块被 import 时（测试）不应要求 DATABASE_URL。 */
function makeClient() {
  const connectionString = process.env.DATABASE_URL?.trim();
  if (!connectionString) throw new Error("DATABASE_URL is required");
  const sslDisabled = process.env.DATABASE_SSL_MODE === "disable";
  return new Client({
    connectionString,
    ssl: sslDisabled ? false : undefined,
    connectionTimeoutMillis: 15_000,
  });
}

/** 调桥接解析名称。失败时抛错 —— 静默返回空表会把"取数挂了"伪装成"名称表里没有"。 */
async function resolveNames(symbols) {
  // 自解析路径：本脚本是 .mjs，不能 import TS 模块（Node 不认 .ts 里的类型语法）。
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const signalSystemDir = path.join(root, "..", "signal_system");
  const bridgeScript = path.join(signalSystemDir, "web_bridge.py");
  if (!fs.existsSync(bridgeScript)) {
    throw new Error(`找不到桥接脚本：${bridgeScript}`);
  }
  const payload = JSON.stringify({ symbols });
  // 镜像里 Python 只在 venv 中（PYTHON_BIN=/opt/venv/bin/python），**不在 PATH 上** ——
  // 退回到裸 "python" 会 ENOENT。容器内由 compose 提供 PYTHON_BIN。
  const pythonBin = process.env.PYTHON_BIN?.trim() || "python3";
  const out = await new Promise((resolve) => {
    const child = spawn(pythonBin, ["-B", bridgeScript, "resolve-names", "--payload", "-"], {
      cwd: signalSystemDir,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => { stdout += d; });
    child.stderr.on("data", (d) => { stderr += d; });
    child.on("close", (code) => resolve({ stdout, stderr, code }));
    child.on("error", (err) => resolve({ stdout: "", stderr: `spawn 失败: ${err.message}`, code: -1 }));
    child.stdin.end(payload);
  });
  let parsed;
  try {
    parsed = JSON.parse(out.stdout);
  } catch {
    throw new Error(
      `桥接未返回可解析的 JSON（exit=${out.code}，bin=${pythonBin}）。stdout=${out.stdout.slice(0, 400) || "(空)"} stderr=${out.stderr.slice(0, 400) || "(空)"}`,
    );
  }
  if (!parsed?.ok) {
    throw new Error(`桥接报错：${parsed?.error ?? "未知"}（stderr=${out.stderr.slice(0, 300) || "(空)"}）`);
  }
  return parsed?.data?.names ?? {};
}

async function main() {
  const client = makeClient();
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
    const unresolvedCodes = codes.filter((c) => !names[c]);
    console.log(
      JSON.stringify({ resolved_total: resolved.length, unresolved: unresolvedCodes.length }),
    );
    // 把解不出来的代码点名 —— 否则无法区分"名称表取数挂了"与"这些标的本来就不在表里"
    // （名称表刻意排除 ST / 退市整理 / 北交所 / ETF）。
    if (unresolvedCodes.length > 0) {
      console.log(JSON.stringify({ unresolved_codes: unresolvedCodes.slice(0, 40) }));
    }
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

// 仅在直接执行时跑 main —— 被 import 时（测试）不产生副作用。
const invokedDirectly =
  process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) await main();

export { resolveNames };
