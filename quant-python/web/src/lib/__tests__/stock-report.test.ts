import { describe, expect, it } from "vitest";
import {
  compactDataSource, compactTimeframe, confirmationLabel, executionLabel, finiteNumber,
  formatPrice, formatRatio, formatTime, orderedTimeframes, positionRisk,
  priceChange, primaryTimeframe, structureLabel, timeframeSignals, timestamp,
} from "../stock-report";

function daily(overrides: Record<string, unknown> = {}) {
  return compactTimeframe("1d", {
    status: "ok", latest_price: 11, latest_time: "2026-09-02T15:00:00",
    recent_bars: [
      { datetime: "2026-09-01T15:00:00", close: 10 },
      { datetime: "2026-09-02T15:00:00", close: 11 },
    ],
    ...overrides,
  });
}

describe("stock report read-only projection", () => {
  it("keeps historical pullback confirmation separate from current entry readiness", () => {
    expect(confirmationLabel(daily({ indicators: { golden_cross_entry_ready: true, golden_cross_state: "confirmed_pullback" } }))).toBe("本根确认就绪");
    expect(confirmationLabel(daily({ indicators: { golden_cross_entry_ready: false, golden_cross_state: "confirmed_pullback", golden_cross_first_confirmation_time: "2026-09-01T15:00:00" } }))).toBe("此前已确认");
    expect(confirmationLabel(daily({ indicators: { golden_cross_entry_ready: false, golden_cross_state: "pending_pullback" } }))).toBe("等待回踩确认");
    expect(confirmationLabel(daily({ indicators: { golden_cross_entry_ready: false, golden_cross_state: "invalidated" } }))).toBe("结构已失效");
  });
  it.each([null, undefined, [], "legacy", 42])("accepts absent or invalid report %j", (input) => {
    expect(compactDataSource(input)).toMatchObject({ results: [], candidates: [], errors: [], market_context: null });
  });

  it("keeps old reports readable without inventing structure or execution evidence", () => {
    const source = { results: [{ symbol: "000001", name: "平安银行", timeframes: { "1d": { latest_price: 10 } } }] };
    const before = JSON.stringify(source);
    const projected = compactDataSource(source);
    expect(projected.results[0]).toMatchObject({ symbol: "000001", name: "平安银行", analyzed_at: null });
    expect(projected.results[0].timeframes[0]).toMatchObject({ latest_price: 10, status: "unknown", bars: [], latest_center: null, events: [] });
    expect(JSON.stringify(source)).toBe(before);
  });

  it.each([Infinity, -Infinity, NaN, "10", undefined, null])("rejects non-finite/non-number %s", (input) => {
    expect(finiteNumber(input)).toBeNull();
    expect(compactTimeframe("1d", { latest_price: input, recent_bars: [{ close: input }] }).bars[0].close).toBeNull();
  });

  it("distinguishes saved bars, history coverage, and displayed bars", () => {
    const bars = Array.from({ length: 145 }, (_, i) => ({ datetime: `bar-${i}`, close: i }));
    const projected = compactTimeframe("1d", { recent_bars: bars, indicators: { bar_count: 600 } });
    expect(projected.bar_count).toBe(145);
    expect(projected.history_bar_count).toBe(600);
    expect(projected.displayed_bar_count).toBe(120);
    expect(projected.bars).toHaveLength(120);
    expect(projected.bars[0].datetime).toBe("bar-25");
    expect(projected.bars.at(-1)?.datetime).toBe("bar-144");
    expect(bars).toHaveLength(145);
  });

  it("uses an indicator whitelist, preserves false, and sanitizes arrays and numbers", () => {
    const tf = daily({ indicators: {
      high_position_risk: false, ma_long: Infinity, confirmation_items: ["a", null, 2, "b"],
      secret: "not for the report", high_volume_risk: { value: false },
    } });
    expect(tf.indicators).toEqual({ high_position_risk: false, ma_long: null, confirmation_items: ["a", "b"] });
  });

  it.each([{ zd: 11, zg: 10 }, { zd: 0, zg: 10 }, { zd: 9, zg: Infinity }, { zd: "9", zg: 10 }])("rejects invalid centers %j", (center) => {
    expect(daily({ chan: { latest_center: center } }).latest_center).toBeNull();
  });

  it("retains center boundaries and reports price position without recomputing signals", () => {
    const tf = daily({ chan: { latest_center: { zd: 9, zg: 10, start_time: "2026-08-01" } } });
    expect(tf.latest_center).toEqual({ zd: 9, zg: 10, start_time: "2026-08-01", end_time: null });
    expect(structureLabel(tf)).toBe("中枢上方");
    expect(structureLabel({ ...tf, latest_price: 9 })).toBe("中枢区间内");
    expect(structureLabel({ ...tf, latest_price: 8 })).toBe("中枢下方");
    expect(structureLabel({ ...tf, status: "error" })).toBe("数据不足");
  });

  it("prefers event evidence over raw signals and preserves unknown/observe execution modes", () => {
    const signal = { signal_type: "buy_1", side: "buy", confirmed_at: "2026-09-01T15:00:00", execution_mode: "enabled", actionable: true };
    const tf = daily({ chan: { fresh_signals: [signal] }, events: [
      { ...signal, evidence: { execution_mode: "observe_only", actionable: false } },
      { ...signal, signal_type: "new_signal", evidence: { execution_mode: "future_mode", actionable: true } },
      { ...signal, signal_type: "disabled_signal", execution_mode: "disabled" },
      { signal_type: "unknown_signal" },
    ] });
    const signals = timeframeSignals(tf);
    expect(signals).toHaveLength(4);
    const observed = signals.find((item) => item.signal_type === "buy_1")!;
    expect(observed).toMatchObject({ execution_mode: "observe_only", actionable: false });
    expect(executionLabel(observed)).toBe("仅观察");
    expect(executionLabel(signals.find((item) => item.signal_type === "new_signal")!)).toBe("执行状态未提供");
    expect(executionLabel(signals.find((item) => item.signal_type === "disabled_signal")!)).toBe("策略禁用");
    expect(executionLabel(signals.find((item) => item.signal_type === "unknown_signal")!)).toBe("执行状态未提供");
  });
});

