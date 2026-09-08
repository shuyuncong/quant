// @vitest-environment jsdom
import { act, createElement, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StockAnalysisReport, StockReportLoader } from "../stock-analysis-report";
import { StockPriceChart } from "../stock-price-chart";
import { compactDataSource, compactTimeframe, type DataSource } from "@/lib/stock-report";

const bars = [1, 2, 3].map((day) => ({
  datetime: `2026-09-0${day}T15:00:00`, open: 10, high: 12, low: 9,
  close: day === 3 ? 11 : 10, volume: day * 100, dif: 0.1, dea: 0.05, hist: 0.1,
}));

function fixture(): DataSource {
  const timeframe = {
    status: "ok", latest_time: bars[2].datetime, latest_price: 11, recent_bars: bars,
    indicators: { high_position_risk: false, high_volume_risk: false, ma_long: null, golden_cross_entry_ready: false, golden_cross_state: "pending_pullback" },
    chan: {
      latest_center: { zd: 9.5, zg: 10.5, start_time: bars[0].datetime, end_time: bars[1].datetime },
      fresh_signals: [{ signal_type: "buy_1", side: "buy", price: 10, structure_time: bars[0].datetime, confirmed_at: bars[2].datetime }],
    },
  };
  return compactDataSource({
    analyzed_at: "2026-09-03T16:00:00", market_context: { regime: "bull", allows_entries: true },
    results: [
      { symbol: "000001", name: "甲股票", timeframes: { "60m": timeframe, "1d": timeframe, "1w": timeframe } },
      { symbol: "000002", name: "乙股票", timeframes: { "1d": { ...timeframe, latest_price: 22 }, "60m": timeframe } },
    ],
  });
}

