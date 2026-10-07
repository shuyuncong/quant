import { describe, expect, it } from "vitest";
import {
  BACKTEST_DISABLED_MESSAGE,
  backtestAvailable,
  backtestServiceUnavailable,
  isLegacyBacktestPayload,
  resolvePublicBacktestJob,
  validateBacktestOptions,
} from "../backtest-service";

const base = { start: "2024-01-02", end: "2024-03-31", initial_cash: 100000, max_positions: 4, position_size_pct: 0.25 };

describe("backtest option validation", () => {
  it("requires exactly one normalised symbol for a stock run", () => {
    expect(validateBacktestOptions({ ...base, mode: "stock", symbols: ["600036"] }).symbols).toEqual(["600036.SH"]);
    expect(validateBacktestOptions({ ...base, mode: "stock", symbols: ["600036.SH"] }).symbols).toEqual(["600036.SH"]);
    // An empty stock request must not silently become a market-wide run.
    expect(() => validateBacktestOptions({ ...base, mode: "stock", symbols: [] })).toThrow("个股回测需要且只需一个股票代码");
    expect(() => validateBacktestOptions({ ...base, mode: "stock", symbols: ["600036", "000001"] })).toThrow("个股回测需要且只需一个股票代码");
  });

  it("rejects symbols on a market run", () => {
    expect(validateBacktestOptions({ ...base, mode: "market", symbols: [] }).mode).toBe("market");
    expect(() => validateBacktestOptions({ ...base, mode: "market", symbols: ["600036"] })).toThrow("全市场回测不接受股票代码");
  });

  it("requires a mode", () => {
    expect(() => validateBacktestOptions({ ...base, symbols: [] })).toThrow("请选择个股或全市场回测");
  });

  it("accepts a leap day and rejects a nonexistent calendar day", () => {
    expect(validateBacktestOptions({ ...base, mode: "market", end: "2024-02-29" }).end).toBe("2024-02-29");
    expect(() => validateBacktestOptions({ ...base, mode: "market", end: "2024-02-30" })).toThrow("请选择有效的起止日期");
    expect(() => validateBacktestOptions({ ...base, mode: "market", start: "2024-13-01" })).toThrow("请选择有效的起止日期");
  });

  it("rejects inverted and future ranges", () => {
    expect(() => validateBacktestOptions({ ...base, mode: "market", start: "2024-03-31", end: "2024-01-02" })).toThrow("结束日期必须晚于开始日期");
    expect(() => validateBacktestOptions({ ...base, mode: "market", end: "2999-01-01" })).toThrow("结束日期不能在未来");
  });

  it("rejects out-of-range capital, positions and allocation", () => {
    for (const bad of [999, 1e11, Number.NaN]) {
      expect(() => validateBacktestOptions({ ...base, mode: "market", initial_cash: bad })).toThrow("初始资金应为 1000 至 100 亿");
    }
    for (const bad of [0, 101, 2.5]) {
      expect(() => validateBacktestOptions({ ...base, mode: "market", max_positions: bad })).toThrow("最大持仓数量应为 1-100");
    }
    for (const bad of [0, 1.5, -0.1]) {
      expect(() => validateBacktestOptions({ ...base, mode: "market", position_size_pct: bad })).toThrow("单笔仓位比例应为 0-100%");
    }
  });

  it("never lets the caller narrow the strategy comparison", () => {
    expect(() => validateBacktestOptions({ ...base, mode: "market", strategies: ["macd_zero_axis"] })).toThrow("回测固定比较全部三策略，请勿传 strategies");
    expect(() => validateBacktestOptions({ ...base, mode: "market", strategies: [] })).toThrow("回测固定比较全部三策略，请勿传 strategies");
  });
});

describe("backtest availability", () => {
  it("depends only on BACKTEST_ENABLED", () => {
    const before = process.env.BACKTEST_ENABLED;
    process.env.BACKTEST_ENABLED = "1";
    expect(backtestAvailable()).toBe(true);
    process.env.BACKTEST_ENABLED = "0";
    expect(backtestAvailable()).toBe(false);
    expect(backtestServiceUnavailable()).toBe(BACKTEST_DISABLED_MESSAGE);
    delete process.env.BACKTEST_ENABLED;
    expect(backtestAvailable()).toBe(false);
    if (before === undefined) delete process.env.BACKTEST_ENABLED;
    else process.env.BACKTEST_ENABLED = before;
  });
});

describe("public job projection", () => {
  const row = {
    id: 7,
    status: "success",
    created_at: "2026-01-01 10:00:00",
    error: null,
    payload: JSON.stringify({ options: { mode: "stock", start: "2024-01-02", output_dir: "/secret/path" }, overrides: { risk: { stop_loss_pct: 0.08 } } }),
  };

  it("exposes only the request that produced the job", () => {
    const projected = resolvePublicBacktestJob(row);
    expect(projected.options).toEqual({ mode: "stock", start: "2024-01-02" });
    // The server-side output directory and the frozen rule snapshot stay internal.
    expect(JSON.stringify(projected)).not.toContain("/secret/path");
    expect(JSON.stringify(projected)).not.toContain("stop_loss_pct");
  });

  it("tolerates a malformed payload instead of throwing", () => {
    expect(resolvePublicBacktestJob({ ...row, payload: "not json" }).options).toBeNull();
  });

  it("classifies rows without the current version as legacy", () => {
    expect(isLegacyBacktestPayload(JSON.stringify({ options: {}, backtest_version: 2 }))).toBe(false);
    expect(isLegacyBacktestPayload(JSON.stringify({ options: {} }))).toBe(true);
    expect(isLegacyBacktestPayload(JSON.stringify({ options: {}, backtest_version: 1 }))).toBe(true);
    expect(isLegacyBacktestPayload("broken")).toBe(true);
  });
});
