import fs from "node:fs";
import { NextResponse } from "next/server";
import { addNote, addOperationLog, getJob, listNotesByJob } from "@/lib/db";
import { pickChatModel, reviewCandidatesWithFallback } from "@/lib/llm";
import {
  buildHoldingsContext,
  historyCompleteFromJobPayload,
  holdingsFromJobPayload,
  totalCapitalFromJobPayload,
  tradesFromJobPayload,
} from "@/lib/holdings-context";

const REVIEW_KIND = "candidate_review";
const SCAN_KINDS: Record<string, true> = { scan: true, "daily-scan": true };

async function existingReview(jobId: number) {
  const notes = await listNotesByJob(jobId, undefined, REVIEW_KIND);
  return notes[0] ?? null;
}

function invalidId(value: string): boolean {
  const id = Number(value);
  return !Number.isInteger(id) || id <= 0;
}

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  if (invalidId(id)) {
    return NextResponse.json({ error: "任务 ID 无效" }, { status: 400 });
  }
  const jobId = Number(id);
  const job = await getJob(jobId);
  if (!job) return NextResponse.json({ error: "任务不存在" }, { status: 404 });
  if (!SCAN_KINDS[job.kind]) {
    return NextResponse.json({ error: "候选复核仅支持日线扫描任务" }, { status: 422 });
  }
  const note = await existingReview(jobId);
  return NextResponse.json({
    ok: true,
    note: note
      ? {
          id: note.id,
          content: note.content,
          model: note.model,
          created_at: note.created_at,
        }
      : null,
  });
}

export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  if (invalidId(id)) {
    return NextResponse.json({ error: "任务 ID 无效" }, { status: 400 });
  }
  const jobId = Number(id);
  try {
    const body = (await request.json().catch(() => ({}))) as { regenerate?: unknown };
    const regenerate = body.regenerate === true;
    const job = await getJob(jobId);
    if (!job) return NextResponse.json({ error: "任务不存在" }, { status: 404 });
    if (!SCAN_KINDS[job.kind]) {
      return NextResponse.json({ error: "候选复核仅支持日线扫描任务" }, { status: 422 });
    }
    if (job.status !== "success" || !job.result_path) {
      return NextResponse.json(
        { error: "任务尚未成功，没有可复核的扫描结果" },
        { status: 409 }
      );
    }
    const full = job.result_path;
    if (!full.endsWith(".json") || !fs.existsSync(full) || !fs.statSync(full).isFile()) {
      return NextResponse.json({ error: `结果文件不存在：${full}` }, { status: 404 });
    }

    const existing = await existingReview(jobId);
    if (existing && !regenerate) {
      return NextResponse.json({
        ok: true,
        note_id: existing.id,
        content: existing.content,
        model: existing.model,
        created_at: existing.created_at,
      });
    }

    const profile = await pickChatModel();
    if (!profile) {
      await addOperationLog({
        job_id: jobId,
        level: "warning",
        module: "review",
        message: "候选复核失败：未配置可用的模型",
        detail: "需要启用模型并配置 API Key",
      });
      return NextResponse.json(
        { error: "未配置可用的模型（需启用且配置 API Key），无法生成候选复核" },
        { status: 409 }
      );
    }

    const report = fs.readFileSync(full, "utf8");
    const context = buildHoldingsContext(report, {
      holdings: holdingsFromJobPayload(job.payload),
      trades: tradesFromJobPayload(job.payload),
      totalCapital: totalCapitalFromJobPayload(job.payload),
      historyComplete: historyCompleteFromJobPayload(job.payload),
    });
    const content = await reviewCandidatesWithFallback(report, context);
    const noteId = await addNote({
      job_id: jobId,
      symbol: "",
      content: content.content,
      model: content.model.name,
      result_path: full,
      kind: REVIEW_KIND,
    });
    await addOperationLog({
      job_id: jobId,
      level: "info",
      module: "review",
      message: "候选复核完成",
      detail: `模型 ${content.model.name}，笔记 #${noteId}`,
    });
    return NextResponse.json({
      ok: true,
      note_id: noteId,
      content: content.content,
      model: content.model.name,
      created_at: new Date().toISOString(),
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "候选复核失败" },
      { status: 500 }
    );
  }
}
