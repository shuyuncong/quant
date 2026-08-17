import { NextResponse } from "next/server";
import { startJob, type JobKind } from "@/lib/jobs";
import { listHoldings } from "@/lib/db";

const VALID_KINDS = new Set<JobKind>([
  "analyze",
  "scan",
  "monitor-once",
  "test-notify",
  "dispatch-outbox",
]);

export async function POST(request: Request) {
  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const kind = String(body.kind ?? "") as JobKind;
  if (!VALID_KINDS.has(kind)) {
    return NextResponse.json({ error: "不支持的任务类型" }, { status: 422 });
  }
  if (kind === "analyze") {
    const symbols = Array.isArray(body.symbols) ? (body.symbols as unknown[]).map(String) : [];
    if (symbols.length === 0) {
      return NextResponse.json({ error: "analyze 需要至少一个股票代码" }, { status: 422 });
    }
  }
  const payload: Record<string, unknown> = {
    notify: body.notify !== false,
  };
  if (kind === "scan") {
    const universeMode = String(body.universe_mode ?? "");
    if (universeMode && !["watchlist", "all_a"].includes(universeMode)) {
      return NextResponse.json(
        { error: "scan 的 universe_mode 仅支持 watchlist / all_a" },
        { status: 422 }
      );
    }
    if (universeMode) payload.overrides = { scan: { universe_mode: universeMode } };
  }
  if (Array.isArray(body.symbols)) payload.symbols = (body.symbols as unknown[]).map(String);
  if (kind === "analyze") {
    // 个股分析携带用户持仓，供报告与 AI 解读参考
    payload.holdings = listHoldings();
  }
  const jobId = startJob(kind, payload);
  return NextResponse.json({ ok: true, jobId }, { status: 202 });
}
