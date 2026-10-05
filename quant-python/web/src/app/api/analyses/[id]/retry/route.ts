import { NextResponse } from "next/server";
import { getAnalysis, runAnalysisStages, refreshAnalysisSummary } from "@/lib/analysis-service";
import { getDb } from "@/lib/db";
export async function POST(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const id = Number((await params).id);
  if (!Number.isSafeInteger(id) || id<=0) return NextResponse.json({ error: "记录 ID 无效" }, { status: 400 });
  const record = await getAnalysis(id);
  if (!record) return NextResponse.json({ error: "记录不存在" }, { status: 404 });
  if (record.status === "success") return NextResponse.json({ ok: true, reused: true });
  // Persist retry intent so a busy LLM worker or restart cannot discard the request.
  await (await getDb()).query("UPDATE quant.stock_analyses SET status='pending' WHERE id=$1 AND status='partial_failed'", [id]);
  void runAnalysisStages(id).then(()=>refreshAnalysisSummary(record.job_id)).catch(error => console.error("[analysis-retry]", error));
  return NextResponse.json({ ok: true }, { status: 202 });
}
