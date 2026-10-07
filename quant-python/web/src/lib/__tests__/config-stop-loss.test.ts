import { describe, expect, it, vi } from "vitest";

const { effectiveConfig } = vi.hoisted(() => ({
  effectiveConfig: {
    risk: {
      stop_loss_pct: 0.08,
      strategy_stop_loss_pct: {
        macd_zero_axis: null as number | null,
        yearline_pullback: 0.03,
      },
    },
  },
}));

vi.mock("../db", () => ({
  getAllSettings: async () => ({}),
  getSetting: vi.fn(),
  setSetting: vi.fn(),
  deleteSetting: vi.fn(),
}));

vi.mock("../bridge", () => ({
  runBridge: async () => ({
    ok: true,
    data: { config: effectiveConfig, secret_sources: {}, runtime: {} },
    code: 0,
  }),
}));

import { freezeEngineConfig } from "../config";

describe("frozen strategy stop rules", () => {
  it("preserves an existing task's global and strategy rules after later configuration changes", async () => {
    const original = await freezeEngineConfig();

    effectiveConfig.risk.stop_loss_pct = 0.06;
    effectiveConfig.risk.strategy_stop_loss_pct.macd_zero_axis = 0.04;
    effectiveConfig.risk.strategy_stop_loss_pct.yearline_pullback = 0.05;
    const next = await freezeEngineConfig();

    expect(original.risk).toEqual({
      stop_loss_pct: 0.08,
      strategy_stop_loss_pct: { macd_zero_axis: null, yearline_pullback: 0.03 },
    });
    expect(next.risk).toEqual({
      stop_loss_pct: 0.06,
      strategy_stop_loss_pct: { macd_zero_axis: 0.04, yearline_pullback: 0.05 },
    });
  });
});
