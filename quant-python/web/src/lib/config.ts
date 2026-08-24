import { runBridge } from "./bridge";
import { deleteSetting, getAllSettings, getSetting, setSetting } from "./db";

export const SECRET_PATHS = [
  "market_data.tushare_token",
  "data_source.tushare_token",
  "notification.wechat.webhook_url",
  "notification.webhook.url",
  "notification.webhook.headers.Authorization",
  "notification.email.sender",
  "notification.email.password",
  "notification.email.receiver",
  "notification.bark.device_key",
] as const;

export const SECRET_ENV_KEYS: Record<string, string> = {
  "market_data.tushare_token": "TUSHARE_TOKEN",
  "data_source.tushare_token": "TUSHARE_TOKEN",
  "notification.wechat.webhook_url": "WECHAT_WEBHOOK_URL",
  "notification.webhook.url": "SIGNAL_WEBHOOK_URL",
  "notification.webhook.headers.Authorization": "SIGNAL_WEBHOOK_AUTH",
  "notification.email.sender": "SIGNAL_EMAIL_SENDER",
  "notification.email.password": "SIGNAL_EMAIL_PASSWORD",
  "notification.email.receiver": "SIGNAL_EMAIL_RECEIVER",
  "notification.bark.device_key": "SIGNAL_BARK_DEVICE_KEY",
};

export function isSecretPath(path: string): boolean {
  return (SECRET_PATHS as readonly string[]).includes(path);
}

export function deepGet(obj: unknown, dotPath: string): unknown {
  let current: unknown = obj;
  for (const part of dotPath.split(".")) {
    if (current == null || typeof current !== "object") return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

export function deepSet(obj: Record<string, unknown>, dotPath: string, value: unknown): void {
  const parts = dotPath.split(".");
  let current = obj;
  for (const part of parts.slice(0, -1)) {
    if (typeof current[part] !== "object" || current[part] === null) {
      current[part] = {};
    }
    current = current[part] as Record<string, unknown>;
  }
  current[parts[parts.length - 1]] = value;
}

export function deepDelete(obj: Record<string, unknown>, dotPath: string): void {
  const parts = dotPath.split(".");
  let current = obj;
  for (const part of parts.slice(0, -1)) {
    if (typeof current[part] !== "object" || current[part] === null) return;
    current = current[part] as Record<string, unknown>;
  }
  delete current[parts[parts.length - 1]];
}

/** Build bridge overrides from DB settings. Secret fields prefer the matching environment variable. */
export async function buildOverrides(providedSettings?: Record<string, unknown>): Promise<Record<string, unknown>> {
  const settings = providedSettings ?? (await getAllSettings());
  const overrides: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(settings)) {
    // holdings.* 是 Web 端持仓数据（总资金等），不是引擎配置，不随任务下发。
    if (key.startsWith("holdings.")) continue;
    const envKey = SECRET_ENV_KEYS[key];
    if (envKey && process.env[envKey]) {
      deepSet(overrides, key, { __env__: envKey });
    } else {
      deepSet(overrides, key, value);
    }
  }
  return overrides;
}

export function maskSettings(settings: Record<string, unknown>): Record<string, unknown> {
  const masked: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(settings)) {
    if (isSecretPath(key) && typeof value === "string") {
      masked[key] = value ? "****" : "";
    } else {
      masked[key] = value;
    }
  }
  return masked;
}

export interface EffectiveConfig {
  config: Record<string, unknown>;
  secret_sources: Record<string, string>;
  runtime: { output_dir?: string; database_path?: string; config_path?: string; base_dir?: string };
  settings: Record<string, unknown>;
  version: string;
  generated_at: string;
}

let cache: { at: number; version: string; result: EffectiveConfig } | null = null;

