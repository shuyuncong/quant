import { beforeEach, describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { getTotalCapital, listHoldings, removeHolding, setTotalCapital, upsertHolding } from "../db";
import {
  buildHoldingsContext,
  historyCompleteFromJobPayload,
  holdingsFromJobPayload,
  totalCapitalFromJobPayload,
  tradesFromJobPayload,
} from "../holdings-context";

describe.skipIf(!process.env.SUPABASE_TEST_DATABASE_URL)("holdings", () => {
  beforeEach(() => {
    process.env.DATABASE_URL = process.env.SUPABASE_TEST_DATABASE_URL;
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "web-holdings-test-"));
    process.env.WEB_DATA_DIR = tempDir;
  });

  it("upserts, computes total when empty, lists and removes", async () => {
    const holding = await upsertHolding({
      symbol: "600036.SH",
      name: "招商银行",
      shares: 1000,
      cost_price: 30,
      total_amount: 0,
    });
    expect(holding.total_amount).toBe(30000);
    expect(await listHoldings()).toHaveLength(1);

    await upsertHolding({
      symbol: "600036.SH",
      name: "招商银行",
      shares: 2000,
      cost_price: 30,
      total_amount: 0,
    });
    expect(await listHoldings()).toHaveLength(1);
    expect((await listHoldings())[0].shares).toBe(2000);

    await removeHolding("600036.SH");
    expect(await listHoldings()).toHaveLength(0);
  });

  it("keeps manually entered total amount", async () => {
    const holding = await upsertHolding({
      symbol: "000001.SZ",
      name: "平安银行",
      shares: 100,
      cost_price: 10,
      total_amount: 1234.5,
    });
    expect(holding.total_amount).toBe(1234.5);
  });

  it("reads and writes total capital setting", async () => {
    expect(await getTotalCapital()).toBe(0);
    await setTotalCapital(100000);
    expect(await getTotalCapital()).toBe(100000);
    await setTotalCapital(0);
    expect(await getTotalCapital()).toBe(0);
    await expect(setTotalCapital(-1)).rejects.toThrow();
    await expect(setTotalCapital(Number.NaN)).rejects.toThrow();
  });
});

describe("holdings context", () => {
  it("没有持仓时明确说明空仓，而不是返回 null 让模型去猜", () => {
    const context = buildHoldingsContext(JSON.stringify({ results: [{ symbol: "600036" }] }), { holdings: [] });
    expect(context).toContain("空仓");
  });

  it("账户有持仓但报告股票没匹配上时提示未知，不假设空仓", () => {
    const context = buildHoldingsContext(
      JSON.stringify({ results: [{ symbol: "000001" }] }),
      { holdings: [{ ...HOLDING, symbol: "600036.SH" }], trades: [] }
    );
    expect(context).toContain("未匹配到");
    expect(context).toContain("不要假设为空仓");
  });

  it("报告无法解析时返回 null（等于不注入任何上下文）", () => {
    expect(buildHoldingsContext("not json", { holdings: [HOLDING] })).toBeNull();
    expect(buildHoldingsContext(JSON.stringify({ results: [] }), { holdings: [HOLDING] })).toBeNull();
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
    const context = buildHoldingsContext(report, { holdings });
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
    const context = buildHoldingsContext(report, { holdings, totalCapital: 100000 });
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
    const context = buildHoldingsContext(report, { holdings });
    expect(context).toContain("当前总持仓金额：30000.00 元");
    expect(context).not.toContain("账户总资金");
    expect(context).not.toContain("占总资金");
  });

  it("includes latest price and floating gain from the report", () => {
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
    const report = JSON.stringify({
      results: [{ symbol: "600036", timeframes: { "1d": { latest_price: 33 } } }],
    });
    const context = buildHoldingsContext(report, { holdings });
    expect(context).toContain("现价 33.00 元");
    expect(context).toContain("浮盈 +10.0%");
  });

  it("shows floating loss and falls back to any priced timeframe", () => {
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
    const report = JSON.stringify({
      results: [{ symbol: "600036", timeframes: { "60m": { latest_price: 27 } } }],
    });
    const context = buildHoldingsContext(report, { holdings });
    expect(context).toContain("现价 27.00 元");
    expect(context).toContain("浮盈 -10.0%");
  });

  it("omits price bits when the report has no price", () => {
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
    const report = JSON.stringify({ results: [{ symbol: "600036", timeframes: {} }] });
    const context = buildHoldingsContext(report, { holdings });
    expect(context).toContain("持仓 1000 股，持仓价 30.00 元");
    expect(context).not.toContain("现价");
  });
});

