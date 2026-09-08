import { NextResponse } from "next/server";
import { listJobsWithNote, listPool } from "@/lib/db";
import { normalizeSymbol } from "@/lib/symbols";

export async function GET() {
  const pool = await listPool();
  const nameMap: Record<string, string> = {};
  for (const row of pool) {
    const symbol = normalizeSymbol(row.symbol);
    if (symbol) nameMap[symbol] = row.name;
  }

  const jobs = (await listJobsWithNote(100)).map((job) => {
    let payload: Record<string, unknown> = {};
    try {
      payload = JSON.parse(job.payload) as Record<string, unknown>;
    } catch {
      /* ignore */
    }
    const symbols: string[] = Array.isArray(payload.symbols) ? payload.symbols : [];
    // 名称源：任务负载中手动解析的名称（resolve-names）> 持仓名称 > 股票池名称
    const payloadSymbolNames =
      payload.symbol_names && typeof payload.symbol_names === "object"
        ? (payload.symbol_names as Record<string, unknown>)
        : {};
    const payloadHoldings: Array<{ symbol?: unknown; name?: unknown }> = Array.isArray(
      payload.holdings
    )
      ? (payload.holdings as Array<{ symbol?: unknown; name?: unknown }>)
      : [];
    const localNames: Record<string, string> = {};
    for (const [symbol, name] of Object.entries(payloadSymbolNames)) {
      const normalized = normalizeSymbol(symbol);
      const value = String(name ?? "").trim();
      if (normalized && value) localNames[normalized] = value;
    }
    for (const holding of payloadHoldings) {
      const symbol = normalizeSymbol(String(holding.symbol ?? ""));
      const name = String(holding.name ?? "").trim();
      if (symbol && name) localNames[symbol] = name;
    }
    const names = symbols
      .map((s) => {
        const symbol = normalizeSymbol(s);
        const name = (symbol && (localNames[symbol] ?? nameMap[symbol])) || "";
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
