import { NextResponse } from "next/server";
import { runBridge } from "@/lib/bridge";
import { createPendingImport } from "@/lib/db";

export async function POST(request: Request) {
  let body: { text?: unknown };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const text = String(body.text ?? "").trim();
  if (!text) {
    return NextResponse.json({ error: "请输入股票列表文本" }, { status: 422 });
  }
  if (text.length > 200_000) {
    return NextResponse.json({ error: "文本过长（最多 20 万字符）" }, { status: 422 });
  }
  const outcome = await runBridge("normalize", { text }, { timeoutMs: 30_000 });
  if (!outcome.ok || !outcome.data) {
    return NextResponse.json({ error: outcome.error || "解析失败" }, { status: 500 });
  }
  const data = outcome.data as { symbols: Array<{ symbol: string; name: string }>; unknown: string[]; raw_lines: string[] };
  const pendingId = await createPendingImport("text", text, { symbols: data.symbols, unknown: data.unknown });
  return NextResponse.json({
    ok: true,
    pending_id: pendingId,
    symbols: data.symbols,
    unknown: data.unknown,
    raw_lines: data.raw_lines,
  });
}
