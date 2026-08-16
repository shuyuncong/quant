import fs from "node:fs";
import path from "node:path";
import { NextResponse } from "next/server";
import { getEffectiveConfig } from "@/lib/config";
import { addNote, getDb } from "@/lib/db";
import { interpretReport, pickChatModel } from "@/lib/llm";

export async function POST(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  try {
    const effective = await getEffectiveConfig();
    const outputDir = effective.runtime.output_dir;
    if (!outputDir) return NextResponse.json({ error: "输出目录未配置" }, { status: 500 });
    const full = path.join(outputDir, id);
    if (!full.startsWith(path.resolve(outputDir)) || !fs.existsSync(full)) {
      return NextResponse.json({ error: "结果不存在" }, { status: 404 });
    }
    const profile = pickChatModel();
    if (!profile) {
      return NextResponse.json(
        { error: "未配置可用的模型（需启用且配置 API Key），无法生成 AI 解读" },
        { status: 409 }
      );
    }
    const report = fs.readFileSync(full, "utf8");
    const content = await interpretReport(profile, report);
    const db = getDb();
    const job = db
      .prepare("SELECT id FROM jobs WHERE result_path = ? ORDER BY id DESC LIMIT 1")
      .get(full) as { id: number } | undefined;
    const noteId = addNote(
      { job_id: job?.id ?? null, symbol: "", content, model: profile.name, result_path: full },
      db
    );
    return NextResponse.json({ ok: true, note_id: noteId, content, model: profile.name });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "AI 解读失败" },
      { status: 500 }
    );
  }
}
