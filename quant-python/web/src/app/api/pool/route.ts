import { NextResponse } from "next/server";
import { addPoolSymbols, listPool } from "@/lib/db";
import { normalizeSymbol } from "@/lib/symbols";

function validSymbol(symbol: string): boolean {
  return /^\d{6}\.(SH|SZ|BJ)$/.test(symbol);
}

export async function GET() {
  return NextResponse.json({ pool: await listPool() });
}

export async function POST(request: Request) {
  let body: { items?: Array<{ symbol?: unknown; name?: unknown; source?: unknown }> };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const items = Array.isArray(body.items) ? body.items : [];
  if (items.length === 0) {
    return NextResponse.json({ error: "缺少 items" }, { status: 422 });
  }
  const normalized = items
    .map((item) => ({
      symbol: normalizeSymbol(String(item.symbol ?? "").trim()),
      name: String(item.name ?? "").trim(),
      source: String(item.source ?? "manual").trim() || "manual",
    }))
    .filter((item) => validSymbol(item.symbol));
  if (normalized.length === 0) {
    return NextResponse.json({ error: "没有合法的股票代码" }, { status: 422 });
  }
  const added = await addPoolSymbols(normalized);
  return NextResponse.json({ ok: true, added, pool: await listPool() });
}