const HOLDING = {
  symbol: "600036.SH",
  name: "招商银行",
  shares: 1000,
  cost_price: 30,
  total_amount: 30000,
  created_at: "",
  updated_at: "",
};

function trade(overrides: Record<string, unknown> = {}) {
  return {
    symbol: "600036.SH",
    side: "buy",
    quantity: 1000,
    price: 30,
    fees: 15,
    amount: 30000,
    realized_pnl: 0,
    traded_at: "2026-09-01 10:00:00",
    ...overrides,
  };
}

describe("holdings context: 成交记录", () => {
  const report = JSON.stringify({ results: [{ symbol: "600036" }] });

  it("列出逐笔成交，最新在前", () => {
    const context = buildHoldingsContext(report, {
      holdings: [HOLDING],
      trades: [
        trade({ traded_at: "2026-08-01 10:00:00", side: "buy", quantity: 500 }),
        trade({ traded_at: "2026-09-01 10:00:00", side: "sell", quantity: 200, realized_pnl: 320.5 }),
      ],
    });
    expect(context).toContain("近期成交");
    expect(context).toContain("卖出 200 股");
    expect(context).toContain("已实现盈亏 +320.50 元");
    expect(context!.indexOf("2026-09-01")).toBeLessThan(context!.indexOf("2026-08-01"));
  });

  it("买入不写已实现盈亏（恒为 0，写出来会被读成不亏不赚）", () => {
    const context = buildHoldingsContext(report, { holdings: [HOLDING], trades: [trade()] });
    expect(context).toContain("买入 1000 股");
    expect(context).not.toContain("已实现盈亏");
  });

  it("只列最近 12 笔并说明还有多少未列", () => {
    const trades = Array.from({ length: 20 }, (_, index) =>
      trade({ traded_at: `2026-09-${String(index + 1).padStart(2, "0")} 10:00:00` })
    );
    const context = buildHoldingsContext(report, { holdings: [HOLDING], trades });
    expect(context).toContain("共显示 12 / 20 笔");
    expect(context).toContain("其余 8 笔未列出");
  });

  it("没登记过成交时点明成本价可能来自期初持仓", () => {
    const context = buildHoldingsContext(report, { holdings: [HOLDING], trades: [] });
    expect(context).toContain("无登记成交");
    expect(context).toContain("期初持仓");
  });

  it("没附带成交记录与零成交是两回事", () => {
    const missing = buildHoldingsContext(report, { holdings: [HOLDING] });
    expect(missing).toContain("未附带成交记录");
    expect(missing).not.toContain("无登记成交");
    const empty = buildHoldingsContext(report, { holdings: [HOLDING], trades: [] });
    expect(empty).not.toContain("未附带成交记录");
  });

  it("history_complete 为 false 时提示历史可能不完整", () => {
    const context = buildHoldingsContext(report, {
      holdings: [HOLDING],
      trades: [trade()],
      historyComplete: false,
    });
    expect(context).toContain("可能不完整");
    expect(context).toContain("不代表真实买入价");
  });
});

describe("job payload 解析：两种键都要认", () => {
  const holding = { symbol: "600036.SH", shares: 1000 };

  it("认 portfolio_context（analyze / scan 批次写的键）", () => {
    const payload = JSON.stringify({
      portfolio_context: {
        holdings: [holding],
        trades: [trade()],
        total_capital: 400000,
        history_complete: false,
      },
    });
    expect(holdingsFromJobPayload(payload)).toHaveLength(1);
    expect(tradesFromJobPayload(payload)).toHaveLength(1);
    expect(totalCapitalFromJobPayload(payload)).toBe(400000);
    expect(historyCompleteFromJobPayload(payload)).toBe(false);
  });

  it("认顶层 holdings（其它报告任务由 startJob 附上）", () => {
    const payload = JSON.stringify({ holdings: [holding], total_capital: 250000 });
    expect(holdingsFromJobPayload(payload)).toHaveLength(1);
    expect(totalCapitalFromJobPayload(payload)).toBe(250000);
    // 未附带成交记录时必须是 null（"不知道"），不是 []（"一笔没有"）
    expect(tradesFromJobPayload(payload)).toBeNull();
    expect(historyCompleteFromJobPayload(payload)).toBeUndefined();
  });

  it("真实 analyze payload 里的持仓能被读到（旧实现只认 holdings 会漏成空）", () => {
    const payload = JSON.stringify({
      symbols: ["002594.SZ"],
      analysis_version: 2,
      portfolio_context: { holdings: [holding], trades: [], total_capital: 400000, history_complete: false },
    });
    expect(holdingsFromJobPayload(payload)).toHaveLength(1);
    expect(tradesFromJobPayload(payload)).toEqual([]);
  });
});
