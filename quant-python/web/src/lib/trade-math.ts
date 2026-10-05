/** Decimal arithmetic: prices/costs use 8 decimal places, cash rounds to cents. */
const SCALE = 100_000_000n;
export function decimal(value: unknown): bigint {
  const text = String(value ?? "").trim();
  if (!/^\d+(?:\.\d{1,8})?$/.test(text)) throw new Error("金额必须为非负数字，最多 8 位小数");
  const [whole, fraction = ""] = text.split(".");
  const result = BigInt(whole) * SCALE + BigInt(fraction.padEnd(8, "0"));
  if (result > 100_000_000_000_000n * SCALE) throw new Error("金额超出支持范围");
  return result;
}
const cash = (value: bigint) => ((value + 500_000n) / 1_000_000n) * 1_000_000n;
export function decimalString(value: bigint): string {
  const negative = value < 0n;
  const absolute = negative ? -value : value;
  return `${negative ? "-" : ""}${absolute / SCALE}.${String(absolute % SCALE).padStart(8, "0")}`;
}
export interface TradeInput {
  symbol: string;
  side: "buy" | "sell";
  quantity: number;
  price: string;
  fees: string;
  traded_at: string;
  request_key: string;
  note?: string;
}
export function calculateTrade(
  current: { shares: number; cost_price: string | number; total_amount?: string | number },
  input: Pick<TradeInput, "side" | "quantity" | "price" | "fees">,
) {
  if (!["buy", "sell"].includes(input.side)) throw new Error("交易方向不合法");
  if (!Number.isSafeInteger(current.shares) || current.shares < 0) throw new Error("原持仓数量需先校正为整数");
  if (!Number.isSafeInteger(input.quantity) || input.quantity <= 0) throw new Error("成交数量必须为正整数");
  const price = decimal(input.price);
  const fees = cash(decimal(input.fees));
  if (price <= 0n) throw new Error("成交价格必须大于 0");
  const amount = cash(price * BigInt(input.quantity));
  const oldCost = current.total_amount != null
    ? decimal(current.total_amount)
    : decimal(current.cost_price) * BigInt(current.shares);
  if (input.side === "sell" && input.quantity > current.shares) throw new Error("卖出数量超过持仓数量");
  if (input.side === "sell" && fees > amount) throw new Error("卖出费用不能超过成交金额");
  const shares = current.shares + (input.side === "buy" ? input.quantity : -input.quantity);
  const removedCost = input.side === "sell"
    ? (input.quantity === current.shares ? oldCost : (oldCost * BigInt(input.quantity) / BigInt(current.shares)))
    : 0n;
  const cost = input.side === "buy" ? oldCost + amount + fees : oldCost - removedCost;
  const average = shares ? (cost + BigInt(shares) / 2n) / BigInt(shares) : 0n;
  return {
    shares,
    cost_price: decimalString(average),
    total_amount: decimalString(cost),
    amount: decimalString(amount),
    cash_amount: decimalString(input.side === "buy" ? amount + fees : amount - fees),
    realized_pnl: decimalString(input.side === "sell" ? amount - fees - removedCost : 0n),
    fees: decimalString(fees),
  };
}

export function validateTradeLot(symbol: string, side: "buy" | "sell", quantity: number, available: number) {
  const code = symbol.slice(0, 6);
  const star = code.startsWith("688") || code.startsWith("689");
  const beijing = symbol.endsWith(".BJ");
  if (side === "buy") {
    if (star ? quantity < 200 : beijing ? quantity < 100 : quantity % 100 !== 0) {
      throw new Error(star ? "科创板买入不少于 200 股" : beijing ? "北交所买入不少于 100 股" : "买入数量应为 100 股的整数倍");
    }
  } else if (quantity !== available && (star ? quantity < 200 : beijing ? quantity < 100 : quantity % 100 !== 0)) {
    throw new Error("非整单位余股需一次卖出全部可卖数量");
  }
}
