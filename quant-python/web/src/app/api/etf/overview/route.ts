import { NextResponse } from "next/server";
import { runBridge } from "@/lib/bridge";

export const dynamic = "force-dynamic";

export async function GET() {
  const outcome = await runBridge("etf-overview", {}, { timeoutMs: 20_000 });
  if (!outcome.ok) {
    return NextResponse.json(
      { error: outcome.error || "ETF 概览读取失败", code: outcome.code },
      { status: 502 }
    );
  }
  return NextResponse.json(outcome.data);
}
