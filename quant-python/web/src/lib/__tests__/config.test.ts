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
    });
    expect(ok).toBe(true);
    expect(errors).toEqual([]);
    expect(normalized["signal_strategy.chan.min_bi_bars"]).toBe(4);
  });

  it("rejects unknown keys and out-of-range numbers", () => {
    const { ok, errors } = validateSection("strategies", {
      "signal_strategy.chan.min_bi_bars": 1,
      "foo.bar": 1,
    });
    expect(ok).toBe(false);
    expect(errors.some((item) => item.includes("未知配置项"))).toBe(true);
  });
});
