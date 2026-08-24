import fs from "node:fs";
import { NextResponse } from "next/server";
import { addNote, addOperationLog, getJob, listNotesByJob } from "@/lib/db";
import { interpretReport, pickChatModel } from "@/lib/llm";
import {
  buildHoldingsContext,
  holdingsFromJobPayload,
  totalCapitalFromJobPayload,
} from "@/lib/holdings-context";

export async function POST(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  try {
    const jobId = Number(id);
    if (!Number.isInteger(jobId) || jobId <= 0) {
      return NextResponse.json({ error: "任务 ID 无效" }, { status: 400 });
    }
    const job = await getJob(jobId);
    if (!job) return NextResponse.json({ error: "任务不存在" }, { status: 404 });
    if (job.status !== "success" || !job.result_path) {
      return NextResponse.json(
        { error: "任务尚未成功，没有可解读的结果文件" },
        { status: 409 }
      );
    }
    const full = job.result_path;
    if (!full.endsWith(".json") || !fs.existsSync(full) || !fs.statSync(full).isFile()) {
      return NextResponse.json({ error: `结果文件不存在：${full}` }, { status: 404 });
    }
    const existing = (await listNotesByJob(jobId))[0];
    if (existing) {
      return NextResponse.json({
        ok: true,
        note_id: existing.id,
        content: existing.content,
        model: existing.model,
      });
    }
    const profile = await pickChatModel();
    if (!profile) {
      await addOperationLog({
        job_id: jobId,
        level: "warning",
        module: "interpret",
        message: "手动 AI 解读失败：未配置可用的模型",
        detail: "需要启用模型并配置 API Key",
      });
      return NextResponse.json(
        { error: "未配置可用的模型（需启用且配置 API Key），无法生成 AI 解读" },
        { status: 409 }
      );
    }
    const report = fs.readFileSync(full, "utf8");
    const holdings = holdingsFromJobPayload(job.payload);
    const totalCapital = totalCapitalFromJobPayload(job.payload);
    const context = buildHoldingsContext(report, holdings, totalCapital);
    const content = await interpretReport(profile, report, context);
    const noteId = await addNote({
      job_id: jobId,
      symbol: "",
      content,
      model: profile.name,
      result_path: full,
    });
    await addOperationLog({
      job_id: jobId,
      level: "info",
      module: "interpret",
      message: "手动 AI 解读完成",
      detail: `模型 ${profile.name}，笔记 #${noteId}`,
    });
    return NextResponse.json({ ok: true, note_id: noteId, content, model: profile.name });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "AI 解读失败" },
      { status: 500 }
    );
  }
}
