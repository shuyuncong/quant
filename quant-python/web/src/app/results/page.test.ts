// @vitest-environment jsdom
import { act, createElement, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/components/symbol-combobox", () => ({
  SymbolCombobox: ({ id, value, onChange }: { id?: string; value: string; onChange: (value: string) => void }) =>
    createElement("input", { id, value, onChange: (event: { target: { value: string } }) => onChange(event.target.value) }),
}));
vi.mock("@/components/stock-analysis-report", () => ({
  StockReportLoader: ({ jobId }: { jobId: number }) => createElement("div", { "data-report-job": jobId }, `report ${jobId}`),
}));
vi.mock("@/components/markdown-content", () => ({
  MarkdownContent: ({ content }: { content: string }) => createElement("div", { "data-markdown": true }, content),
}));
vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ open, children }: { open: boolean; children: ReactNode }) => open ? createElement("div", { "data-dialog": true }, children) : null,
  DialogContent: ({ children }: { children: ReactNode }) => createElement("div", null, children),
  DialogDescription: ({ children }: { children: ReactNode }) => createElement("p", null, children),
  DialogHeader: ({ children }: { children: ReactNode }) => createElement("div", null, children),
  DialogTitle: ({ children }: { children: ReactNode }) => createElement("h2", null, children),
}));
vi.mock("@/components/ui/tooltip", () => ({
  TooltipProvider: ({ children }: { children: ReactNode }) => createElement("div", null, children),
  Tooltip: ({ children }: { children: ReactNode }) => createElement("div", null, children),
  TooltipTrigger: ({ render }: { render: ReactNode }) => render,
  TooltipContent: () => null,
}));

import ResultsPage from "./page";

const jobs = [
  { id: 2, kind: "analyze", status: "success", payload: {}, symbol_names: "乙股票", result_path: "B.json", error: null, created_at: "2026-09-02T15:00:00", finished_at: "2026-09-02T15:01:00", note: null },
  { id: 1, kind: "analyze", status: "success", payload: {}, symbol_names: "甲股票", result_path: "A.json", error: null, created_at: "2026-09-01T15:00:00", finished_at: "2026-09-01T15:01:00", note: null },
];
const scanJob = { id: 3, kind: "scan", status: "success", payload: {}, symbol_names: "全市场扫描", result_path: "scan.json", error: null, created_at: "2026-09-03T15:00:00", finished_at: "2026-09-03T15:01:00", note: null };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function response(data: unknown, ok = true): Response {
  return { ok, status: ok ? 200 : 500, json: async () => data } as Response;
}
function rowFor(id: number, host: HTMLElement): HTMLTableRowElement {
  const row = [...host.querySelectorAll<HTMLTableRowElement>("tbody tr")].find((item) => item.textContent?.includes(`#${id}`));
  expect(row, `row #${id}`).toBeDefined();
  return row!;
}
function buttonIn(container: HTMLElement, label: string): HTMLButtonElement {
  const button = [...container.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent?.includes(label));
  expect(button, label).toBeDefined();
  return button!;
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

async function renderWith(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetchMock);
  await act(async () => root.render(createElement(ResultsPage)));
  await act(async () => {});
  expect(host.textContent).toContain("乙股票");
}

