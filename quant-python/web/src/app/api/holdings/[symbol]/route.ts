import { NextResponse } from "next/server";
import { removeHolding, upsertHolding } from "@/lib/db";
import { normalizeSymbol } from "@/lib/symbols";

function toNumber(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
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
  const holding = upsertHolding({
    symbol: normalized,
    name: String(body.name ?? "").trim(),
    shares: toNumber(body.shares),
    cost_price: toNumber(body.cost_price),
    total_amount: toNumber(body.total_amount),
  });
  return NextResponse.json({ ok: true, holding });
}

export async function DELETE(_request: Request, { params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = await params;
  removeHolding(decodeURIComponent(symbol));
  return NextResponse.json({ ok: true });
}
