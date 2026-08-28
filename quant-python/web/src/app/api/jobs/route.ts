import { NextResponse } from "next/server";
import { listJobsWithNote, listPool } from "@/lib/db";

export async function GET() {
  const pool = await listPool();
  const nameMap: Record<string, string> = {};
  for (const row of pool) {
    nameMap[row.symbol] = row.name;
  }

  const jobs = (await listJobsWithNote(100)).map((job) => {
    let payload: Record<string, unknown> = {};
    try {
      payload = JSON.parse(job.payload) as Record<string, unknown>;
    } catch {
      /* ignore */
    }
    const symbols: string[] = Array.isArray(payload.symbols) ? payload.symbols : [];
    // 任务负载里带持仓名称（比亚迪 等），比股票池更可靠；股票池有名称时也并入
    const payloadHoldings: Array<{ symbol?: unknown; name?: unknown }> = Array.isArray(
      payload.holdings
    )
      ? (payload.holdings as Array<{ symbol?: unknown; name?: unknown }>)
      : [];
    const localNames: Record<string, string> = {};
    for (const holding of payloadHoldings) {
      const symbol = String(holding.symbol ?? "").toUpperCase();
      const name = String(holding.name ?? "").trim();
      if (symbol && name) localNames[symbol] = name;
    }
    const names = symbols
      .map((s) => {
        const symbol = s.toUpperCase();
        const name = localNames[symbol] ?? nameMap[symbol] ?? "";
        return name ? `${name}/${symbol}` : s;
      })
      .join(", ");
    return {
      ...job,
      payload,
      symbol_names: names,
    };
  });
  return NextResponse.json({ jobs });
}