describe("results page request isolation", () => {
  it("keeps the stock report shell inside the analysis dialog", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/jobs") return Promise.resolve(response({ jobs }));
      return Promise.reject(new Error(`unexpected fetch ${url}`));
    });
    await renderWith(fetchMock);
    expect(host.querySelector(".stock-report-page")).toBeNull();
    await act(async () => buttonIn(rowFor(2, host), "查看 AI 分析").click());
    const dialog = host.querySelector<HTMLElement>("[data-dialog]");
    expect(dialog).not.toBeNull();
    expect(dialog!.textContent).toContain("研究报告");
    expect(dialog!.textContent).toContain("AI 解读");
    expect([...dialog!.querySelectorAll("[data-slot='tabs-trigger']")].map((tab) => tab.textContent)).toEqual(["AI 解读", "研究报告"]);
    expect(buttonIn(dialog!, "AI 解读").getAttribute("data-active")).not.toBeNull();
    expect(dialog!.querySelector(".stock-report-page")).toBeNull();
    await act(async () => buttonIn(dialog!, "研究报告").click());
    expect(dialog!.querySelector(".stock-report-page")).not.toBeNull();
    expect(dialog!.textContent).toContain("report 2");
    expect(host.firstElementChild?.className).toContain("gap-6");
    expect(host.firstElementChild?.className).not.toContain("stock-report-page");
  });

  it("keeps non-analysis task dialogs on the existing AI view", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/jobs") return Promise.resolve(response({ jobs: [scanJob] }));
      return Promise.reject(new Error(`unexpected fetch ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    await act(async () => root.render(createElement(ResultsPage)));
    await act(async () => {});
    await act(async () => buttonIn(rowFor(3, host), "查看 AI 分析").click());
    const dialog = host.querySelector<HTMLElement>("[data-dialog]");
    expect(dialog).not.toBeNull();
    expect(dialog!.textContent).toContain("AI 解读");
    expect(dialog!.textContent).toContain("该任务暂无 AI 解读");
    expect(dialog!.textContent).not.toContain("研究报告");
    expect(dialog!.querySelector(".stock-report-page")).toBeNull();
  });

  it("refreshes an open analysis dialog when the task finishes", async () => {
    const runningJob = { ...jobs[0], status: "running", result_path: null };
    let jobPoll = 0;
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/jobs") {
        jobPoll += 1;
        return Promise.resolve(response({ jobs: jobPoll === 1 ? [runningJob] : [jobs[0]] }));
      }
      return Promise.reject(new Error(`unexpected fetch ${url}`));
    });
    await renderWith(fetchMock);
    await act(async () => buttonIn(rowFor(2, host), "查看 AI 分析").click());
    expect(host.textContent).toContain("任务正在运行");
    expect(host.querySelector("[data-report-job='2']")).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    await act(async () => buttonIn(host, "研究报告").click());
    expect(host.querySelector("[data-report-job='2']")).not.toBeNull();
    expect(host.textContent).toContain("report 2");
  });

  it("ignores an older data-source response after another task is opened", async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/jobs") return Promise.resolve(response({ jobs }));
      if (url === "/api/jobs/1/data") return first.promise;
      if (url === "/api/jobs/2/data") return second.promise;
      return Promise.reject(new Error(`unexpected fetch ${url}`));
    });
    await renderWith(fetchMock);

    await act(async () => buttonIn(rowFor(1, host), "数据源").click());
    await act(async () => buttonIn(rowFor(2, host), "数据源").click());
    await act(async () => second.resolve(response({ mode: "analysis", analyzed_at: null, scanned_at: null, market_context: { index_code: "INDEX-B" }, delivery: null, results: [], candidates: [], errors: [] })));
    expect(host.textContent).toContain("数据源 #2");
    expect(host.textContent).toContain("INDEX-B");

    await act(async () => first.resolve(response({ mode: "analysis", analyzed_at: null, scanned_at: null, market_context: { index_code: "INDEX-A" }, delivery: null, results: [], candidates: [], errors: [] })));
    expect(host.textContent).toContain("数据源 #2");
    expect(host.textContent).toContain("INDEX-B");
    expect(host.textContent).not.toContain("INDEX-A");
  });

  it("does not attach a completed interpretation to a different open task", async () => {
    const interpretation = deferred<Response>();
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/jobs") return Promise.resolve(response({ jobs }));
      if (url === "/api/jobs/1/interpret") return interpretation.promise;
      return Promise.reject(new Error(`unexpected fetch ${url}`));
    });
    await renderWith(fetchMock);

    await act(async () => buttonIn(rowFor(1, host), "查看 AI 分析").click());
    expect(host.textContent).toContain("分析详情 #1");
    await act(async () => buttonIn(host, "AI 解读").click());
    await act(async () => buttonIn(host, "生成 AI 解读").click());
    await act(async () => buttonIn(rowFor(2, host), "查看 AI 分析").click());
    expect(host.textContent).toContain("分析详情 #2");
    await act(async () => buttonIn(host, "AI 解读").click());

    await act(async () => interpretation.resolve(response({ content: "A 的解读", model: "mock-model" })));
    expect(host.textContent).toContain("分析详情 #2");
    expect(host.textContent).toContain("该任务暂无 AI 解读");
    expect(host.textContent).not.toContain("A 的解读");
  });
});
