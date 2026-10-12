// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { StrategyRuleSettings } from "../strategy-rule-settings";

/** Effective config as `/api/config` returns it: shares + per-strategy plans. */
function effectiveConfig(overrides: Record<string, unknown> = {}) {
  return {
    risk: {
      stop_loss_pct: 0.08,
      stop_profit_pct: 0.3,
      strategy_stop_loss_pct: { macd_zero_axis: 0.1, yearline_pullback: null, macd_divergence: 0.06 },
      exit_plan: null,
      strategy_exit_plan: { macd_zero_axis: null, yearline_pullback: {}, macd_divergence: null },
      ...(overrides.risk as object ?? {}),
    },
    macd_divergence: { enabled: true, min_volume_ratio: 1.5, volume_window: 20, long_ma_slope_window: 20, min_macd_segment_bars: 2 },
    backtest: { exit_rules: { mode: "fixed" }, chan_zero_axis: { max_holding_bars: 40 } },
  };
}

let host: HTMLDivElement;
let root: Root;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function settle() {
  await act(async () => { await Promise.resolve(); });
  await act(async () => { await Promise.resolve(); });
}

async function render(config = effectiveConfig()) {
  fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "PUT") return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) } as Response);
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ config, secret_sources: {}, runtime: {} }) } as Response);
  });
  vi.stubGlobal("fetch", fetchMock);
  await act(async () => root.render(createElement(StrategyRuleSettings)));
  await settle();
}

function saveButton(): HTMLButtonElement {
  const button = [...host.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent === "保存止损与退出规则");
  expect(button, "保存止损与退出规则").toBeDefined();
  return button!;
}

function selectFor(title: string): HTMLSelectElement {
  const select = host.querySelector<HTMLSelectElement>(`select[aria-label='${title}分批止盈方式']`);
  expect(select, `${title} 分批止盈`).toBeDefined();
  return select!;
}

function setSelect(select: HTMLSelectElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, value);
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

function numberField(id: string): HTMLInputElement {
  const input = host.querySelector<HTMLInputElement>(`#${id}`);
  expect(input, id).toBeDefined();
  return input!;
}

function setNumber(input: HTMLInputElement, value: string) {
  // React tracks the previous value on the node; the native setter keeps the tracker stale.
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function savedPayload() {
  const call = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
  expect(call, "PUT payload").toBeDefined();
  return JSON.parse((call![1] as RequestInit).body as string) as Record<string, unknown>;
}

describe("per-strategy scale-out plans", () => {
  it("keeps an explicit empty plan as 关闭 rather than collapsing it to 继承", async () => {
    await render();

    expect(selectFor("年线趋势").value).toBe("off");
    expect(selectFor("日线零轴金叉").value).toBe("inherit");
    // 关闭分层的策略不应渲染出自定义档位输入。
    expect(host.querySelector("#rule-risk-strategy_exit_plan-yearline_pullback-first_gain")).toBeNull();
  });

  it("saves null for 继承, an empty object for 关闭, and the plan for 自定义", async () => {
    await render();

    await act(async () => setSelect(selectFor("日线零轴金叉"), "custom"));
    await settle();
    const prefix = "rule-risk-strategy_exit_plan-macd_zero_axis";
    await act(async () => {
      setNumber(numberField(`${prefix}-first_gain`), "15");
      setNumber(numberField(`${prefix}-initial_fraction`), "50");
    });
    await settle();

    await act(async () => saveButton().click());
    await settle();

    expect(savedPayload()).toMatchObject({
      "risk.exit_plan": null,
      "risk.strategy_exit_plan.macd_zero_axis": { first_gain: 0.15, initial_fraction: 0.5, tiers: [], protect_tail: "breakeven" },
      "risk.strategy_exit_plan.yearline_pullback": {},
    });
    expect(savedPayload()["risk.strategy_exit_plan.macd_divergence"]).toBeNull();
  });

  it("seeds a custom plan from the global one so a new override is never blank", async () => {
    await render(effectiveConfig({ risk: { exit_plan: { first_gain: 0.2, initial_fraction: 0.25, tiers: [{ gain: 0.4, fraction: 0.25 }], protect_tail: "none" } } }));

    await act(async () => setSelect(selectFor("零轴＋底背离"), "custom"));
    await settle();

    const prefix = "rule-risk-strategy_exit_plan-macd_divergence";
    expect(numberField(`${prefix}-first_gain`).value).toBe("20");
    expect(numberField(`${prefix}-initial_fraction`).value).toBe("25");
    expect(numberField(`${prefix}-tiers-0-gain`).value).toBe("40");
  });

  it("blocks the save and points at the strategy when a scoped plan is invalid", async () => {
    await render();

    await act(async () => setSelect(selectFor("零轴＋底背离"), "custom"));
    await settle();
    const prefix = "rule-risk-strategy_exit_plan-macd_divergence";
    // 两档之和 60% + 60% 超过 100%，必须在本地拦下而不是交给服务端。
    await act(async () => {
      setNumber(numberField(`${prefix}-first_gain`), "10");
      setNumber(numberField(`${prefix}-initial_fraction`), "60");
    });
    await settle();
    const addTier = [...host.querySelectorAll<HTMLButtonElement>("button")].filter((item) => item.textContent === "增加档位").at(-1)!;
    await act(async () => addTier.click());
    await settle();
    await act(async () => {
      setNumber(numberField(`${prefix}-tiers-0-gain`), "20");
      setNumber(numberField(`${prefix}-tiers-0-fraction`), "60");
    });
    await settle();

    await act(async () => saveButton().click());
    await settle();

    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "PUT")).toBe(false);
    expect(host.textContent).toContain("各档减仓比例之和不能超过 100%");
  });

  it("round-trips an unsupported trailing plan untouched instead of erasing it", async () => {
    const trailing = { trailing_activation_gain: 0.15, trailing_pct: 0.08 };
    await render(effectiveConfig({ risk: { strategy_exit_plan: { macd_zero_axis: null, yearline_pullback: null, macd_divergence: trailing } } }));

    expect(selectFor("零轴＋底背离").value).toBe("custom");
    expect(host.textContent).toContain("该策略的计划含移动止盈等本表单不支持的字段");

    await act(async () => saveButton().click());
    await settle();
    expect(savedPayload()["risk.strategy_exit_plan.macd_divergence"]).toEqual(trailing);
  });

  it("refuses to save when the stored plan is unparseable instead of overwriting it", async () => {
    await render(effectiveConfig({ risk: { strategy_exit_plan: { macd_zero_axis: "ladder", yearline_pullback: null, macd_divergence: null } } }));

    await act(async () => saveButton().click());
    await settle();

    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "PUT")).toBe(false);
    expect(host.textContent).toContain("分批止盈计划格式无法识别");
  });

  it("keeps a trailing global plan when only the stop levels change", async () => {
    const trailing = { trailing_activation_gain: 0.15, trailing_pct: 0.08 };
    await render(effectiveConfig({ risk: { exit_plan: trailing } }));

    expect(host.textContent).toContain("移动止盈等本表单不支持的字段");
    await act(async () => saveButton().click());
    await settle();
    // 保存止损不应顺手把全局移动止盈计划替换成 null/阶梯。
    expect(savedPayload()["risk.exit_plan"]).toEqual(trailing);
  });
});
