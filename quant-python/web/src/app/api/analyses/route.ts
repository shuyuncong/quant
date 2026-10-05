import { NextResponse } from "next/server";
import { getDb } from "@/lib/db";
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
  return NextResponse.json({ records: rows.map(row => ({ ...row, id: Number(row.id), job_id: Number(row.job_id) })), total, page, page_size: 5 });
}
