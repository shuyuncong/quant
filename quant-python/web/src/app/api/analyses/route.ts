import { NextResponse } from "next/server";
import { getDb } from "@/lib/db";

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

/**
 * 归一化成 6 位代码，用于跨表匹配名称。
 *
 * 各表存储格式不统一（`605296` / `605296.SH` / `SH605296` 都出现过），直接相等比较会静默匹配不上，
 * 所以两边都去掉交易所前缀与后缀后再比。
 */
function bareCode(column: string): string {
  return `regexp_replace(regexp_replace(upper(${column}), '^(SH|SZ|BJ)\\.?', ''), '\\.[A-Z]{2,3}$', '')`;
}

/**
 * 名称回退：记录自带 name 为空时，用品池/持仓里的名称兜底。
 * 子查询里必须写 `s.symbol` —— 只写 `symbol` 会绑定到内层表的同名列（p.symbol = p.symbol），
 * 子查询返回多行，Postgres 直接报 21000。
 */
const NAME_FALLBACK = `COALESCE(
         NULLIF(BTRIM(s.name), ''),
         (SELECT p.name FROM quant.stock_pool p
           WHERE ${bareCode("p.symbol")} = ${bareCode("s.symbol")} AND BTRIM(p.name) <> '' LIMIT 1),
         (SELECT h.name FROM quant.holdings h
           WHERE ${bareCode("h.symbol")} = ${bareCode("s.symbol")} AND BTRIM(h.name) <> '' LIMIT 1),
         ''
       )`;

export async function GET(request: Request) {
  const search = new URL(request.url).searchParams;
  const page = Math.min(1_000_000, Math.max(1, Math.floor(Number(search.get("page")) || 1)));
  const jobId = Number(search.get("job_id")) || null;
  if (jobId !== null && (!Number.isSafeInteger(jobId) || jobId < 1)) return NextResponse.json({ error: "任务编号无效" }, { status: 422 });
  const symbol = search.get("symbol")?.trim() || null;
  const where = "WHERE ($1::bigint IS NULL OR s.job_id=$1) AND ($2::text IS NULL OR s.symbol LIKE '%' || $2 || '%')";
  const db = await getDb();
  const total = Number((await db.query(`SELECT COUNT(*) FROM quant.stock_analyses s ${where}`, [jobId, symbol])).rows[0].count);
  const rows = (await db.query(
    `SELECT s.id, s.job_id, s.symbol, ${NAME_FALLBACK} AS name, s.status, s.created_at, s.updated_at,
            ${SUMMARY_DOCUMENT} AS document
     FROM quant.stock_analyses s ${where} ORDER BY s.id DESC LIMIT 5 OFFSET $3`,
    [jobId, symbol, (page-1)*5],
  )).rows;
  return NextResponse.json({
    records: rows.map((row) => ({ ...row, id: Number(row.id), job_id: Number(row.job_id) })),
    total,
    page,
    page_size: 5,
  });
}
