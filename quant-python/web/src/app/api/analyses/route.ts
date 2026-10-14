import { NextResponse } from "next/server";
import { getDb } from "@/lib/db";

/**
 * 列表只返回摘要：把 document 里的 report / portfolio 剥掉。
 *
 * 单条 document 实测 ~129KB（`results[0].timeframes` 118KB），列表按 5 条翻页若整份返回，
 * 每页 ~650KB，且 10 分钟静默轮询还会重传一次；网络稍慢时翻页看起来就像"点了没反应"。
 * 列表实际只用到 strategies 与阶段状态，正文由 GET /api/analyses/:id 按需提供。
 */
function toSummary(row: Record<string, unknown>) {
  const document = { ...((row.document ?? {}) as Record<string, unknown>) };
  // 用 delete 而不是解构重命名：解构残留的 _report/_portfolio 会触发 no-unused-vars。
  delete document.report;
  delete document.portfolio;
  return {
    ...row,
    id: Number(row.id),
    job_id: Number(row.job_id),
    document,
  };
}

export async function GET(request: Request) {
  const search = new URL(request.url).searchParams;
  const page = Math.min(1_000_000, Math.max(1, Math.floor(Number(search.get("page")) || 1)));
  const jobId = Number(search.get("job_id")) || null;
  if (jobId !== null && (!Number.isSafeInteger(jobId) || jobId < 1)) return NextResponse.json({ error: "任务编号无效" }, { status: 422 });
  const symbol = search.get("symbol")?.trim() || null;
  const where = "WHERE ($1::bigint IS NULL OR job_id=$1) AND ($2::text IS NULL OR symbol LIKE '%' || $2 || '%')";
  const db = await getDb();
  const total = Number((await db.query(`SELECT COUNT(*) FROM quant.stock_analyses ${where}`, [jobId, symbol])).rows[0].count);
  const rows = (await db.query(`SELECT * FROM quant.stock_analyses ${where} ORDER BY id DESC LIMIT 5 OFFSET $3`, [jobId, symbol, (page-1)*5])).rows;
  return NextResponse.json({ records: rows.map(toSummary), total, page, page_size: 5 });
}
