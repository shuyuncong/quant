import { beforeEach, describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { getTotalCapital, listHoldings, removeHolding, setTotalCapital, upsertHolding } from "../db";
import {
  buildHoldingsContext,
  holdingsFromJobPayload,
  totalCapitalFromJobPayload,
} from "../holdings-context";

describe("holdings", () => {
  beforeEach(() => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "web-holdings-test-"));
    process.env.WEB_DATA_DIR = tempDir;
  });

  it("upserts, computes total when empty, lists and removes", () => {
    const holding = upsertHolding({
      symbol: "600036.SH",
      name: "招商银行",
      shares: 1000,
      cost_price: 30,
      total_amount: 0,
    });
    expect(holding.total_amount).toBe(30000);
    expect(listHoldings()).toHaveLength(1);

    upsertHolding({
      symbol: "600036.SH",
      name: "招商银行",
      shares: 2000,
      cost_price: 30,
      total_amount: 0,
    });
    expect(listHoldings()).toHaveLength(1);
    expect(listHoldings()[0].shares).toBe(2000);

    removeHolding("600036.SH");
    expect(listHoldings()).toHaveLength(0);
  });

  it("keeps manually entered total amount", () => {
    const holding = upsertHolding({
      symbol: "000001.SZ",
      name: "平安银行",
      shares: 100,
      cost_price: 10,
      total_amount: 1234.5,
    });
    expect(holding.total_amount).toBe(1234.5);
  });

  it("reads and writes total capital setting", () => {
    expect(getTotalCapital()).toBe(0);
    setTotalCapital(100000);
    expect(getTotalCapital()).toBe(100000);
    setTotalCapital(0);
    expect(getTotalCapital()).toBe(0);
    expect(() => setTotalCapital(-1)).toThrow();
    expect(() => setTotalCapital(Number.NaN)).toThrow();
  });
});

describe("holdings context", () => {
  it("returns null without holdings or with unmatched symbols", () => {
    expect(buildHoldingsContext(JSON.stringify({ results: [{ symbol: "600036" }] }), [])).toBeNull();
  });

  it("builds context for symbols appearing in the report", () => {
    const holdings = [
      {
        symbol: "600036.SH",
        name: "招商银行",
        shares: 1000,
        cost_price: 30,
        total_amount: 30000,
        created_at: "",
        updated_at: "",
      },
    ];
    const report = JSON.stringify({ results: [{ symbol: "600036" }, { symbol: "000001.SZ" }] });
    const context = buildHoldingsContext(report, holdings);
    expect(context).toContain("600036.SH");
    expect(context).toContain("招商银行");
    expect(context).toContain("30000.00");
  });

  it("parses holdings from job payload json", () => {
    const holdings = holdingsFromJobPayload(
      JSON.stringify({ holdings: [{ symbol: "600036.SH", shares: 1 }] })
    );
    expect(holdings).toHaveLength(1);
    expect(holdingsFromJobPayload("not json")).toEqual([]);
  });

  it("parses total capital from job payload json", () => {
    expect(totalCapitalFromJobPayload(JSON.stringify({ total_capital: 123456 }))).toBe(123456);
    expect(totalCapitalFromJobPayload(JSON.stringify({ total_capital: 0 }))).toBe(0);
    expect(totalCapitalFromJobPayload("not json")).toBe(0);
    expect(totalCapitalFromJobPayload(null)).toBe(0);
  });

  it("includes account total capital and per-symbol share when configured", () => {
    const holdings = [
      {
        symbol: "600036.SH",
        name: "招商银行",
        shares: 1000,
        cost_price: 30,
        total_amount: 30000,
        created_at: "",
        updated_at: "",
      },
      {
        symbol: "000001.SZ",
        name: "平安银行",
        shares: 100,
        cost_price: 10,
        total_amount: 1000,
        created_at: "",
        updated_at: "",
      },
    ];
    const report = JSON.stringify({ results: [{ symbol: "600036" }] });
    const context = buildHoldingsContext(report, holdings, 100000);
    expect(context).toContain("账户总资金：100000.00 元");
    expect(context).toContain("当前总持仓金额：30000.00 元");
    expect(context).toContain("占总资金 30.0%");
    expect(context).toContain("占总持仓 100.0%");
    // 报告未涉及的持仓不出现
    expect(context).not.toContain("000001.SZ");
  });

  it("omits capital lines when total capital is unknown", () => {
    const holdings = [
      {
        symbol: "600036.SH",
        name: "招商银行",
        shares: 1000,
        cost_price: 30,
        total_amount: 30000,
        created_at: "",
        updated_at: "",
      },
    ];
    const report = JSON.stringify({ results: [{ symbol: "600036" }] });
    const context = buildHoldingsContext(report, holdings);
    expect(context).toContain("当前总持仓金额：30000.00 元");
    expect(context).not.toContain("账户总资金");
    expect(context).not.toContain("占总资金");
  });
});
