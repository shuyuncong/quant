import fs from "node:fs";
import { NextResponse } from "next/server";
import { getEffectiveConfig } from "@/lib/config";
import { findJobByResultPath, listNotesByJob } from "@/lib/db";
import { resolvePathWithin } from "@/lib/paths";

export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  try {
    const effective = await getEffectiveConfig();
    const outputDir = effective.runtime.output_dir;
    if (!outputDir) return NextResponse.json({ error: "输出目录未配置" }, { status: 500 });
    const full = resolvePathWithin(outputDir, id);
    if (!full || !fs.existsSync(full) || !fs.statSync(full).isFile()) {
      return NextResponse.json({ error: "结果不存在" }, { status: 404 });
    }
    const report = JSON.parse(fs.readFileSync(full, "utf8")) as Record<string, unknown>;
    const job = await findJobByResultPath(full);
    const notes = job ? await listNotesByJob(job.id) : [];
    return NextResponse.json({ report, job_id: job?.id ?? null, notes });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "读取结果失败" },
      { status: 500 }
    );
  }
}
