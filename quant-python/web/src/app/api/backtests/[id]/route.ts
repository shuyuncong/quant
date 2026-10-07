import path from "node:path";
import { NextResponse } from "next/server";
import { getJob, getDb } from "@/lib/db";
import { backtestAvailable, backtestServiceUnavailable, readBacktestFile, resolvePublicBacktestJob } from "@/lib/backtest-service";

/** The worker reads options.output_dir from the frozen payload; the report file lives beside it. */
function outputDirOf(payload: string): string | null {
  try {
    const parsed: unknown = JSON.parse(payload);
    if (!parsed || typeof parsed !== "object") return null;
    const options = Reflect.get(parsed, "options");
    if (!options || typeof options !== "object") return null;
    const dir = Reflect.get(options, "output_dir");
    return typeof dir === "string" ? dir : null;
  } catch {
    return null;
  }
}

export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  let id: number;
  try {
    id = Number((await params).id);
    if (!Number.isInteger(id)) throw new Error("bad id");
  } catch {
    return NextResponse.json({ error: "回测不存在" }, { status: 404 });
  }
  try {
    const job = await getJob(id);
    if (!job || job.kind !== "backtest") return NextResponse.json({ error: "回测不存在" }, { status: 404 });
    const outputDir = outputDirOf(job.payload);
    const progress = outputDir && job.status !== "pending"
      ? await readBacktestFile(path.join(outputDir, "progress.json")).catch(() => null)
      : null;
    const report = job.result_path ? await readBacktestFile(job.result_path) : null;
    return NextResponse.json({ job: resolvePublicBacktestJob(job), progress, report });
  } catch (error) {
    // A damaged report file must surface as a failure, never as the previous task's numbers.
    console.error("[backtests] 读取回测失败", error);
    return NextResponse.json({ error: error instanceof Error ? error.message : String(error) }, { status: 500 });
  }
}

export async function POST(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  if (!backtestAvailable()) {
    return NextResponse.json({ error: backtestServiceUnavailable() }, { status: 503 });
  }
  let id: number;
  try {
    id = Number((await params).id);
    if (!Number.isInteger(id)) throw new Error("bad id");
  } catch {
    return NextResponse.json({ error: "回测不存在" }, { status: 404 });
  }
  try {
    const job = await getJob(id);
    if (!job || job.kind !== "backtest") return NextResponse.json({ error: "回测不存在" }, { status: 404 });
    // Retry reuses the same frozen inputs and rule snapshot; the worker picks it up.
    const claimed = await (await getDb()).query(
      "UPDATE quant.jobs SET status='pending', error=NULL, finished_at=NULL WHERE id=$1 AND status='failed' RETURNING id",
      [id]
    );
    if (!claimed.rowCount) return NextResponse.json({ error: "仅失败的线上回测可重试" }, { status: 409 });
    return NextResponse.json({ ok: true }, { status: 202 });
  } catch (error) {
    console.error("[backtests] 重试失败", error);
    return NextResponse.json({ error: error instanceof Error ? error.message : String(error) }, { status: 500 });
  }
}
