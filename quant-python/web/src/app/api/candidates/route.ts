import { NextResponse } from "next/server";
import { runBridge } from "@/lib/bridge";

export interface CandidateRow {
  symbol: string;
  name: string;
  score: number;
  confirmed_at?: string;
  dif?: number;
  dea?: number;
  zero_distance?: number;
  chan_signals?: unknown[];
}

export async function GET() {
  try {
    const outcome = await runBridge("candidates", {}, { timeoutMs: 60_000 });
    if (!outcome.ok) {
      return NextResponse.json(
        { error: outcome.error || "读取指标股票池失败" },
        { status: 500 }
      );
    }
    return NextResponse.json(outcome.data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "读取指标股票池失败" },
      { status: 500 }
    );
  }
}
