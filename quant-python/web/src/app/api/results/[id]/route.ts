import fs from "node:fs";
import path from "node:path";
import { NextResponse } from "next/server";
import { getEffectiveConfig } from "@/lib/config";
import { getDb, listNotesByJob } from "@/lib/db";

export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  try {
    const effective = await getEffectiveConfig();
    const outputDir = effective.runtime.output_dir;
    if (!outputDir) return NextResponse.json({ error: "输出目录未配置" }, { status: 500 });
    const full = path.join(outputDir, id);
    if (!full.startsWith(path.resolve(outputDir)) || !fs.existsSync(full)) {
      return NextResponse.json({ error: "结果不存在" }, { status: 404 });
    }
    const report = JSON.parse(fs.readFileSync(full, "utf8")) as Record<string, unknown>;
    const db = getDb();
    const job = db
      .prepare("SELECT id FROM jobs WHERE result_path = ? ORDER BY id DESC LIMIT 1")
      .get(full) as { id: number } | undefined;
    const notes = job ? listNotesByJob(job.id, db) : [];
    return NextResponse.json({ report, job_id: job?.id ?? null, notes });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "读取结果失败" },
      { status: 500 }
    );
  }
}
