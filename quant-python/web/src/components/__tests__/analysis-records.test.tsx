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

async function renderRecords(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetchMock);
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
