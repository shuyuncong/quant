import { NextResponse } from "next/server";
import { runBridge } from "@/lib/bridge";

export interface CandidateRow {
  symbol: string;
  name: string;
  score: number;
  pool_type?: "macd_zero_axis" | "yearline_pullback" | "all";
  strategy_score?: number;
  confirmed_at?: string;
  dif?: number;
  dea?: number;
  zero_distance?: number;
  golden_cross_zone?: "above" | "near" | "below";
  golden_cross_zone_label?: string;
  confirmation_items?: string[];
  chan_signals?: unknown[];
}

const POOL_TYPES = new Set(["macd_zero_axis", "yearline_pullback", "all"]);

export async function GET(request: Request) {
  const poolType = new URL(request.url).searchParams.get("pool_type") || "macd_zero_axis";
  if (!POOL_TYPES.has(poolType)) {
    return NextResponse.json(
      { error: "pool_type 仅支持 macd_zero_axis / yearline_pullback / all" },
      { status: 422 }
    );
  }
  try {
    const outcome = await runBridge(
      "candidates",
      { pool_type: poolType },
      { timeoutMs: 60_000 }
    );
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
