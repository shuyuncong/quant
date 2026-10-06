import type { HoldingRow, HoldingTradeRow, PortfolioContextInput } from "./types";

/** 成交记录最多列出的条数：每股 50 笔是最新 50 笔，全列会让上下文膨胀、挤掉报告正文。 */
const MAX_TRADES_SHOWN = 12;

/** 一行持仓的字段：报告里的最新价按"1d 优先、否则任意有价周期"取。 */
interface ReportResult {
  symbol?: unknown;
  timeframes?: unknown;
}

function symbolCode(symbol: string): string {
  return String(symbol ?? "").replace(/\D/g, "").slice(0, 6);
}

/** 从一份报告结果的 1d（其次任意有价周期）取最新价；取不到返回 null。 */
function latestPriceOf(result: ReportResult): number | null {
  const timeframes = result.timeframes;
  if (!timeframes || typeof timeframes !== "object") return null;
  const entries = Object.entries(timeframes);
  const priceOf = (value: unknown): number | null => {
    if (!value || typeof value !== "object" || !("latest_price" in value)) return null;
    const price = Number(value.latest_price);
    return Number.isFinite(price) && price > 0 ? price : null;
  };
  const primary = entries.find(([key]) => key === "1d");
  return (primary ? priceOf(primary[1]) : null) ?? entries.map(([, value]) => priceOf(value)).find((price) => price !== null) ?? null;
}

/** 占比百分文本：任一侧非正时返回空串。 */
function pctText(part: number, total: number): string {
  return total > 0 && part > 0 ? `${((part / total) * 100).toFixed(1)}%` : "";
}

/** Parse total capital attached to a job payload (`portfolio_context.total_capital` 或顶层 `total_capital`)。 */
export function totalCapitalFromJobPayload(payloadJson: string | null | undefined): number {
  if (!payloadJson) return 0;
  try {
    const payload = JSON.parse(payloadJson) as Record<string, unknown>;
    const context = payload.portfolio_context;
    const raw = context && typeof context === "object" && "total_capital" in context
      ? context.total_capital
      : payload.total_capital;
    const num = Number(raw);
    return Number.isFinite(num) && num > 0 ? num : 0;
  } catch {
    return 0;
  }
}

/**
 * Parse holdings attached to a job payload.
 *
 * 两种键并存，因为是两条路径写进去的：
 * - `portfolio_context`：`analyze` / `scan` 批次（含成交记录与账户口径）；
 * - `holdings`：其它报告任务由 `startJob` 直接附上（**每股最多取 50 笔成交**，
 *   而 `portfolio_context` 是快照，含全部持股）。
 *
 * 只认 `holdings` 会漏掉 analyze/scan 这两类最常用的任务。
 */
export function holdingsFromJobPayload(payloadJson: string | null | undefined): HoldingRow[] {
  if (!payloadJson) return [];
  try {
    const payload = JSON.parse(payloadJson) as Record<string, unknown>;
    if (Array.isArray(payload.holdings)) return payload.holdings as HoldingRow[];
    const context = payload.portfolio_context;
    if (context && typeof context === "object" && "holdings" in context && Array.isArray(context.holdings)) {
      return context.holdings as HoldingRow[];
    }
    return [];
  } catch {
    return [];
  }
}

/**
 * Parse trades attached to a job payload.
 *
 * Returns null when the payload carries no `trades` key at all — 那与"确实一笔没成交"
 * 是两回事：前者说明这条任务在写 payload 时没带上成交记录（例如更早版本的任务），
 * 后者说明账户真的没成交过。模型必须能区分，否则会把"没带"读成"没有"。
 */
export function tradesFromJobPayload(payloadJson: string | null | undefined): HoldingTradeRow[] | null {
  if (!payloadJson) return null;
  try {
    const payload = JSON.parse(payloadJson) as Record<string, unknown>;
    if (Array.isArray(payload.trades)) return payload.trades as HoldingTradeRow[];
    const context = payload.portfolio_context;
    if (context && typeof context === "object" && "trades" in context && Array.isArray(context.trades)) {
      return context.trades as HoldingTradeRow[];
    }
    return null;
  } catch {
    return null;
  }
}

/** 成交记录是否完整：只有 `portfolio_context` 会带这个标记（快照口径）。 */
export function historyCompleteFromJobPayload(payloadJson: string | null | undefined): boolean | undefined {
  if (!payloadJson) return undefined;
  try {
    const payload = JSON.parse(payloadJson) as Record<string, unknown>;
    const context = payload.portfolio_context;
    if (context && typeof context === "object" && "history_complete" in context) {
      return context.history_complete === true;
    }
    // `startJob` 附上的 `holdings` 是按"每股最近 50 笔"截取的，历史同样不完整。
    return Array.isArray(payload.trades) ? false : undefined;
  } catch {
    return undefined;
  }
}

