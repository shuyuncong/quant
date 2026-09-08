import fs from "node:fs";
import { NextResponse } from "next/server";
import { getJob } from "@/lib/db";
import { compactDataSource } from "@/lib/stock-report";

export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  try {
    const jobId = Number(id);
    if (!Number.isInteger(jobId) || jobId <= 0) {
      return NextResponse.json({ error: "任务 ID 无效" }, { status: 400 });
    }
    const job = await getJob(jobId);
    if (!job) return NextResponse.json({ error: "任务不存在" }, { status: 404 });
    if (!job.result_path) {
      return NextResponse.json({ error: "该任务没有结果文件" }, { status: 409 });
    }
    const full = job.result_path;
    if (!full.endsWith(".json") || !fs.existsSync(full) || !fs.statSync(full).isFile()) {
      return NextResponse.json({ error: `结果文件不存在：${full}` }, { status: 404 });
    }
    const report = JSON.parse(fs.readFileSync(full, "utf8")) as Record<string, unknown>;
    return NextResponse.json(compactDataSource(report));
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "读取数据源失败" },
      { status: 500 }
    );
  }
}
