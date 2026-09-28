import { NextResponse } from "next/server";
import { runBridge } from "@/lib/bridge";

export async function GET() {
  try {
    const outcome = await runBridge(
      "observed-candidates",
      {},
      { timeoutMs: 60_000 },
    );
    if (!outcome.ok) {
      return NextResponse.json(
        { error: outcome.error || "读取观察候选失败" },
        { status: 500 },
      );
    }
    return NextResponse.json(outcome.data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "读取观察候选失败" },
      { status: 500 },
    );
  }
}