export async function getEffectiveConfig(force = false): Promise<EffectiveConfig> {
  const settings = await getAllSettings();
  const version = JSON.stringify(settings);
  const now = Date.now();
  if (!force && cache && now - cache.at < 10_000 && cache.version === version) {
    return cache.result;
  }
  const outcome = await runBridge("config", { overrides: await buildOverrides(settings) }, { timeoutMs: 30_000 });
  if (!outcome.ok || !outcome.data) {
    throw new Error(outcome.error || "获取有效配置失败");
  }
  const data = outcome.data as {
    config: Record<string, unknown>;
    secret_sources: Record<string, string>;
    runtime: EffectiveConfig["runtime"];
  };
  const result: EffectiveConfig = {
    config: data.config,
    secret_sources: data.secret_sources,
    runtime: data.runtime,
    settings: maskSettings(settings),
    version,
    generated_at: new Date().toISOString(),
  };
  cache = { at: now, version, result };
  return result;
}

export async function saveSettings(values: Record<string, unknown>): Promise<void> {
  for (const [key, value] of Object.entries(values)) {
    await setSetting(key, value);
  }
}

export async function clearSetting(key: string): Promise<void> {
  await deleteSetting(key);
}

export async function getSettingValue(key: string): Promise<unknown> {
  return getSetting(key);
}

type FieldType = "number" | "string" | "boolean" | "stringArray" | "enum";

interface FieldDef {
  type: FieldType;
  min?: number;
  max?: number;
  integer?: boolean;
  enum?: string[];
  optional?: boolean;
}

const STRATEGIES_SCHEMA: Record<string, FieldDef> = {
  "signal_strategy.chan.min_bi_bars": { type: "number", min: 2 },
  "signal_strategy.chan.divergence_ratio": { type: "number", min: 0, max: 1 },
  "signal_strategy.chan.fresh_signal_bars": { type: "number", min: 1 },
  "signal_strategy.macd.fast": { type: "number", min: 1 },
  "signal_strategy.macd.slow": { type: "number", min: 1 },
  "signal_strategy.macd.signal": { type: "number", min: 1 },
  "signal_strategy.macd.zero_axis_tolerance": { type: "number", min: 0 },
  "signal_strategy.macd.moderate_volume_min": { type: "number", min: 0 },
  "signal_strategy.macd.moderate_volume_max": { type: "number", min: 0 },
  "signal_strategy.llm_context_bars": { type: "number", min: 10, max: 200 },
  "signal_strategy.scoring.buy_threshold": { type: "number", min: 0, max: 100 },
  "signal_strategy.scoring.sell_threshold": { type: "number", min: 0, max: 100 },
  "monitor.timeframes": { type: "stringArray" },
  "monitor.watchlist": { type: "stringArray" },
  "monitor.bar_limit": { type: "number", min: 30 },
  "monitor.max_symbols_per_cycle": { type: "number", min: 1, max: 100 },
  "scan.universe_mode": { type: "enum", enum: ["watchlist", "all_a"] },
  "stock_pool.enabled": { type: "boolean" },
  "stock_pool.min_market_cap": { type: "number", min: 0 },
  "stock_pool.max_market_cap": { type: "number", min: 0 },
  "stock_pool.amount_window": { type: "number", min: 1, max: 250, integer: true },
  "stock_pool.min_avg_amount": { type: "number", min: 0 },
  "stock_pool.turnover_window": { type: "number", min: 1, max: 250, integer: true },
  "stock_pool.min_avg_turnover_rate": { type: "number", min: 0, max: 100 },
  "stock_pool.max_avg_turnover_rate": { type: "number", min: 0, max: 100 },
  "stock_pool.min_listing_trade_days": { type: "number", min: 0, max: 5000, integer: true },
  "stock_pool.exclude_st": { type: "boolean" },
  "stock_pool.exclude_delisting": { type: "boolean" },
  "stock_pool.missing_data_policy": { type: "enum", enum: ["reject", "allow"] },
};

