import { NextResponse } from "next/server";
import { removePoolSymbol, updatePoolSymbol } from "@/lib/db";

export async function PUT(request: Request, { params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = await params;
  let body: { name?: unknown };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  updatePoolSymbol(decodeURIComponent(symbol), String(body.name ?? ""));
  return NextResponse.json({ ok: true });
}

export async function DELETE(_request: Request, { params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = await params;
  removePoolSymbol(decodeURIComponent(symbol));
  return NextResponse.json({ ok: true });
}
