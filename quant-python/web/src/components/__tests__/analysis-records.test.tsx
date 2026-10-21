// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/components/five-tab-analysis", () => ({
  FiveTabAnalysis: ({ document }: { document: { symbol: string } }) =>
    createElement("div", { "data-open-record": document.symbol }, document.symbol),
}));

import { AnalysisRecords } from "../analysis-records";
import { ANALYSIS_PAGE_SIZE } from "@/lib/analysis-types";

const records = [
  { id: 11, job_id: 1, symbol: "600036.SH", name: "招商银行", status: "success", created_at: "2026-09-01T15:00:00", updated_at: "2026-09-01T15:00:00", document: { symbol: "600036.SH" } },
  { id: 12, job_id: 1, symbol: "000001.SZ", name: "平安银行", status: "success", created_at: "2026-09-01T15:00:00", updated_at: "2026-09-01T15:00:00", document: { symbol: "000001.SZ" } },
];
const maotai = { ...records[0], id: 99, symbol: "600519.SH", name: "贵州茅台", document: { symbol: "600519.SH" } };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

/** 组件现在先读 text() 再解析 JSON（以免把文本型 500 误报成 JSON 解析错误）。 */
function jsonResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  } as unknown as Response;
}

function response(list: typeof records): Response {
  return jsonResponse({ records: list, total: list.length });
}

function articleFor(symbol: string, host: HTMLElement): HTMLButtonElement {
  const article = [...host.querySelectorAll<HTMLElement>("article")].find((item) => item.textContent?.includes(symbol));
  expect(article, symbol).toBeDefined();
  return article!.querySelector<HTMLButtonElement>("button")!;
}

function buttonIn(container: HTMLElement, label: string): HTMLButtonElement {
  const button = [...container.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent === label);
  expect(button, label).toBeDefined();
  return button!;
}

function query(host: HTMLElement, value: string) {
  const input = host.querySelector<HTMLInputElement>("input[aria-label='按股票代码查询报告']")!;
  // React tracks the previous value on the DOM node; the native setter keeps the tracker stale.
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  buttonIn(host, "查询").click();
}

function expandedSymbols(host: HTMLElement): string[] {
  return [...host.querySelectorAll<HTMLElement>("[data-open-record]")].map((node) => node.dataset.openRecord!);
}

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.useFakeTimers();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

/** Flush the component's deferred load plus the resulting re-render. */
async function settle() {
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
}

/** 组件只会以 URL 调 fetch；测试用这个签名替身，避免笼统的 `vi.fn()` 掩盖参数形状。 */
type FetchStub = (url: string) => Promise<Response>;

async function renderRecords(fetchMock: FetchStub) {
  vi.stubGlobal("fetch", vi.fn(fetchMock));
  await act(async () => root.render(createElement(AnalysisRecords)));
  await settle();
}