const NOTIFICATION_SCHEMA: Record<string, FieldDef> = {
  "notification.timeout_seconds": { type: "number", min: 1, max: 120 },
  "notification.wechat.enabled": { type: "boolean" },
  "notification.wechat.webhook_url": { type: "string" },
  "notification.webhook.enabled": { type: "boolean" },
  "notification.webhook.url": { type: "string" },
  "notification.webhook.headers.Authorization": { type: "string" },
  "notification.email.enabled": { type: "boolean" },
  "notification.email.smtp_server": { type: "string" },
  "notification.email.smtp_port": { type: "number", min: 1, max: 65535 },
  "notification.email.sender": { type: "string" },
  "notification.email.password": { type: "string" },
  "notification.email.receiver": { type: "string" },
  "notification.bark.enabled": { type: "boolean" },
  "notification.bark.url": { type: "string" },
  "notification.bark.device_key": { type: "string" },
  "notification.push_trade_signal": { type: "boolean" },
  "notification.push_candidate_pool": { type: "boolean" },
  "notification.push_ai_analysis": { type: "boolean" },
};

export type ConfigSection = "strategies" | "notification";

export function validateSection(
  section: ConfigSection,
  values: Record<string, unknown>
): { ok: boolean; errors: string[]; normalized: Record<string, unknown> } {
  const schema = section === "strategies" ? STRATEGIES_SCHEMA : NOTIFICATION_SCHEMA;
  const errors: string[] = [];
  const normalized: Record<string, unknown> = {};
  for (const [key, raw] of Object.entries(values)) {
    const def = schema[key];
    if (!def) {
      errors.push(`未知配置项: ${key}`);
      continue;
    }
    if (raw === undefined || raw === null || raw === "") {
      if (def.optional) {
        normalized[key] = "";
        continue;
      }
      if (def.type !== "string") {
        errors.push(`${key} 不能为空`);
        continue;
      }
    }
    if (def.type === "number") {
      const num = Number(raw);
      if (!Number.isFinite(num)) {
        errors.push(`${key} 必须是数字`);
        continue;
      }
      if (def.integer && !Number.isInteger(num)) errors.push(`${key} 必须是整数`);
      if (def.min !== undefined && num < def.min) errors.push(`${key} 不能小于 ${def.min}`);
      if (def.max !== undefined && num > def.max) errors.push(`${key} 不能大于 ${def.max}`);
      normalized[key] = num;
      continue;
    }
    if (def.type === "boolean") {
      if (typeof raw !== "boolean") {
        errors.push(`${key} 必须是布尔值`);
        continue;
      }
      normalized[key] = raw;
      continue;
    }
    if (def.type === "stringArray") {
      if (!Array.isArray(raw) || raw.some((item) => typeof item !== "string")) {
        errors.push(`${key} 必须是字符串数组`);
        continue;
      }
      normalized[key] = raw;
      continue;
    }
    if (def.type === "enum") {
      if (typeof raw !== "string" || !(def.enum ?? []).includes(raw)) {
        errors.push(`${key} 必须是 ${(def.enum ?? []).join("/")}`);
        continue;
      }
      normalized[key] = raw;
      continue;
    }
    normalized[key] = String(raw);
  }
  if (section === "strategies") {
    const minVolume = Number(normalized["signal_strategy.macd.moderate_volume_min"]);
    const maxVolume = Number(normalized["signal_strategy.macd.moderate_volume_max"]);
    if (Number.isFinite(minVolume) && Number.isFinite(maxVolume) && minVolume > maxVolume) {
      errors.push("温和放量下限不能大于上限");
    }
    const minMarketCap = Number(normalized["stock_pool.min_market_cap"]);
    const maxMarketCap = Number(normalized["stock_pool.max_market_cap"]);
    if (Number.isFinite(minMarketCap) && Number.isFinite(maxMarketCap) && minMarketCap > maxMarketCap) {
      errors.push("流通市值下限不能大于上限");
    }
    const minTurnover = Number(normalized["stock_pool.min_avg_turnover_rate"]);
    const maxTurnover = Number(normalized["stock_pool.max_avg_turnover_rate"]);
    if (Number.isFinite(minTurnover) && Number.isFinite(maxTurnover) && minTurnover > maxTurnover) {
      errors.push("平均换手率下限不能大于上限");
    }
  }
  return { ok: errors.length === 0, errors, normalized };
}

/** True when saving the value should keep the existing DB secret instead of overwriting with the placeholder. */
export function shouldKeepExistingSecret(key: string, value: unknown): boolean {
  return isSecretPath(key) && value === "****";
}
