import { PGlite } from "@electric-sql/pglite";
import fs from "node:fs";
import path from "node:path";
import { beforeAll, describe, expect, it } from "vitest";
// 脚本是 .mjs（`node scripts/...` 直接跑），走相对路径导入；类型来自脚本内的 JSDoc。
import { lookupTableNames } from "../../../scripts/backfill-analysis-names.mjs";

/**
 * 回填脚本的名称回退必须与 `/api/analyses` 同源。
 *
 * 线上真实故障：`/results` 页面明明显示了 `科创50ETF 588000.SZ`，回填脚本却报
 * `unresolved_codes: ["517520","588000"]` 并提示"名称表可能仍不可用"。
 * 原因不是名称表挂了 —— 而是**两条路数据源不同**：
 *   - 页面：`stock_analyses.name` → `payload.symbol_names` → **`stock_pool`/`holdings`**
 *   - 脚本（修复前）：只查行情名称表，而 `get_stock_list()` 是可交易股票 universe，
 *     刻意不含 ETF，ETF 在这条路上**永远**查不到。
 * 所以脚本必须补上池/持仓这条路，否则会一直自相矛盾。
 *
 * 用真库（pglite）测：被测的是 SQL 里的代码归一化与"空名不算命中"，
 * mock 掉 query 就什么都测不到。
 */

let db: PGlite;

beforeAll(async () => {
  db = new PGlite();
  const schema = fs.readFileSync(path.resolve(process.cwd(), "db/schema.sql"), "utf8");
  await db.exec(schema);
}, 120_000);

/** 最小 client —— 与脚本里 `client.query` 的形状一致。 */
const client = {
  query: async (text: string, values?: readonly unknown[]) => {
    const result = await db.query(text, values ? [...values] : undefined);
    return { rows: result.rows as Array<Record<string, unknown>> };
  },
};

const TS = "2026-01-01T00:00:00Z";

/** 两表的 NOT NULL 列不同（stock_pool 只有 created_at），故分开封装。 */
async function seedPool(symbol: string, name: string) {
  await db.query("INSERT INTO quant.stock_pool (symbol, name, created_at) VALUES ($1,$2,$3)", [
    symbol,
    name,
    TS,
  ]);
}

async function seedHolding(symbol: string, name: string) {
  await db.query(
    "INSERT INTO quant.holdings (symbol, name, created_at, updated_at) VALUES ($1,$2,$3,$3)",
    [symbol, name, TS],
  );
}

async function reset() {
  await db.exec("TRUNCATE quant.stock_pool, quant.holdings");
}

describe("backfill lookupTableNames", () => {
  it("从 holdings 解析 ETF 名称（本仓库线上踩到的真实场景）", async () => {
    await reset();
    await seedHolding("588000.SZ", "科创50ETF");
    const found = await lookupTableNames(client, ["588000"]);
    expect(found["588000"]).toBe("科创50ETF");
  });

  it("股票池与持仓都查，且同一代码以股票池为先", async () => {
    await reset();
    await seedPool("600750.SH", "华润江中");
    await seedHolding("600750.SH", "不应覆盖");
    await seedHolding("517520.SZ", "黄金ETF");
    const found = await lookupTableNames(client, ["600750", "517520"]);
    expect(found["600750"]).toBe("华润江中");
    expect(found["517520"]).toBe("黄金ETF");
  });

  it("跨写法归一化：SH600000 / SZ000001 / 带后缀都能命中", async () => {
    await reset();
    await seedPool("600000", "浦发银行");
    await seedHolding("SZ000001", "平安银行");
    const found = await lookupTableNames(client, ["600000", "000001"]);
    expect(found["600000"]).toBe("浦发银行");
    expect(found["000001"]).toBe("平安银行");
  });

  it("空名称不算命中 —— 否则会把回填写成空串，等于没修", async () => {
    await reset();
    await seedPool("000572.SZ", "   ");
    const found = await lookupTableNames(client, ["000572"]);
    expect(found["000572"]).toBeUndefined();
  });

  it("无待查代码时不查库", async () => {
    expect(await lookupTableNames(client, [])).toEqual({});
  });
});
