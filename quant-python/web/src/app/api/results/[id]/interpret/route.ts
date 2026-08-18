import fs from "node:fs";
import { NextResponse } from "next/server";
import { getEffectiveConfig } from "@/lib/config";
import { addNote, addOperationLog, getDb } from "@/lib/db";
import { interpretReport, pickChatModel } from "@/lib/llm";
import { buildHoldingsContext, holdingsFromJobPayload, totalCapitalFromJobPayload } from "@/lib/holdings-context";
import { resolvePathWithin } from "@/lib/paths";

export async function POST(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  try {
    const effective = await getEffectiveConfig();
    const outputDir = effective.runtime.output_dir;
    if (!outputDir) return NextResponse.json({ error: "输出目录未配置" }, { status: 500 });
    const full = resolvePathWithin(outputDir, id);
    if (!full || !fs.existsSync(full) || !fs.statSync(full).isFile()) {
      return NextResponse.json({ error: "结果不存在" }, { status: 404 });
    }
    const profile = pickChatModel();
    if (!profile) {
      addOperationLog({
        job_id: null,
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
    const db = getDb();
    const job = db
      .prepare("SELECT id, payload FROM jobs WHERE result_path = ? ORDER BY id DESC LIMIT 1")
      .get(full) as { id: number; payload: string } | undefined;
    const holdings = holdingsFromJobPayload(job?.payload);
    const totalCapital = totalCapitalFromJobPayload(job?.payload);
    const context = buildHoldingsContext(report, holdings, totalCapital);
    const content = await interpretReport(profile, report, context);
    const noteId = addNote(
      { job_id: job?.id ?? null, symbol: "", content, model: profile.name, result_path: full },
      db
    );
    addOperationLog({
      job_id: job?.id ?? null,
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