describe("analysis records expansion", () => {
  it("keeps every report collapsed on first load", async () => {
    await renderRecords(vi.fn(() => Promise.resolve(response(records))));

    expect(host.textContent).toContain("招商银行");
    expect(articleFor("招商银行", host).getAttribute("aria-expanded")).toBe("false");
    expect(articleFor("平安银行", host).getAttribute("aria-expanded")).toBe("false");
    expect(expandedSymbols(host)).toEqual([]);
  });

  it("keeps a manually opened report open across polls and collapses on request", async () => {
    await renderRecords(vi.fn(() => Promise.resolve(response(records))));

    await act(async () => articleFor("平安银行", host).click());
    expect(expandedSymbols(host)).toEqual(["000001.SZ"]);

    // A 5-second poll must not change the user's selection or auto-open the first row.
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(expandedSymbols(host)).toEqual(["000001.SZ"]);

    await act(async () => articleFor("平安银行", host).click());
    expect(expandedSymbols(host)).toEqual([]);

    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(expandedSymbols(host)).toEqual([]);
  });

  it("collapses the open report when paging", async () => {
    const many = Array.from({ length: 12 }, (_, i) => ({
      ...records[0],
      id: 100 + i,
      symbol: `6001${String(i).padStart(2, "0")}.SH`,
      name: `股票${i}`,
      document: { ...records[0].document, symbol: `6001${String(i).padStart(2, "0")}.SH` },
    }));
    const fetchMock = vi.fn((url: string) => {
      const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
      return Promise.resolve(jsonResponse({
        records: many.slice((page - 1) * ANALYSIS_PAGE_SIZE, page * ANALYSIS_PAGE_SIZE),
        total: many.length,
        page,
        page_size: ANALYSIS_PAGE_SIZE,
      }));
    });
    await renderRecords(fetchMock);

    await act(async () => articleFor("股票1", host).click());
    expect(expandedSymbols(host)).toEqual(["600101.SH"]);

    await act(async () => buttonIn(host, "下一页").click());
    await settle();
    expect(expandedSymbols(host)).toEqual([]);
  });

  it("collapses the open report when the query changes", async () => {
    const fetchMock = vi.fn((url: string) => Promise.resolve(response(String(url).includes("symbol=600") ? [maotai] : records)));
    await renderRecords(fetchMock);

    await act(async () => articleFor("平安银行", host).click());
    expect(expandedSymbols(host)).toEqual(["000001.SZ"]);

    await act(async () => query(host, "600"));
    await settle();
    expect(host.textContent).toContain("贵州茅台");
    expect(expandedSymbols(host)).toEqual([]);
  });

  it("ignores a stale response that arrives after the query changed", async () => {
    const stale = deferred<Response>();
    const fresh = deferred<Response>();
    let call = 0;
    const fetchMock = vi.fn((url: string) => {
      if (String(url).includes("symbol=600")) return fresh.promise;
      call += 1;
      return call === 1 ? Promise.resolve(response(records)) : stale.promise;
    });
    await renderRecords(fetchMock);
    expect(host.textContent).toContain("招商银行");

    await act(async () => query(host, "600"));
    await settle();
    await act(async () => fresh.resolve(response([maotai])));
    expect(host.textContent).toContain("贵州茅台");

    // The stale pre-query filter must not repopulate the list or open any record.
    await act(async () => stale.resolve(response(records)));
    expect(host.textContent).toContain("贵州茅台");
    expect(host.textContent).not.toContain("招商银行");
    expect(expandedSymbols(host)).toEqual([]);
  });

  it("clears the open report when the polled list no longer contains it", async () => {
    let poll = 0;
    const fetchMock = vi.fn(() => {
      poll += 1;
      return Promise.resolve(response(poll === 1 ? records : [records[1]]));
    });
    await renderRecords(fetchMock);

    await act(async () => articleFor("招商银行", host).click());
    expect(expandedSymbols(host)).toEqual(["600036.SH"]);

    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(expandedSymbols(host)).toEqual([]);
    expect(host.textContent).not.toContain("招商银行");
  });
});

