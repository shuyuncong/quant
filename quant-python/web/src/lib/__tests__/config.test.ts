import { beforeEach, describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { buildOverrides, maskSettings, validateSection } from "../config";
import { setSetting } from "../db";

describe.skipIf(!process.env.SUPABASE_TEST_DATABASE_URL)("config precedence (env > DB > YAML)", () => {
  beforeEach(() => {
    process.env.DATABASE_URL = process.env.SUPABASE_TEST_DATABASE_URL;
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "web-config-test-"));
    process.env.WEB_DATA_DIR = tempDir;
  });

  it("sends env marker when env var exists for a secret", async () => {
    process.env.TUSHARE_TOKEN = "env-token";
    await setSetting("market_data.tushare_token", "db-token");
    const overrides = await buildOverrides();
    expect((overrides.market_data as Record<string, unknown>).tushare_token).toEqual({ __env__: "TUSHARE_TOKEN" });
    delete process.env.TUSHARE_TOKEN;
  });

  it("uses DB value when env var is absent", async () => {
    delete process.env.TUSHARE_TOKEN;
    await setSetting("market_data.tushare_token", "db-token");
    const overrides = await buildOverrides();
    expect((overrides.market_data as Record<string, unknown>).tushare_token).toBe("db-token");
  });

  it("masks secret settings and keeps plain values", () => {
    const masked = maskSettings({
      "notification.webhook.url": "https://example.com/hook",
      "notification.wechat.enabled": true,
      "monitor.daily_scan_time": "15:30",
    });
    expect(masked["notification.webhook.url"]).toBe("****");
    expect(masked["notification.wechat.enabled"]).toBe(true);
    expect(masked["monitor.daily_scan_time"]).toBe("15:30");
  });
});

describe("validateSection", () => {
  it("accepts valid strategies values", () => {
    const { ok, errors, normalized } = validateSection("strategies", {
      "signal_strategy.chan.min_bi_bars": "4",
      "signal_strategy.chan.divergence_ratio": "0.9",
      "signal_strategy.execution_policy.default": "enabled",
      "signal_strategy.execution_policy.signals.buy_2": "observe_only",
      "stock_pool.enabled": true,
      "stock_pool.min_market_cap": "50",
      "stock_pool.max_market_cap": "3000",
      "stock_pool.amount_window": "20",
      "stock_pool.min_avg_amount": "1",
      "stock_pool.turnover_window": "20",
      "stock_pool.min_avg_turnover_rate": "0.5",
      "stock_pool.max_avg_turnover_rate": "8",
      "stock_pool.min_listing_trade_days": "120",
      "stock_pool.exclude_st": true,
      "stock_pool.exclude_delisting": true,
      "stock_pool.missing_data_policy": "reject",
    });
    expect(ok).toBe(true);
    expect(errors).toEqual([]);
    expect(normalized["signal_strategy.chan.min_bi_bars"]).toBe(4);
    expect(normalized["signal_strategy.execution_policy.signals.buy_2"]).toBe("observe_only");
    expect(normalized["stock_pool.min_market_cap"]).toBe(50);
  });

  it("rejects unknown keys and out-of-range numbers", () => {
    const { ok, errors } = validateSection("strategies", {
      "signal_strategy.chan.min_bi_bars": 1,
      "foo.bar": 1,
    });
    expect(ok).toBe(false);
    expect(errors.some((item) => item.includes("未知配置项"))).toBe(true);
  });

  it("rejects inverted stock pool ranges", () => {
    const { ok, errors } = validateSection("strategies", {
      "stock_pool.min_market_cap": 3001,
      "stock_pool.max_market_cap": 3000,
      "stock_pool.min_avg_turnover_rate": 8.1,
      "stock_pool.max_avg_turnover_rate": 8,
    });
    expect(ok).toBe(false);
    expect(errors).toContain("流通市值下限不能大于上限");
    expect(errors).toContain("平均换手率下限不能大于上限");
  });

  it("rejects invalid stock pool windows and missing policy", () => {
    const { ok, errors } = validateSection("strategies", {
      "stock_pool.amount_window": 0,
      "stock_pool.turnover_window": 20.5,
      "stock_pool.missing_data_policy": "ignore",
    });
    expect(ok).toBe(false);
    expect(errors.length).toBeGreaterThanOrEqual(3);
    expect(errors.some((item) => item.includes("必须是整数"))).toBe(true);
  });

  it("rejects invalid signal execution modes", () => {
    const { ok, errors } = validateSection("strategies", {
      "signal_strategy.execution_policy.signals.buy_2": "paper_trade",
    });

    expect(ok).toBe(false);
    expect(errors.some((item) => item.includes("enabled/observe_only/disabled"))).toBe(true);
  });
});
