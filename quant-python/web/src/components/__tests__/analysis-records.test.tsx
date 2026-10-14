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

function response(list: typeof records): Response {
  return { ok: true, status: 200, json: async () => ({ records: list, total: list.length }) } as Response;
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
    const many = [...records, maotai, { ...records[0], id: 13 }, { ...records[0], id: 14 }, { ...records[0], id: 15 }];
    const fetchMock = vi.fn((url: string) => Promise.resolve(response(String(url).includes("page=2") ? [many[4]] : many)));
    await renderRecords(fetchMock);

    await act(async () => articleFor("平安银行", host).click());
    expect(expandedSymbols(host)).toEqual(["000001.SZ"]);

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
      const pageRecords = many.slice((page - 1) * 5, page * 5);
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ records: pageRecords, total: many.length, page, page_size: 5 }),
      } as Response);
    });
  }

  it("shows the next page records and hides the previous page", async () => {
    await renderRecords(pagedFetch());
    expect(host.textContent).toContain("股票0");
    expect(host.textContent).not.toContain("股票6");

    await act(async () => buttonIn(host, "下一页").click());
    await settle();

    expect(host.textContent).toContain("股票5");
    expect(host.textContent).not.toContain("股票0");
  });

  it("counts pages from total and disables 下一页 on the last page", async () => {
    await renderRecords(pagedFetch());
    expect(host.textContent).toContain("/ 3");

    await act(async () => buttonIn(host, "下一页").click());
    await settle();
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
    expect(host.textContent).not.toContain("股票5");
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
    expect(host.textContent).toContain("1 / 3");

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
    expect(host.textContent).toContain("2 / 3");
    expect(host.textContent).toContain("股票5");
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
