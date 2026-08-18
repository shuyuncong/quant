import type { HoldingRow } from "./types";

function symbolCode(symbol: string): string {
  return String(symbol ?? "").replace(/\D/g, "").slice(0, 6);
}

/** 占比百分文本：任一侧非正时返回空串。 */
function pctText(part: number, total: number): string {
  return total > 0 && part > 0 ? `${((part / total) * 100).toFixed(1)}%` : "";
}

/** Parse total capital attached to a job payload (added by startJob for report kinds). */
export function totalCapitalFromJobPayload(payloadJson: string | null | undefined): number {
  if (!payloadJson) return 0;
  try {
    const payload = JSON.parse(payloadJson) as { total_capital?: unknown };
    const num = Number(payload.total_capital);
    return Number.isFinite(num) && num > 0 ? num : 0;
  } catch {
    return 0;
  }
}

/** Parse holdings attached to a job payload (added by startJob for report kinds). */
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
 * Includes the configured account total capital and per-symbol share so the
 * model can reason about position concentration and sizing.
 * Returns null when there are no relevant holdings or the report cannot be parsed.
 */
export function buildHoldingsContext(
  reportText: string,
  holdings: HoldingRow[],
  totalCapital = 0
): string | null {
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

  const totalHoldAmount = matched.reduce(
    (sum, holding) => sum + (holding.total_amount > 0 ? holding.total_amount : 0),
    0
  );
  const lines: string[] = ["\n\n【我的持仓】（本次分析涉及以下持仓，供参考）"];
  if (totalCapital > 0) {
    lines.push(`账户总资金：${totalCapital.toFixed(2)} 元`);
    if (totalHoldAmount > 0) {
      lines.push(
        `当前总持仓金额：${totalHoldAmount.toFixed(2)} 元` +
          `（占总资金 ${pctText(totalHoldAmount, totalCapital)}）`
      );
    }
  } else if (totalHoldAmount > 0) {
    lines.push(`当前总持仓金额：${totalHoldAmount.toFixed(2)} 元`);
  }
  for (const holding of matched) {
    const shares = Number.isInteger(holding.shares)
      ? String(holding.shares)
      : holding.shares.toFixed(2);
    const amount = holding.total_amount > 0 ? holding.total_amount.toFixed(2) : "0.00";
    const bits: string[] = [];
    const pctCapital = pctText(holding.total_amount, totalCapital);
    const pctHold = pctText(holding.total_amount, totalHoldAmount);
    if (pctCapital) bits.push(`占总资金 ${pctCapital}`);
    if (pctHold) bits.push(`占总持仓 ${pctHold}`);
    lines.push(
      `- ${holding.symbol} ${holding.name || "（未填名称）"}：` +
        `持仓 ${shares} 股，持仓价 ${holding.cost_price.toFixed(2)} 元，` +
        `总金额 ${amount} 元${bits.length > 0 ? `（${bits.join("，")}）` : ""}`
    );
  }
  return lines.join("\n");
}