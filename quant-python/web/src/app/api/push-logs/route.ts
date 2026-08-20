import { NextResponse } from "next/server";
import { runBridge } from "@/lib/bridge";
import { buildOverrides } from "@/lib/config";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const limit = Math.min(Number(url.searchParams.get("limit") ?? 200) || 200, 500);
  try {
    const overrides = await buildOverrides();
    const [logOutcome, summaryOutcome] = await Promise.all([
      runBridge("outbox-log", { limit, overrides }, { timeoutMs: 30_000 }),
      runBridge("outbox-status", { overrides }, { timeoutMs: 30_000 }),
    ]);
    if (!logOutcome.ok || !logOutcome.data) {
      throw new Error(logOutcome.error || "读取推送日志失败");
    }
    const logData = logOutcome.data;
    const records =
      logData && typeof logData === "object" && "records" in logData && Array.isArray(logData.records)
        ? logData.records
        : [];
    const summaryData = summaryOutcome.ok ? summaryOutcome.data : null;
    const summary =
      summaryData && typeof summaryData === "object"
        ? {
            pending: "pending" in summaryData ? Number(summaryData.pending) || 0 : 0,
            delivered: "delivered" in summaryData ? Number(summaryData.delivered) || 0 : 0,
            failed: "failed" in summaryData ? Number(summaryData.failed) || 0 : 0,
            total_events: "total_events" in summaryData ? Number(summaryData.total_events) || 0 : 0,
          }
        : { pending: 0, delivered: 0, failed: 0, total_events: 0 };
    return NextResponse.json({ records, summary });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "读取推送日志失败" },
      { status: 500 }
    );
  }
}
