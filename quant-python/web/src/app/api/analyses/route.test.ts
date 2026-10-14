import { describe, expect, it, vi } from "vitest";

/**
 * /api/analyses 的分页契约 + 列表载荷体积契约。
 *
 * 这里只关心 SQL 参数与响应外形：page N -> OFFSET (N-1)*5、total 是全集数、
 * 列表不带 report/portfolio（单条 ~129KB，列表按 5 条翻页会放大到 ~0.6–1.2MB，
 * 且 10 分钟静默轮询会重传一遍——那是"翻页点了像没反应"的来源）。
 */
const state: { sqls: { text: string; values: unknown[] }[] } = { sqls: [] };

const bigReport = { results: [{ timeframes: { "1m": "x".repeat(50_000) } }] };
const bigPortfolio = { cash: 1, positions: "y".repeat(20_000) };

vi.mock("@/lib/db", () => ({
  getDb: async () => ({
    query: async (text: string, values?: readonly unknown[]) => {
      state.sqls.push({ text, values: [...(values ?? [])] });
      if (/COUNT\(\*\)/i.test(text)) return { rows: [{ count: "12" }] };
      const offset = Number((values ?? [])[2] ?? 0);
      const from = offset;
      return {
        rows: Array.from({ length: Math.max(0, Math.min(5, 12 - from)) }, (_, i) => ({
          id: String(100 - from - i),
          job_id: "1",
          symbol: `60000${from + i}.SH`,
          name: `股票${from + i}`,
          status: "success",
          document: {
            schema_version: 2,
            symbol: `60000${from + i}.SH`,
            name: `股票${from + i}`,
            as_of: "2026-09-01",
            snapshot_hash: "abc",
            report: bigReport,
            portfolio: bigPortfolio,
            strategies: [{ strategy_id: "macd_zero_axis", buy: true }],
            technical: { status: "success", content: "t" },
            synthesis: { status: "success", content: "s" },
            revision: 1,
          },
          created_at: "2026-09-01T15:00:00",
          updated_at: "2026-09-01T15:00:00",
        })),
      };
    },
  }),
}));

import { GET } from "./route";

async function call(query: string) {
  state.sqls = [];
  const response = await GET(new Request(`http://localhost/api/analyses?${query}`));
  return { status: response.status, body: await response.json() as Record<string, unknown> };
}

describe("GET /api/analyses pagination", () => {
  it("returns the second page with OFFSET 5 and the full total", async () => {
    const { status, body } = await call("page=2&symbol=");
    expect(status).toBe(200);
    expect(body.total).toBe(12);
    expect(body.page).toBe(2);
    expect(body.page_size).toBe(5);
    const records = body.records as { id: number; symbol: string }[];
    expect(records).toHaveLength(5);
    expect(records[0].symbol).toBe("600005.SH");

    const select = state.sqls.find((entry) => /SELECT \* FROM quant\.stock_analyses/i.test(entry.text));
    expect(select, "select ran").toBeDefined();
    expect(select!.values).toEqual([null, null, 5]);
  });

  it("clamps page below 1 to the first page", async () => {
    const { body } = await call("page=0&symbol=");
    expect(body.page).toBe(1);
    const select = state.sqls.find((entry) => /SELECT \* FROM quant\.stock_analyses/i.test(entry.text));
    expect(select!.values[2]).toBe(0);
  });

  it("counts with the same filters as the page query", async () => {
    const { body } = await call("page=1&symbol=600519&job_id=7");
    expect(body.total).toBe(12);
    const count = state.sqls.find((entry) => /COUNT\(\*\)/i.test(entry.text));
    const select = state.sqls.find((entry) => /SELECT \* FROM quant\.stock_analyses/i.test(entry.text));
    expect(count!.values.slice(0, 2)).toEqual([7, "600519"]);
    expect(count!.values.slice(0, 2)).toEqual(select!.values.slice(0, 2));
  });

  it("serializes bigint ids as numbers so the client can key rows", async () => {
    const { body } = await call("page=1&symbol=");
    const record = (body.records as Record<string, unknown>[])[0];
    expect(typeof record.id).toBe("number");
    expect(typeof record.job_id).toBe("number");
  });

  it("drops report/portfolio from the list payload but keeps what the tabs render", async () => {
    const { body } = await call("page=1&symbol=");
    const records = body.records as { document: Record<string, unknown> }[];
    for (const record of records) {
      expect(record.document).not.toHaveProperty("report");
      expect(record.document).not.toHaveProperty("portfolio");
      // 五页签与一致度徽标依赖的字段必须在
      expect(record.document).toHaveProperty("strategies");
      expect(record.document).toHaveProperty("technical");
      expect(record.document).toHaveProperty("synthesis");
      expect(record.document).toHaveProperty("as_of");
      expect(record.document).toHaveProperty("revision");
    }
    // 载荷确实变小了：剥离前光 report 就有 5 万字符
    expect(JSON.stringify(records).length).toBeLessThan(20_000);
  });
});
