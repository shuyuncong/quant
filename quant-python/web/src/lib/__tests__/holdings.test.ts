import { beforeEach, describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { listHoldings, removeHolding, upsertHolding } from "../db";
import { buildHoldingsContext, holdingsFromJobPayload } from "../holdings-context";

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
});