/** 逐笔成交一行：`- 2026-09-01 买入 1000 股，@ 35.20 元，金额 35200.00 元`。 */
function tradeLine(trade: HoldingTradeRow): string {
  const sell = String(trade.side ?? "").toLowerCase() === "sell";
  const quantity = Number(trade.quantity);
  const price = Number(trade.price);
  const amount = Number(trade.amount);
  const fees = Number(trade.fees);
  const realized = Number(trade.realized_pnl);
  const bits = [
    `${sell ? "卖出" : "买入"} ${Number.isFinite(quantity) ? quantity : "未知"} 股`,
    `@ ${Number.isFinite(price) ? price.toFixed(2) : "未知"} 元`,
    `金额 ${Number.isFinite(amount) ? amount.toFixed(2) : "未知"} 元`,
  ];
  if (Number.isFinite(fees) && fees !== 0) bits.push(`费用 ${fees.toFixed(2)} 元`);
  // 已实现盈亏只在卖出时有意义；买入那笔恒为 0，写出来会让模型以为"不亏不赚"。
  if (sell) {
    bits.push(`已实现盈亏 ${Number.isFinite(realized) ? `${realized >= 0 ? "+" : ""}${realized.toFixed(2)}` : "未知"} 元`);
  }
  const tradedAt = String(trade.traded_at ?? "").slice(0, 10);
  return `  - ${tradedAt || "日期未知"} ${bits.join("，")}`;
}

/**
 * Build a holdings + trades context snippet for symbols appearing in the report.
 *
 * Includes account total capital, per-symbol share, and the already-recorded
 * trades so the model can reason about concentration, sizing, and whether a
 * position was recently added to or trimmed — instead of only seeing a static
 * cost price that may be an opening-balance estimate.
 *
 * Returns null when the report cannot be parsed or no holding matches the report
 * symbols. 「没传持仓」与「确实零持仓」在调用方就已区分：后者仍会说明账户当前
 * 空仓，而不是让模型去猜。
 */
export function buildHoldingsContext(
  reportText: string,
  input: PortfolioContextInput
): string | null {
  const holdings = input.holdings ?? [];
  const trades = input.trades ?? null;
  const totalCapital = input.totalCapital ?? 0;
  let reportResults: ReportResult[];
  try {
    const report = JSON.parse(reportText) as Record<string, unknown>;
    reportResults = Array.isArray(report.results) ? (report.results as ReportResult[]) : [];
  } catch {
    return null;
  }
  const reportSymbols = new Set(
    reportResults.map((item) => symbolCode(String(item.symbol ?? ""))).filter(Boolean)
  );
  if (reportSymbols.size === 0) return null;
  const matched = holdings.filter((holding) => reportSymbols.has(symbolCode(holding.symbol)));
  // 「确实空仓」要说出来；「报告涉及的股票查不到账户」多半是读取/匹配出问题，
  // 两者都不能让模型误以为"账户有这只股票、只是没告诉我持仓"。
  if (matched.length === 0) {
    return holdings.length === 0
      ? "\n\n【我的持仓与近期成交】账户当前**空仓**（没有任何已登记持仓）。"
      : "\n\n【我的持仓与近期成交】⚠️ 账户有持仓，但本次报告的股票未匹配到任何一条：" +
        "无法确认这些代码的持仓、" +
        "成本与可用资金，请按未知处理，不要假设为空仓。";
  }

  const prices: Record<string, number | null> = {};
  for (const result of reportResults) {
    const code = symbolCode(String(result.symbol ?? ""));
    if (code && !(code in prices)) prices[code] = latestPriceOf(result);
  }
  const totalHoldAmount = matched.reduce(
    (sum, holding) => sum + (holding.total_amount > 0 ? holding.total_amount : 0),
    0
  );
  const lines: string[] = ["\n\n【我的持仓与近期成交】（本次分析涉及以下持仓，供参考）"];
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
    const price = prices[symbolCode(holding.symbol)] ?? null;
    const priceBits: string[] = [];
    if (price !== null && price > 0) {
      priceBits.push(`现价 ${price.toFixed(2)} 元`);
      if (holding.cost_price > 0) {
        const gain = ((price - holding.cost_price) / holding.cost_price) * 100;
        priceBits.push(`浮盈 ${gain >= 0 ? "+" : ""}${gain.toFixed(1)}%`);
      }
    }
    lines.push(
      `- ${holding.symbol} ${holding.name || "（未填名称）"}：` +
        `持仓 ${shares} 股，持仓价 ${holding.cost_price.toFixed(2)} 元，` +
        `${priceBits.length > 0 ? `${priceBits.join("，")}，` : ""}` +
        `总金额 ${amount} 元${bits.length > 0 ? `（${bits.join("，")}）` : ""}`
    );
    if (!trades) continue;
    const own = trades
      .filter((trade) => symbolCode(trade.symbol) === symbolCode(holding.symbol))
      .sort((left, right) => String(right.traded_at).localeCompare(String(left.traded_at)));
    if (own.length === 0) {
      lines.push("  - 近期成交：无登记成交（持仓价可能来自期初持仓，不是真实买入价）");
      continue;
    }
    lines.push(`  - 近期成交（最新在前，共显示 ${Math.min(own.length, MAX_TRADES_SHOWN)} / ${own.length} 笔）：`);
    for (const trade of own.slice(0, MAX_TRADES_SHOWN)) lines.push(tradeLine(trade));
    if (own.length > MAX_TRADES_SHOWN) {
      lines.push(`    （其余 ${own.length - MAX_TRADES_SHOWN} 笔未列出）`);
    }
  }
  lines.push(
    !trades
      ? "\n注：本次任务未附带成交记录，只能看到当前持仓，看不到买卖历史。"
      : input.historyComplete === false
        ? "\n注：成交记录可能不完整（每股最多保留最近 50 笔）；持仓价可能是期初持仓的估算成本，不代表真实买入价。"
        : `\n注：以上成交记录共 ${trades.length} 笔。`
  );
  return lines.join("\n");
}
