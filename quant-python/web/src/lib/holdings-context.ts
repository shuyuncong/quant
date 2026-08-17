import type { HoldingRow } from "./types";

function symbolCode(symbol: string): string {
  return String(symbol ?? "").replace(/\D/g, "").slice(0, 6);
}

function formatHolding(holding: HoldingRow): string {
  const shares = Number.isInteger(holding.shares)
    ? String(holding.shares)
    : holding.shares.toFixed(2);
  return (
    `- ${holding.symbol} ${holding.name || "（未填名称）"}：` +
    `持仓 ${shares} 股，持仓价 ${holding.cost_price.toFixed(2)} 元，` +
    `总金额 ${holding.total_amount.toFixed(2)} 元`
  );
}

/** Parse holdings attached to a job payload (added by /api/run for analyze jobs). */
export function holdingsFromJobPayload(payloadJson: string | null | undefined): HoldingRow[] {
  if (!payloadJson) return [];
  try {
    const payload = JSON.parse(payloadJson) as { holdings?: unknown };
    return Array.isArray(payload.holdings) ? (payload.holdings as HoldingRow[]) : [];
  } catch {
    return [];
  }
}

/**
 * Build a holdings context snippet for symbols appearing in the report.
 * Returns null when there are no relevant holdings or the report cannot be parsed.
 */
export function buildHoldingsContext(reportText: string, holdings: HoldingRow[]): string | null {
  if (holdings.length === 0) return null;
  let reportSymbols: Set<string>;
  try {
    const report = JSON.parse(reportText) as { results?: Array<{ symbol?: unknown }> };
    reportSymbols = new Set(
      (report.results ?? []).map((item) => symbolCode(String(item.symbol ?? ""))).filter(Boolean)
    );
  } catch {
    return null;
  }
  if (reportSymbols.size === 0) return null;
  const matched = holdings.filter((holding) => reportSymbols.has(symbolCode(holding.symbol)));
  if (matched.length === 0) return null;
  return `\n\n【我的持仓】（本次分析涉及以下持仓，供参考）\n${matched.map(formatHolding).join("\n")}`;
}
