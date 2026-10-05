import { NextResponse } from "next/server";
import { correctHolding } from "@/lib/portfolio";
import { normalizeSymbol } from "@/lib/symbols";

function toNumber(value: unknown): number {
  const parsed = Number(value);
  return value == null ? 0 : parsed;
}

export async function PUT(request: Request, { params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = await params;
  let body: {
    name?: unknown;
    shares?: unknown;
    cost_price?: unknown;
    total_amount?: unknown;
  };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const normalized = normalizeSymbol(decodeURIComponent(symbol));
  try {
  const holding = await correctHolding({
    symbol: normalized,
    name: String(body.name ?? "").trim(),
    shares: toNumber(body.shares),
    cost_price: toNumber(body.cost_price),
    total_amount: toNumber(body.total_amount),
  });
  return NextResponse.json({ ok: true, holding });
  } catch (error) { return NextResponse.json({ error: error instanceof Error ? error.message : "持仓保存失败" }, { status: 422 }); }
}

export async function DELETE(_request: Request, { params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = await params;
  await correctHolding({ symbol: decodeURIComponent(symbol), shares: 0 });
  return NextResponse.json({ ok: true });
}
