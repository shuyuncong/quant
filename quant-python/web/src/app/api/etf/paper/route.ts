import { NextResponse } from "next/server";
import { runBridge } from "@/lib/bridge";

export const dynamic = "force-dynamic";

export async function GET() {
  const outcome = await runBridge("etf-paper", { action: "status" }, { timeoutMs: 20_000 });
  if (!outcome.ok) {
    return NextResponse.json(
      { error: outcome.error || "ETF 纸面账本读取失败", code: outcome.code },
      { status: 502 }
    );
  }
  return NextResponse.json(outcome.data);
}

export async function POST(request: Request) {
  let body: { action?: unknown; capital?: unknown };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const action = String(body.action ?? "");
  if (action !== "init" && action !== "rebalance" && action !== "mark") {
    return NextResponse.json({ error: "action 必须是 init、rebalance 或 mark" }, { status: 422 });
  }
  const capital = Number(body.capital ?? 250_000);
  if (action === "init" && (!Number.isFinite(capital) || capital <= 0)) {
    return NextResponse.json({ error: "初始资金必须大于 0" }, { status: 422 });
  }
  const outcome = await runBridge(
    "etf-paper",
    { action, capital },
    { timeoutMs: 30_000 }
  );
  if (!outcome.ok) {
    return NextResponse.json(
      { error: outcome.error || "ETF 纸面账本操作失败", code: outcome.code },
      { status: 502 }
    );
  }
  return NextResponse.json(outcome.data);
}
