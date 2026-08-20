import { NextResponse } from "next/server";
import { startJob, type JobKind } from "@/lib/jobs";

const VALID_KINDS: Partial<Record<JobKind, true>> = {
  analyze: true,
  scan: true,
  "daily-scan": true,
  "monitor-once": true,
  "test-notify": true,
  "dispatch-outbox": true,
};

export async function POST(request: Request) {
  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const kind = String(body.kind ?? "") as JobKind;
  if (!(kind in VALID_KINDS)) {
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
  const jobId = await startJob(kind, payload);
  return NextResponse.json({ ok: true, jobId }, { status: 202 });
}