let host: HTMLDivElement;
let root: Root;
async function render(node: ReactNode) { await act(async () => { root.render(node); }); }
async function click(element: HTMLElement) { await act(async () => { element.click(); }); }
async function select(element: HTMLSelectElement, value: string) {
  await act(async () => { element.value = value; element.dispatchEvent(new Event("change", { bubbles: true })); });
}
function element<T extends Element>(selector: string): T {
  const found = host.querySelector<T>(selector);
  expect(found, selector).not.toBeNull();
  return found!;
}
function buttonWithin(selector: string, label: string): HTMLButtonElement {
  const found = [...element(selector).querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === label);
  expect(found, label).toBeDefined();
  return found!;
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function response(data: unknown, ok = true) {
  return { ok, json: async () => data } as Response;
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  // Any unexpected network use fails locally; loader tests install their own controlled responses.
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("unexpected fetch in isolated component test")));
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => { root.unmount(); });
  host.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("stock research report interactions", () => {
  it("defaults to daily and synchronizes chart period buttons with the comparison table", async () => {
    await render(createElement(StockAnalysisReport, { source: fixture() }));
    for (const label of ["市场环境", "日线结构", "日线金叉回踩状态", "日线位置风险", "价格走势与结构", "多周期结构对照"]) {
      expect(host.textContent).toContain(label);
    }
    expect(buttonWithin('[aria-label="图表周期"]', "日线").getAttribute("aria-pressed")).toBe("true");
    expect(element("tr[data-active='true']").textContent).toContain("日线");
    await click(buttonWithin('[aria-label="图表周期"]', "60分"));
    expect(element("tr[data-active='true']").textContent).toContain("60分");
    expect(host.textContent).toContain("60分条件与信号");
    await click(buttonWithin("table", "周线"));
    expect(buttonWithin('[aria-label="图表周期"]', "周线").getAttribute("aria-pressed")).toBe("true");
    expect(host.textContent).toContain("周线条件与信号");
    // The headline remains the primary daily quote while inspecting other periods.
    expect(host.querySelector("header")?.textContent).toContain("日线收盘");
  });

  it("resets to daily when changing stock and displays the new quote", async () => {
    await render(createElement(StockAnalysisReport, { source: fixture() }));
    await click(buttonWithin('[aria-label="图表周期"]', "60分"));
    await select(element<HTMLSelectElement>('[aria-label="报告内股票"]'), "1");
    expect(host.querySelector("h2")?.textContent).toContain("乙股票");
    expect(host.querySelector("header")?.textContent).toContain("22.00");
    expect(buttonWithin('[aria-label="图表周期"]', "日线").getAttribute("aria-pressed")).toBe("true");
    expect(element("tr[data-active='true']").textContent).toContain("日线");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("shows volume above MACD, toggles the center, and exposes a keyboard-focusable range", async () => {
    await render(createElement(StockAnalysisReport, { source: fixture() }));
    const chart = element<SVGSVGElement>('svg[role="img"]');
    expect(chart.textContent).toContain("VOL · 报告原始量");
    expect(chart.textContent).toContain("MACD · DIF / DEA / HIST");
    const panelLabels = [...chart.querySelectorAll("text")].filter((node) => node.textContent?.startsWith("VOL ·") || node.textContent?.startsWith("MACD ·"));
    expect(panelLabels.map((node) => node.textContent)).toEqual(["VOL · 报告原始量", "MACD · DIF / DEA / HIST"]);
    expect(Number(panelLabels[0].getAttribute("y"))).toBeLessThan(Number(panelLabels[1].getAttribute("y")));
    expect(chart.textContent).toContain("ZG 10.50");
    expect(element(".report-chart-readout").textContent).toContain("量 300");
    expect(element(".report-chart-readout").textContent).toContain("DIF 0.1000");
    await click(element<HTMLInputElement>('input[type="checkbox"]'));
    expect(chart.textContent).not.toContain("ZG 10.50");
    const slider = element<HTMLInputElement>('[aria-label="选择 K 线"]');
    slider.focus();
    expect(document.activeElement).toBe(slider);
    expect(slider.type).toBe("range");
    expect(slider.min).toBe("0");
    expect(slider.max).toBe("2");
    expect(slider.getAttribute("aria-valuetext")).toBe("2026-09-03 15:00");
    // jsdom does not implement the native ArrowLeft default action. Dispatch the
    // resulting native input event to verify the same React change path.
    await act(async () => {
      slider.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(slider, "1");
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(slider.getAttribute("aria-valuetext")).toBe("2026-09-02 15:00");
    expect(element(".report-chart-readout").textContent).toContain("2026-09-02 15:00");
  });

  it("reports missing MA evidence as unavailable despite false risk flags and an ok timeframe", async () => {
    await render(createElement(StockAnalysisReport, { source: fixture() }));
    expect(host.textContent).toContain("风险检测数据不足");
    expect(host.textContent).not.toContain("已检测项未触发");
    expect(host.textContent).not.toContain("未触发已定义的位置风险项");
  });

  it("keeps historical pullback confirmation distinct from the one-bar entry-ready pulse", async () => {
    const source = fixture();
    const daily = source.results[0].timeframes.find((tf) => tf.timeframe === "1d")!;
    daily.indicators.golden_cross_state = "confirmed_pullback";
    daily.indicators.golden_cross_entry_ready = false;
    daily.indicators.golden_cross_first_confirmation_time = "2026-09-02T15:00:00";
    await render(createElement(StockAnalysisReport, { source }));
    expect(host.querySelector("header")?.textContent).toContain("金叉回踩状态：此前已确认");
    expect(host.textContent).toContain("此前已确认 · 首次确认：2026-09-02 15:00");
    expect(host.textContent).toContain("本根未就绪不抹去历史确认");
    expect(host.textContent).not.toContain("金叉回踩状态：等待回踩确认");
  });

  it("renders empty and failed data without fabricating valid prices or signals", async () => {
    await render(createElement(StockAnalysisReport, { source: compactDataSource(null) }));
    expect(host.textContent).toContain("该任务没有个股结构化报告");
    const source = compactDataSource({
      errors: [{ symbol: "000001", error: "历史文件不完整" }],
      results: [{ symbol: "000001", timeframes: { "1d": { status: "error", error: "行情不可用" } } }],
    });
    await render(createElement(StockAnalysisReport, { source }));
    expect(host.textContent).toContain("历史文件不完整");
    expect(host.textContent).toContain("行情不可用");
    expect(host.textContent).toContain("暂无可绘制的完整 K 线");
    expect(host.textContent).toContain("该周期数据不足，无法判断近期信号");
    expect(host.querySelector('svg[role="img"]')).toBeNull();
    await render(createElement(StockAnalysisReport, { source: compactDataSource({ results: [{ symbol: "empty", timeframes: {} }] }) }));
    expect(host.textContent).toContain("该股票没有可用周期数据");
    expect(host.textContent).toContain("报告未包含周期行情");
  });

  it("discloses missing MACD instead of drawing an invented series", async () => {
    const tf = compactTimeframe("1d", { recent_bars: bars.map(({ datetime, open, high, low, close, volume }) => ({ datetime, open, high, low, close, volume })) });
    await render(createElement(StockPriceChart, { timeframe: tf }));
    expect(element('svg[role="img"]').textContent).toContain("MACD 数据未提供");
    expect(element(".report-chart-readout").textContent).toContain("DIF —");
  });
});

describe("chart rendering boundaries", () => {
  it("handles a flat series and never emits non-finite SVG coordinates", () => {
    const tf = compactTimeframe("1d", { recent_bars: bars.map((bar) => ({ ...bar, open: 10, high: 10, low: 10, close: 10, volume: 0 })) });
    const html = renderToStaticMarkup(createElement(StockPriceChart, { timeframe: tf }));
    expect(html).toContain('role="img"');
    expect(html).not.toMatch(/NaN|Infinity/);
    expect(html).toContain("10.00");
  });

  it("marks a signal on its confirmation bar rather than its earlier structure bar", async () => {
    await render(createElement(StockPriceChart, { timeframe: fixture().results[0].timeframes.find((tf) => tf.timeframe === "1d")! }));
    const markerTitle = [...host.querySelectorAll("svg text > title")].find((title) => title.textContent?.includes("一类买点"));
    expect(markerTitle?.textContent).toContain("记录确认时间 2026-09-03 15:00");
    // Three bars: third center is 68 + 2.5 * (880 - 68) / 3.
    expect(Number(markerTitle?.parentElement?.getAttribute("x"))).toBeCloseTo(744.6666667);
    expect(markerTitle?.textContent).toContain("执行状态未提供");
    expect(markerTitle?.parentElement?.textContent).toMatch(/W$/);
  });

  it("distinguishes an enabled buy-direction event from an observe-only event", async () => {
    const observed = fixture().results[0].timeframes.find((tf) => tf.timeframe === "1d")!;
    observed.fresh_signals[0].execution_mode = "observe_only";
    observed.fresh_signals[0].actionable = false;
    await render(createElement(StockPriceChart, { timeframe: observed }));
    let markerTitle = [...host.querySelectorAll("svg text > title")].find((title) => title.textContent?.includes("一类买点"));
    expect(markerTitle?.textContent).toContain("仅观察");
    expect(markerTitle?.parentElement?.textContent).toMatch(/W$/);

    observed.fresh_signals[0].execution_mode = "enabled";
    observed.fresh_signals[0].actionable = true;
    await render(createElement(StockPriceChart, { timeframe: observed }));
    markerTitle = [...host.querySelectorAll("svg text > title")].find((title) => title.textContent?.includes("一类买点"));
    expect(markerTitle?.textContent).toContain("策略启用");
    expect(markerTitle?.parentElement?.textContent).toMatch(/B$/);
  });
});

describe("report loader request lifecycle", () => {
  it("renders server errors and invokes retry to remount and recover", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(response({ error: "暂时不可读" }, false)).mockResolvedValueOnce(response(fixture()));
    const retry = vi.fn();
    await render(createElement(StockReportLoader, { key: "12:0", jobId: 12, onRetry: retry }));
    expect(element('[role="alert"]').textContent).toContain("暂时不可读");
    await click(buttonWithin('[role="alert"]', "重新读取"));
    expect(retry).toHaveBeenCalledOnce();
    await render(createElement(StockReportLoader, { key: "12:1", jobId: 12, onRetry: retry }));
    expect(host.querySelector("h2")?.textContent).toContain("甲股票");
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("handles network failures and malformed response formats", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("network unavailable"));
    await render(createElement(StockReportLoader, { key: "fail", jobId: 12, onRetry: vi.fn() }));
    expect(element('[role="alert"]').textContent).toContain("network unavailable");
    vi.mocked(fetch).mockResolvedValueOnce(response({ results: null }));
    await render(createElement(StockReportLoader, { key: "invalid", jobId: 12, onRetry: vi.fn() }));
    expect(element('[role="alert"]').textContent).toContain("报告格式不完整");
  });

  it("aborts the old keyed task and ignores its late response after the new task resolves", async () => {
    const older = deferred<Response>();
    const newer = deferred<Response>();
    const fetchMock = vi.mocked(fetch).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
    await render(createElement(StockReportLoader, { key: "12", jobId: 12, onRetry: vi.fn() }));
    expect(element('[role="status"]').textContent).toContain("正在读取个股报告");
    const oldSignal = (fetchMock.mock.calls[0][1] as RequestInit).signal!;
    await render(createElement(StockReportLoader, { key: "13", jobId: 13, onRetry: vi.fn() }));
    expect(oldSignal.aborted).toBe(true);
    const newSource = fixture();
    newSource.results = [{ ...newSource.results[1], name: "新任务报告" }];
    await act(async () => { newer.resolve(response(newSource)); });
    expect(host.querySelector("h2")?.textContent).toContain("新任务报告");
    await act(async () => { older.resolve(response(fixture())); });
    expect(host.querySelector("h2")?.textContent).toContain("新任务报告");
    expect(host.querySelector("h2")?.textContent).not.toContain("甲股票");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(["/api/jobs/12/data", "/api/jobs/13/data"]);
  });
});
