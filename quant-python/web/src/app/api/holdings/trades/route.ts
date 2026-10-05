import { NextResponse } from "next/server";
import { listHoldingTrades, recordHoldingTrade } from "@/lib/portfolio";
import type { TradeInput } from "@/lib/trade-math";
export async function GET(request: Request) {
  try { return NextResponse.json({ trades: await listHoldingTrades(new URL(request.url).searchParams.get("symbol") ?? undefined) }); }
  catch (error) { return NextResponse.json({ error: error instanceof Error ? error.message : "读取成交失败" }, { status: 400 }); }
}
export async function POST(request: Request) {
  try {
    const input = await request.json() as TradeInput;
    const result = await recordHoldingTrade(input);
    return NextResponse.json({ ok: true, ...result });
  } catch (error) { return NextResponse.json({ error: error instanceof Error ? error.message : "登记成交失败" }, { status: 422 }); }
}
