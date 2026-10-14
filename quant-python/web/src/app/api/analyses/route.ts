import { NextResponse } from "next/server";
import { getDb } from "@/lib/db";
import { normalizeSymbol } from "@/lib/symbols";

/**
 * 列表只返回摘要：在 SQL 层就把 document 里的大字段剥掉。
 *
 * 单条 document 实测 ~129KB，其中 `report.results[0].timeframes`（1m/5m/15m/30m/60m/120m）
 * 占 118KB；列表按 5 条翻页若整份取回，每页 ~0.6–1.2MB，且 10 分钟静默轮询会再重传一遍。
 * 更大的问题是 `SELECT *` 会让 Postgres 把整份 document（含正文）读进内存并传给应用，
 * 一旦某条记录的正文特别大，接口就可能在被拖垮后返回 500
 * （线上页面报 "Unexpected token 'I', "Internal S"... is not valid JSON" 即此：
 * 500 的响应体是文本，不是 JSON）。
 *
 * 所以这里用 jsonb_build_object 只挑五页签真正需要的键，正文从不离开数据库。
 */
const SUMMARY_DOCUMENT = `jsonb_build_object(
         'schema_version', s.document->'schema_version',
         'symbol', s.document->'symbol',
         'name', s.document->'name',
         'as_of', s.document->'as_of',
         'snapshot_hash', s.document->'snapshot_hash',
         'revision', s.document->'revision',
         'strategies', s.document->'strategies',
         'technical', s.document->'technical',
         'synthesis', s.document->'synthesis'
       )`;

/** 名称表：只在这两张表里找，表名是字面量，不来自请求。 */
const NAME_TABLES = ["quant.stock_pool", "quant.holdings"] as const;

const LIST_PAGE_SIZE = 5;

/** 列表查询只依赖 stock_analyses；名称回退另走查询，失败也不影响列表。 */
const LIST_QUERY = (where: string) => `SELECT s.id, s.job_id, s.symbol, s.name, s.status, s.created_at, s.updated_at,
            ${SUMMARY_DOCUMENT} AS document
     FROM quant.stock_analyses s ${where} ORDER BY s.id DESC LIMIT ${LIST_PAGE_SIZE} OFFSET $3`;

type AnalysisRow = {
  id: unknown;
  job_id: unknown;
  symbol: string;
  name: string;
  document: Record<string, unknown>;
  [key: string]: unknown;
};

/**
 * 归一化成 6 位代码，用于跨表匹配名称。
 *
 * 各表存储格式不统一（`605296` / `605296.SH` / `SH605296` 都出现过），直接相等比较会静默匹配不上。
 * 这里的取值范围与 `normalizeSymbol` 一致，只是它的输出带交易所后缀、这里只要代码本身。
 */
function bareCode(value: unknown): string {
  return normalizeSymbol(String(value ?? "")).split(".")[0];
}

/**
 * 名称回退的优先级（只填空，不覆盖）：
 *   1. 记录自带 `stock_analyses.name`
 *   2. **该记录所属任务的 `payload.symbol_names`** —— 同一批记录自己携带的名称。
 *      分析批次在 `startAnalysisBatch` 建任务后就解析并写入（`persistSymbolNames`），
 *      覆盖该批全部标的，所以它比池/持仓更贴近这条记录的来源。
 *   3. `quant.stock_pool` / `quant.holdings`
 *   4. 都没有就留空（列表退回显示代码）
 *
 * 全部 best-effort：名称只是补充信息，任何一步失败都不影响列表本身。
 */
async function fillMissingNames(
  db: { query: (text: string, values?: readonly unknown[]) => Promise<{ rows: Array<Record<string, unknown>> }> },
  rows: AnalysisRow[],
): Promise<void> {
  const missing = rows.filter((row) => !String(row.name ?? "").trim());
  if (missing.length === 0) return;
  const codes = new Set(missing.map((row) => bareCode(row.symbol)).filter(Boolean));
  if (codes.size === 0) return;

  const found = new Map<string, string>();
  const collect = (candidates: Map<string, string>) => {
    for (const [code, name] of candidates) {
      if (codes.has(code) && !found.has(code)) found.set(code, name);
    }
  };

  const jobIds = [...new Set(missing.map((row) => Number(row.job_id)).filter(Number.isSafeInteger))];
  try {
    collect(await lookupJobNames(db, jobIds));
  } catch {
    // 任务表/字段不可用时跳过，继续尝试池与持仓。
  }

  const wanted = [...codes];
  for (const table of NAME_TABLES) {
    try {
      collect(await lookupTableNames(db, table, wanted));
    } catch {
      // 名称是补充信息：该表缺失/结构不同时静默跳过，不影响列表本身。
    }
  }

  for (const row of rows) {
    if (String(row.name ?? "").trim()) continue;
    const name = found.get(bareCode(row.symbol));
    if (name) row.name = name;
  }
}

