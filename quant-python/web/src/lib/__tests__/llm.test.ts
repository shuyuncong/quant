import { describe, expect, it } from "vitest";
import { buildInterpretationContext } from "../llm";

function bars(prefix: string, count: number) {
  return Array.from({ length: count }, (_, index) => ({
    datetime: `${prefix}-${String(index).padStart(2, "0")}`,
    open: index,
    high: index + 1,
    low: index - 1,
    close: index + 0.5,
    volume: 1000 + index,
  }));
}

describe("buildInterpretationContext", () => {
  it("keeps every stock and timeframe while compacting long bar arrays", () => {
    const report = {
      mode: "analyze",
      results: ["000001.SZ", "600036.SH"].map((symbol) => ({
        symbol,
        name: symbol,
        timeframes: {
          "1m": { status: "ok", indicators: { dif: 1 }, chan: {}, recent_bars: bars(symbol, 100) },
          "1d": { status: "ok", indicators: { dif: 2 }, chan: {}, recent_bars: bars(symbol, 100) },
        },
      })),
    };
    const context = buildInterpretationContext(JSON.stringify(report), 20_000);
    const parsed = JSON.parse(context) as typeof report;
    expect(parsed.results.map((item) => item.symbol)).toEqual(["000001.SZ", "600036.SH"]);
    for (const result of parsed.results) {
      expect(Object.keys(result.timeframes)).toEqual(["1m", "1d"]);
      expect(result.timeframes["1m"].recent_bars.length).toBeLessThan(100);
    }
  });
});
