import { NextResponse } from "next/server";
import { getTotalCapital, listHoldings } from "@/lib/db";
import { correctHolding } from "@/lib/portfolio";
import { normalizeSymbol } from "@/lib/symbols";

function validSymbol(symbol: string): boolean {
  return /^\d{6}\.(SH|SZ|BJ)$/.test(symbol);
}

function toNumber(value: unknown): number {
  const parsed = Number(value);
  return value == null ? 0 : parsed;
}

export async function GET() {
  return NextResponse.json({ holdings: await listHoldings(), total_capital: await getTotalCapital() });
}

export async function POST(request: Request) {
  let body: {
    symbol?: unknown;
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
  const symbol = normalizeSymbol(String(body.symbol ?? "").trim());
  if (!validSymbol(symbol)) {
    return NextResponse.json({ error: "股票代码不合法" }, { status: 422 });
  }
  const shares = toNumber(body.shares);
  const costPrice = toNumber(body.cost_price);
  if (!Number.isFinite(shares) || !Number.isFinite(costPrice) || shares < 0 || costPrice < 0) {
    return NextResponse.json({ error: "持仓份额/持仓价不能为负数" }, { status: 422 });
  }
  try {
  const holding = await correctHolding({
    symbol,
    name: String(body.name ?? "").trim(),
    shares,
    cost_price: costPrice,
    total_amount: toNumber(body.total_amount),
  });
  return NextResponse.json({ ok: true, holding, holdings: await listHoldings() });
  } catch (error) { return NextResponse.json({ error: error instanceof Error ? error.message : "持仓保存失败" }, { status: 422 }); }
}