/** 从任务 payload 的 `symbol_names` 取名称；键在库里可能是纯代码或带交易所后缀，统一归一。 */
async function lookupJobNames(
  db: { query: (text: string, values?: readonly unknown[]) => Promise<{ rows: Array<Record<string, unknown>> }> },
  jobIds: number[],
): Promise<Map<string, string>> {
  const found = new Map<string, string>();
  if (jobIds.length === 0) return found;
  // `quant.jobs.payload` 是 TEXT（不是 jsonb），所以取原文后在 JS 里解析 ——
  // 与 /api/jobs 的既有做法一致；在 SQL 里对它用 `->` 会报 "operator does not exist: text -> unknown"。
  const result = await db.query("SELECT payload FROM quant.jobs WHERE id = ANY($1::bigint[])", [jobIds]);
  for (const row of result.rows) {
    let names: unknown;
    try {
      const parsed = JSON.parse(String(row.payload ?? ""));
      names = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>).symbol_names : undefined;
    } catch {
      continue; // payload 不是合法 JSON 就跳过这条任务
    }
    if (!names || typeof names !== "object" || Array.isArray(names)) continue;
    for (const [key, value] of Object.entries(names as Record<string, unknown>)) {
      const code = bareCode(key);
      const name = String(value ?? "").trim();
      if (code && name && !found.has(code)) found.set(code, name);
    }
  }
  return found;
}

/** 从池/持仓按归一化代码取名；表名是字面量，不来自请求。 */
async function lookupTableNames(
  db: { query: (text: string, values?: readonly unknown[]) => Promise<{ rows: Array<Record<string, unknown>> }> },
  table: (typeof NAME_TABLES)[number],
  codes: string[],
): Promise<Map<string, string>> {
  const found = new Map<string, string>();
  if (codes.length === 0) return found;
  const result = await db.query(
    `SELECT regexp_replace(regexp_replace(upper(symbol), '^(SH|SZ|BJ)\\.?', ''), '\\.[A-Z]{2,3}$', '') AS code,
            MAX(BTRIM(name)) AS name
       FROM ${table}
      WHERE regexp_replace(regexp_replace(upper(symbol), '^(SH|SZ|BJ)\\.?', ''), '\\.[A-Z]{2,3}$', '') = ANY($1::text[])
        AND BTRIM(name) <> ''
      GROUP BY 1`,
    [codes],
  );
  for (const row of result.rows) {
    const code = String(row.code ?? "");
    const name = String(row.name ?? "").trim();
    if (code && name && !found.has(code)) found.set(code, name);
  }
  return found;
}

export async function GET(request: Request) {
  const search = new URL(request.url).searchParams;
  const page = Math.min(1_000_000, Math.max(1, Math.floor(Number(search.get("page")) || 1)));
  const jobId = Number(search.get("job_id")) || null;
  if (jobId !== null && (!Number.isSafeInteger(jobId) || jobId < 1)) return NextResponse.json({ error: "任务编号无效" }, { status: 422 });
  const symbol = search.get("symbol")?.trim() || null;
  const where = "WHERE ($1::bigint IS NULL OR s.job_id=$1) AND ($2::text IS NULL OR s.symbol LIKE '%' || $2 || '%')";

  // 任何失败都要以 JSON 返回：路由未捕获异常时响应体是文本 "Internal Server Error"，
  // 前端 response.json() 会抛出 "Unexpected token 'I'..." 这种看不出所以然的错。
  try {
    const db = await getDb();
    const total = Number((await db.query(`SELECT COUNT(*) FROM quant.stock_analyses s ${where}`, [jobId, symbol])).rows[0].count);
    const rows = (await db.query(LIST_QUERY(where), [jobId, symbol, (page - 1) * LIST_PAGE_SIZE])).rows as unknown as AnalysisRow[];
    const records = rows.map((row) => ({ ...row, id: Number(row.id), job_id: Number(row.job_id) }));
    await fillMissingNames(db, records);
    return NextResponse.json({ records, total, page, page_size: LIST_PAGE_SIZE });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return NextResponse.json({ error: `读取分析记录失败：${detail}` }, { status: 500 });
  }
}
