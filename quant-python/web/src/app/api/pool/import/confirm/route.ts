import { NextResponse } from "next/server";
import { confirmPendingImport, getPendingImport } from "@/lib/db";
import { normalizeSymbol } from "@/lib/symbols";

export async function POST(request: Request) {
  let body: { pending_id?: unknown; candidates?: Array<{ symbol?: unknown; name?: unknown }> };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const pendingId = Number(body.pending_id);
  const pending = await getPendingImport(pendingId);
  if (!pending) {
    return NextResponse.json({ error: "待确认导入不存在" }, { status: 404 });
  }
  let candidates: Array<{ symbol: string; name: string }> = [];
  try {
    const stored = JSON.parse(pending.candidates) as { symbols?: Array<{ symbol: string; name: string }> };
    candidates = Array.isArray(stored.symbols) ? stored.symbols : [];
  } catch {
    candidates = [];
  }
  if (Array.isArray(body.candidates)) {
    candidates = body.candidates.map((item) => ({
      symbol: normalizeSymbol(String(item.symbol ?? "").trim()),
      name: String(item.name ?? "").trim(),
    }));
  }
  const valid = candidates.filter((item) => /^\d{6}\.(SH|SZ|BJ)$/.test(item.symbol));
  if (valid.length === 0) {
    return NextResponse.json({ error: "没有合法的股票代码可确认" }, { status: 422 });
  }
  const added = await confirmPendingImport(
    pendingId,
    valid.map((item) => ({ ...item, source: pending.kind === "image" ? "image" : "text" })),
  );
  return NextResponse.json({ ok: true, added });
}