describe("honest report presentation", () => {
  const validRisk = { high_position_risk: false, high_volume_risk: false, history_complete: true, ma_long: 10, volume_ratio: 1.1, distance_to_ma_long: 0.1, recent_return: 0.03 };

  it("requires long-MA evidence despite successful MACD and default false risks", () => {
    expect(positionRisk(daily({ indicators: { ...validRisk, ma_long: null, dif: 0.1, dea: 0.05 } }))).toBe("unavailable");
    expect(positionRisk(daily({ indicators: validRisk }))).toBe("clear");
    expect(positionRisk(daily({ indicators: { ...validRisk, history_complete: false } }))).toBe("unavailable");
    expect(positionRisk(daily())).toBe("unavailable");
    expect(positionRisk(undefined)).toBe("unavailable");
    expect(positionRisk({ ...daily({ indicators: validRisk }), timeframe: "60m" })).toBe("unavailable");
    expect(positionRisk(daily({ indicators: { high_position_risk: true } }))).toBe("triggered");
  });

  it("prioritizes daily context even when unavailable; otherwise uses ordered successful fallback", () => {
    const weekly = compactTimeframe("1w", { status: "ok" });
    const hourly = compactTimeframe("60m", { status: "ok" });
    const day = daily({ status: "error" });
    const result = { symbol: "s", name: "n", analyzed_at: null, status: null, timeframes: [hourly, weekly, day] };
    expect(primaryTimeframe(result)).toBe(day);
    expect(primaryTimeframe({ ...result, timeframes: [hourly, weekly] })).toBe(weekly);
    expect(primaryTimeframe({ ...result, timeframes: [] })).toBeUndefined();
    expect(orderedTimeframes(result.timeframes).map((tf) => tf.timeframe)).toEqual(["1w", "1d", "60m"]);
    expect(result.timeframes[0]).toBe(hourly);
  });

  it("formats ratios as percentages and preserves zero prices", () => {
    expect(formatPrice(0)).toBe("0.00");
    expect(formatPrice(null)).toBe("—");
    expect(formatRatio(0.0123)).toBe("+1.23%");
    expect(formatRatio(-0.01)).toBe("-1.00%");
    expect(formatRatio(0)).toBe("0.00%");
    expect(formatRatio(NaN)).toBe("—");
  });

  it("interprets naive report timestamps in Shanghai and respects explicit timezones", () => {
    expect(timestamp("2026-09-02 15:00:00")).toBe(Date.parse("2026-09-02T07:00:00Z"));
    expect(timestamp("2026-09-02")).toBe(Date.parse("2026-09-01T16:00:00Z"));
    expect(formatTime("2026-09-02T07:00:00Z")).toBe("2026-09-02 15:00");
    expect(formatTime("2026-09-02T15:00:00+08:00")).toBe("2026-09-02 15:00");
    expect(timestamp("not-a-date")).toBeNull();
    expect(formatTime(null)).toBe("时间未提供");
  });

  it("only calculates change from consecutive saved bars aligned with the quote", () => {
    expect(priceChange(daily())).toBeCloseTo(0.1);
    expect(priceChange(daily({ latest_price: 12 }))).toBeNull();
    expect(priceChange(daily({ latest_time: "2026-09-03T15:00:00" }))).toBeNull();
    expect(priceChange(daily({ recent_bars: [{ close: 10 }, { close: null }, { datetime: "2026-09-02T15:00:00", close: 11 }] }))).toBeNull();
    expect(priceChange(daily({ recent_bars: [{ close: 0 }, { datetime: "2026-09-02T15:00:00", close: 11 }] }))).toBeNull();
  });

  it("does not treat two missing or invalid timestamps as an aligned quote", () => {
    expect(priceChange(daily({ latest_time: null, recent_bars: [{ close: 10 }, { close: 11 }] }))).toBeNull();
    expect(priceChange(daily({ latest_time: "invalid", recent_bars: [{ close: 10 }, { datetime: "invalid", close: 11 }] }))).toBeNull();
  });
});
