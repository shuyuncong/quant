// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ScanProgress } from "../scan-progress";

let host: HTMLDivElement;
let root: Root;

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
  await act(async () => {
    await Promise.resolve();
  });
  await act(async () => {
    await Promise.resolve();
  });
}

describe("ScanProgress", () => {
  it("renders overall and per-strategy bars from the job payload", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          jobs: [
            {
              id: 9,
              status: "running",
              error: null,
              created_at: "2026-10-23 17:00:00",
              payload: {
                scan_version: 2,
                progress: {
                  macd_zero_axis: {
                    coverage: 1,
                    candidate_count: 4,
                    completed_round: true,
                  },
                  yearline_pullback: {
                    coverage: 0.5,
                    candidate_count: 2,
                    completed_round: false,
                  },
                },
              },
            },
          ],
        }),
      } as Response)
    );

    await act(async () => root.render(createElement(ScanProgress)));
    await settle();

    expect(host.textContent).toContain("最近筛选 #9");
    expect(host.textContent).toContain("整体进度");
    expect(host.textContent).toContain("50.0%");
    expect(host.textContent).toContain("日线零轴金叉");
    expect(host.textContent).toContain("已完成");
    expect(host.textContent).toContain("年线趋势");
    expect(host.textContent).toContain("零轴＋底背离");
    expect(host.textContent).toContain("候选在整轮完成后才入池");

    const bars = [...host.querySelectorAll<HTMLElement>('[role="progressbar"]')];
    expect(bars).toHaveLength(4);
    expect(bars[0].getAttribute("aria-valuenow")).toBe("50");
    expect(bars[1].getAttribute("aria-valuenow")).toBe("100");
    expect(bars[2].getAttribute("aria-valuenow")).toBe("50");
    expect(bars[3].getAttribute("aria-valuenow")).toBe("0");
  });
});
