import { PGlite } from "@electric-sql/pglite";
import fs from "node:fs";
import { beforeAll, describe, expect, it, vi } from "vitest";

/**
 * 用真实 Postgres（pglite/WASM）跑 /api/analyses 的 SQL。
 *
 * 这里不能再 mock `db.query`：本文件覆盖的正是**只有真库才会暴露**的三类问题
 *   1. 子查询作用域 —— `WHERE p.symbol = symbol` 里的 symbol 会绑定到内层表的同名列，
 *      变成 p.symbol = p.symbol，Postgres 报 21000（本项目真实踩过）；
 *   2. 跨表代码格式不统一 —— 池/持仓可能存 `605296`、`605296.SH` 或 `SH605296`，
 *      直接相等比较会静默匹配不上，名称回退形同虚设；
 *   3. 载荷体积 —— `SELECT *` 会把整份 document（含 ~118KB 的 timeframes）读进内存。
 */

let db: PGlite;

vi.mock("@/lib/db", () => ({
  getDb: async () => ({
    query: async (text: string, values?: readonly unknown[]) => {
      const result = await db.query(text, values ? [...values] : undefined);
      return { rows: result.rows };
    },
  }),
}));

import { GET } from "./route";

const slimDocument = (symbol: string, name: string) => JSON.stringify({
  schema_version: 2,
  symbol,
  name,
  as_of: "2026-10-13",
  snapshot_hash: "h",
  revision: 1,
  // 正文：真实记录里 timeframes 占 ~118KB，这里放 200KB 以确保它不进入响应
  report: { results: [{ timeframes: { "1m": "x".repeat(200_000) } }] },
  portfolio: { cash: 1, positions: "y".repeat(20_000) },
  strategies: [{ strategy_id: "macd_zero_axis", name: "日线零轴金叉", buy: true, sell: null, buy_conditions: [], sell_conditions: [], warnings: [] }],
  technical: { status: "success", content: "技术分析正文" },
  synthesis: { status: "success", content: "综合结论正文" },
});

// 全量套件里多个测试文件并行，WASM 编译 + 建 schema 会超过默认 10s 的 hook 超时，
// 故显式放大；单独跑时约 3s。
const SETUP_TIMEOUT_MS = 60_000;

beforeAll(async () => {
  db = new PGlite();
  // 直接吃生产 schema，避免测试用的建表语句与线上漂移
  await db.exec(fs.readFileSync("db/schema.sql", "utf8"));
  await db.exec(`INSERT INTO quant.jobs (id,kind,status,payload,created_at) OVERRIDING SYSTEM VALUE
    VALUES (733,'scan','success','{}','2026-10-13T14:00:00'),(732,'scan','success','{}','2026-10-13T14:00:00')`);

  // 7 条记录：偶数序号 name 为空（走回退），奇数序号自带 name
  for (let i = 0; i < 7; i++) {
    const symbol = `60005${i}.SH`;
    const own = i % 2 === 0 ? "" : `自带名${i}`;
    await db.query(
      "INSERT INTO quant.stock_analyses(job_id,symbol,name,document,created_at,updated_at) VALUES($1,$2,$3,$4,$5,$5)",
      [i < 4 ? 733 : 732, symbol, own, slimDocument(symbol, own), "2026-10-13T14:15:47"],
    );
  }
  // 池存"纯代码"，持仓存"交易所前缀"，记录是"代码.交易所"——三种格式都要能匹配上
  await db.exec(`INSERT INTO quant.stock_pool(symbol,name,source,created_at) VALUES
    ('600050','池内名称甲','manual','2026-10-13'),
    ('600052','池内名称乙','manual','2026-10-13')`);
  await db.exec(`INSERT INTO quant.holdings(symbol,name,created_at,updated_at) VALUES
    ('SH600054','持仓名称丙','2026-10-13','2026-10-13'),
    ('sz600056','持仓名称丁','2026-10-13','2026-10-13')`);
}, SETUP_TIMEOUT_MS);

async function call(query: string) {
  const response = await GET(new Request(`http://localhost/api/analyses?${query}`));
  return { status: response.status, body: await response.json() as Record<string, unknown> };
}

type Row = { id: number; symbol: string; name: string; document: Record<string, unknown> };

describe("GET /api/analyses against real Postgres", () => {
  it("pages with LIMIT 5 / OFFSET and reports the full total", async () => {
    const first = await call("page=1&symbol=");
    expect(first.status).toBe(200);
    expect(first.body.total).toBe(7);
    expect((first.body.records as Row[]).map((r) => r.symbol)).toHaveLength(5);

    const second = await call("page=2&symbol=");
    expect((second.body.records as Row[]).map((r) => r.symbol)).toEqual(["600051.SH", "600050.SH"]);
  });

  it("filters by job and symbol with matching counts", async () => {
    const byJob = await call("page=1&symbol=&job_id=732");
    expect(byJob.body.total).toBe(3);
    const bySymbol = await call("page=1&symbol=600052");
    expect(bySymbol.body.total).toBe(1);
    expect((bySymbol.body.records as Row[])[0].name).toBe("池内名称乙");
  });

  it("clamps an out-of-range page and rejects a negative job id", async () => {
    for (const query of ["page=0&symbol=", "page=-5&symbol=", "page=abc&symbol="]) {
      expect((await call(query)).body.page).toBe(1);
    }
    // job_id=0 会被 `|| null` 归一成"不过滤"（既有行为）；负数才走到校验分支。
    const invalidJob = await GET(new Request("http://localhost/api/analyses?page=1&symbol=&job_id=-3"));
    expect(invalidJob.status).toBe(422);
  });

  it("falls back to pool/holdings names across stored symbol formats", async () => {
    const { body } = await call("page=1&symbol=");
    const names = Object.fromEntries((body.records as Row[]).map((r) => [r.symbol, r.name]));
    // 记录自带 name 优先
    expect(names["600055.SH"]).toBe("自带名5");
    expect(names["600053.SH"]).toBe("自带名3");
    // 池存纯代码 / 持仓存大小写前缀，都要能回退命中
    expect(names["600052.SH"]).toBe("池内名称乙");
    expect(names["600054.SH"]).toBe("持仓名称丙");
    expect(names["600056.SH"]).toBe("持仓名称丁");
  });

  it("keeps the list payload small while retaining every field the tabs render", async () => {
    const { body } = await call("page=1&symbol=");
    const records = body.records as Row[];
    for (const record of records) {
      expect(record.document).not.toHaveProperty("report");
      expect(record.document).not.toHaveProperty("portfolio");
      for (const key of ["strategies", "technical", "synthesis", "as_of", "revision", "symbol"]) {
        expect(record.document).toHaveProperty(key);
      }
    }
    // 夹具里光 report 就有 200KB；瘦身后整个响应应远小于单条正文
    expect(JSON.stringify(body).length).toBeLessThan(50_000);
  });

  it("serializes bigint ids as numbers", async () => {
    const { body } = await call("page=1&symbol=");
    const record = (body.records as Row[])[0];
    expect(typeof record.id).toBe("number");
    expect(typeof (body.records as unknown as { job_id: unknown }[])[0].job_id).toBe("number");
  });
});
