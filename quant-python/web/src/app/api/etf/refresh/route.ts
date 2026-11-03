import { NextResponse } from "next/server";
import { runBridge } from "@/lib/bridge";

export const dynamic = "force-dynamic";

export async function POST() {
  const outcome = await runBridge("etf-refresh", {}, { timeoutMs: 60_000 });
  if (!outcome.ok) {
    return NextResponse.json(
      { error: outcome.error || "ETF 信号刷新失败", code: outcome.code },
      { status: 502 }
    );
  }
  return NextResponse.json(outcome.data);
}