describe("analysis records pagination", () => {
  const many = Array.from({ length: 12 }, (_, i) => ({
    ...records[0],
    id: 200 - i,
    symbol: `6000${String(i).padStart(2, "0")}.SH`,
    name: `股票${i}`,
    document: { ...records[0].document, symbol: `6000${String(i).padStart(2, "0")}.SH` },
  }));

  /** 模拟真实 API：total 是全集数量，records 只含当前页。 */
  function pagedFetch() {
    return vi.fn((url: string) => {
      const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
      const pageRecords = many.slice((page - 1) * ANALYSIS_PAGE_SIZE, page * ANALYSIS_PAGE_SIZE);
      return Promise.resolve(jsonResponse({ records: pageRecords, total: many.length, page, page_size: ANALYSIS_PAGE_SIZE }));
    });
  }

  it("shows the next page records and hides the previous page", async () => {
    await renderRecords(pagedFetch());
    expect(host.textContent).toContain("股票0");
    expect(host.textContent).not.toContain("股票10");

    await act(async () => buttonIn(host, "下一页").click());
    await settle();

    expect(host.textContent).toContain("股票10");
    expect(host.textContent).not.toContain("股票0");
  });

  it("counts pages from total and disables 下一页 on the last page", async () => {
    await renderRecords(pagedFetch());
    expect(host.textContent).toContain("/ 2");

    await act(async () => buttonIn(host, "下一页").click());
    await settle();

    expect(host.textContent).toContain("股票10");
    expect(buttonIn(host, "下一页").disabled).toBe(true);
  });

  it("returns to the previous page", async () => {
    await renderRecords(pagedFetch());
    await act(async () => buttonIn(host, "下一页").click());
    await settle();
    await act(async () => buttonIn(host, "上一页").click());
    await settle();

    expect(host.textContent).toContain("股票0");
    expect(host.textContent).not.toContain("股票10");
  });

  it("ignores a second 下一页 click while the page request is still in flight", async () => {
    const gate = deferred<void>();
    const paged = pagedFetch();
    const slowFetch = vi.fn(async (url: string) => {
      // 只挡住第 2 页，首屏必须正常返回，否则按钮会因首屏未完成而本来就是灰的。
      if (String(url).includes("page=2")) await gate.promise;
      return paged(url);
    });
    await renderRecords(slowFetch);
    expect(host.textContent).toContain("1 / 2");

    const next = buttonIn(host, "下一页");
    await act(async () => {
      next.click();
      next.click();
    });
    // 让请求真正起飞（它是 setTimeout(0) 调度的），此时 gate 未放行 → 按钮应置灰。
    await settle();
    expect(buttonIn(host, "下一页").disabled).toBe(true);

    await act(async () => { gate.resolve(); });
    await settle();

    // 关键：只前进一页（不是两页），且只发一次 page=2。
    expect(host.textContent).toContain("2 / 2");
    expect(host.textContent).toContain("股票10");
    const pageTwoRequests = slowFetch.mock.calls.filter(([url]) => String(url).includes("page=2"));
    expect(pageTwoRequests).toHaveLength(1);
  });

  it("collapses the open report and returns to the top when paging", async () => {
    // jsdom 没有布局，scrollIntoView 未实现；补一个探针再断言它被调用。
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      await renderRecords(pagedFetch());

      await act(async () => articleFor("股票1", host).click());
      expect(expandedSymbols(host)).toEqual(["600001.SH"]);
      scrollIntoView.mockClear();

      await act(async () => buttonIn(host, "下一页").click());
      await settle();

      expect(expandedSymbols(host)).toEqual([]);
      expect(scrollIntoView).toHaveBeenCalled();
    } finally {
      delete (Element.prototype as unknown as Record<string, unknown>).scrollIntoView;
    }
  });
});

describe("analysis records 错误呈现", () => {
  it("把文本型 500 的响应体转成可读错误，而不是 JSON 解析失败的噪声", async () => {
    // 线上就是这样：路由抛未捕获异常 → Next.js 返回 text/plain 的 "Internal Server Error"
    await renderRecords(vi.fn(() =>
      Promise.resolve({
        ok: false,
        status: 500,
        text: async () => "Internal Server Error",
      } as unknown as Response),
    ));

    const alert = host.querySelector("[role='alert']");
    expect(alert, "应显示错误提示").toBeDefined();
    const message = alert!.textContent ?? "";
    expect(message).toContain("500");
    expect(message).toContain("Internal Server Error");
    // 不应再出现让人无从下手的 JSON 解析错误
    expect(message).not.toContain("Unexpected token");
  });

  it("接口返回非 JSON 但状态为 200 时给出明确提示", async () => {
    await renderRecords(vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        text: async () => "<!DOCTYPE html><html>…",
      } as unknown as Response),
    ));
    const message = host.querySelector("[role='alert']")?.textContent ?? "";
    expect(message).toContain("非 JSON");
  });

  it("接口 JSON 里带 error 字段时优先展示它", async () => {
    await renderRecords(vi.fn(() =>
      Promise.resolve({
        ok: false,
        status: 500,
        text: async () => JSON.stringify({ error: "读取分析记录失败：connect ECONNREFUSED 127.0.0.1:5432" }),
      } as unknown as Response),
    ));
    expect(host.querySelector("[role='alert']")?.textContent).toContain("ECONNREFUSED");
  });
});

