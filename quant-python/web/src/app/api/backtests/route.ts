import { NextResponse } from "next/server";
import { getDb } from "@/lib/db";
import {
  backtestAvailable,
  backtestServiceUnavailable,
  BacktestValidationError,
  resolvePublicBacktestJob,
  startBacktest,
} from "@/lib/backtest-service";

export async function GET() {
  try {
    // History stays readable even when new runs are disabled.
    const rows = (await (await getDb()).query(
      "SELECT id, status, created_at, error, payload FROM quant.jobs WHERE kind='backtest' ORDER BY id DESC LIMIT 30"
    )).rows;
    return NextResponse.json({ available: backtestAvailable(), jobs: rows.map(resolvePublicBacktestJob) });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : String(error) }, { status: 500 });
  }
}

export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "请求体不是合法 JSON" }, { status: 400 });
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return NextResponse.json({ error: "请求体必须是 JSON 对象" }, { status: 400 });
  }
  if (!backtestAvailable()) {
    return NextResponse.json({ error: backtestServiceUnavailable() }, { status: 503 });
  }
  try {
    const jobId = await startBacktest(body as Record<string, unknown>);
    return NextResponse.json({ ok: true, jobId }, { status: 202 });
  } catch (error) {
    if (error instanceof BacktestValidationError) {
      return NextResponse.json({ error: error.message }, { status: 422 });
    }
    // Rule/config/database faults are server-side: never report them as bad input.
    console.error("[backtests] 创建回测失败", error);
    return NextResponse.json({ error: error instanceof Error ? error.message : String(error) }, { status: 500 });
  }
}
