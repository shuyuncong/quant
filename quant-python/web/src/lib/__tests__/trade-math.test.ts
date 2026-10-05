import { describe, expect, it } from "vitest";
import { calculateTrade, validateTradeLot } from "../trade-math";
describe("manual trade accounting", () => {
  it("includes fees in weighted cost and keeps every cent through full liquidation", () => {
    const buy = calculateTrade({ shares: 100, cost_price: 10 }, { side: "buy", quantity: 100, price: "12.34", fees: "5" });
    expect(buy.cost_price).toBe("11.19500000");
    expect(buy.total_amount).toBe("2239.00000000");
    const reduce = calculateTrade(buy, { side: "sell", quantity: 100, price: "15", fees: "5" });
    expect(reduce.cost_price).toBe(buy.cost_price);
    expect(reduce.realized_pnl).toBe("375.50000000");
    const close = calculateTrade(reduce, { side: "sell", quantity: 100, price: "15", fees: "5" });
    expect(close.total_amount).toBe("0.00000000");
    expect(close.shares).toBe(0);
  });
  it("rejects overselling, fractional shares and invalid prices", () => {
    const position = { shares: 100, cost_price: 10 };
    expect(() => calculateTrade(position, { side: "sell", quantity: 200, price: "10", fees: "0" })).toThrow("超过");
    expect(() => calculateTrade(position, { side: "buy", quantity: 0.1, price: "10", fees: "0" })).toThrow("整数");
    expect(() => calculateTrade(position, { side: "buy", quantity: 100, price: "NaN", fees: "0" })).toThrow();
  });
  it("supports board-specific buy units and full odd-lot sells", () => {
    expect(() => validateTradeLot("600000.SH", "buy", 101, 0)).toThrow();
    expect(() => validateTradeLot("688001.SH", "buy", 201, 0)).not.toThrow();
    expect(() => validateTradeLot("430001.BJ", "buy", 101, 0)).not.toThrow();
    expect(() => validateTradeLot("600000.SH", "sell", 51, 51)).not.toThrow();
  });
});