describe("analysis records 信号与策略筛选", () => {
  it("点击策略按钮后把 strategy 传给接口并回到第一页", async () => {
    const urls: string[] = [];
    await renderRecords(vi.fn((url: string) => {
      urls.push(url);
      return Promise.resolve(response(records));
    }));

    await act(async () => buttonIn(host, "年线趋势").click());
    await settle();

    const last = urls.at(-1) ?? "";
    expect(last).toContain("strategy=yearline_pullback");
    expect(last).toContain("page=1");
  });

  it("买点/卖点信号可以和策略组合筛选", async () => {
    const urls: string[] = [];
    await renderRecords(vi.fn((url: string) => {
      urls.push(url);
      return Promise.resolve(response(records));
    }));

    await act(async () => buttonIn(host, "买点").click());
    await settle();
    expect(urls.at(-1)).toContain("signal=buy");

    await act(async () => buttonIn(host, "年线趋势").click());
    await settle();
    const combined = urls.at(-1) ?? "";
    expect(combined).toContain("signal=buy");
    expect(combined).toContain("strategy=yearline_pullback");

    await act(async () => buttonIn(host, "卖点").click());
    await settle();
    expect(urls.at(-1)).toContain("signal=sell");
  });

  it("徽标列出所有触发卖点的策略名", async () => {
    const record = {
      ...records[0],
      held: true,
      document: {
        ...records[0].document,
        strategies: [
          { strategy_id: "macd_zero_axis", name: "日线零轴金叉", buy: false, sell: true },
          { strategy_id: "yearline_pullback", name: "年线趋势", buy: false, sell: true },
          { strategy_id: "macd_divergence", name: "零轴＋底背离", buy: false, sell: false },
        ],
      },
    };
    await renderRecords(vi.fn(() => Promise.resolve(response([record]))));

    expect(host.textContent).toContain("建议离场 · 日线零轴金叉、年线趋势");
  });

  it("徽标列出所有触发买点的策略名", async () => {
    const record = {
      ...records[0],
      held: false,
      document: {
        ...records[0].document,
        strategies: [
          { strategy_id: "macd_zero_axis", name: "日线零轴金叉", buy: true, sell: false },
          { strategy_id: "yearline_pullback", name: "年线趋势", buy: true, sell: false },
          { strategy_id: "macd_divergence", name: "零轴＋底背离", buy: false, sell: false },
        ],
      },
    };
    await renderRecords(vi.fn(() => Promise.resolve(response([record]))));

    expect(host.textContent).toContain("买入共振 · 日线零轴金叉、年线趋势");
  });
});

describe("analysis records 持仓口径", () => {
  function sellRecord(held: boolean | null) {
    return {
      ...records[0],
      held,
      document: {
        ...records[0].document,
        strategies: [
          { strategy_id: "macd_zero_axis", name: "日线零轴金叉", buy: false, sell: true },
        ],
      },
    };
  }

  it("未持仓时只提示退出条件触发，不给出离场建议", async () => {
    await renderRecords(vi.fn(() => Promise.resolve(response([sellRecord(false)]))));

    expect(host.textContent).toContain("退出条件触发 · 未持仓");
    expect(host.textContent).not.toContain("建议离场");
  });

  it("有持仓时才显示建议离场", async () => {
    await renderRecords(vi.fn(() => Promise.resolve(response([sellRecord(true)]))));

    expect(host.textContent).toContain("建议离场 · 日线零轴金叉");
  });

  it("缺少持仓快照时标记为持仓未知，而不是当作空仓", async () => {
    await renderRecords(vi.fn(() => Promise.resolve(response([sellRecord(null)]))));

    expect(host.textContent).toContain("退出条件触发 · 持仓未知");
    expect(host.textContent).not.toContain("建议离场");
  });
});
