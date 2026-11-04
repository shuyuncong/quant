import { NextResponse } from "next/server";
import { runBridge } from "@/lib/bridge";
import { etfBridgeScript } from "@/lib/paths";

export const dynamic = "force-dynamic";

export async function POST() {
  // 45s stays below nginx's 60s proxy_read_timeout so a slow refresh returns
  // JSON we can explain, instead of a gateway HTML page.
  const outcome = await runBridge("refresh", {}, { timeoutMs: 45_000, script: etfBridgeScript });
  if (!outcome.ok) {
    // A bridge failure is an application state problem (missing cache, bad
    // snapshot), not a gateway problem. 5xx from the origin gets replaced by
    // Cloudflare's own error page, hiding the reason; 4xx reaches the page.
    const timedOut = outcome.code === 124;
    return NextResponse.json(
      { error: outcome.error || "ETF 信号刷新失败", code: outcome.code },
      { status: timedOut ? 504 : 409 }
    );
  }
  return NextResponse.json(outcome.data);
}
