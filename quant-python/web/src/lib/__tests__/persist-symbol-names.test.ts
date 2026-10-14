import { PGlite } from "@electric-sql/pglite";
import fs from "node:fs";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type * as BridgeModule from "../bridge";
import type * as DbModule from "../db";

/**
 * `persistSymbolNames` 的写入语义。
 *
 * 这里必须用真库（pglite/WASM）而不是 mock 掉 SQL：被测的正是
 * `payload::jsonb || $2::jsonb` 这条**合并表达式**。
 * 换成 `SET payload = $1` 的整体覆盖写法时，并发的 `progress` 会被丢掉，
 * 而 mock 掉 query 的实现根本发现不了 —— 这与本仓库既有教训一致
 * （见 route.pg.test.ts 顶部关于子查询作用域 21000 的说明）。
 */

let db: PGlite;

vi.mock("../db", async (importOriginal) => {
  const actual = await importOriginal<typeof DbModule>();
  const query = async (text: string, values?: readonly unknown[]) => {
    const result = await db.query(text, values ? [...values] : undefined);
    return { rows: result.rows };
  };
  return {
    ...actual,
    // 只替换数据库传输层，其余逻辑保持真实。
    // getJob 必须一并替换：模块内部的调用不会被 vi.mock 拦截，它会绕回真实连接池。
    getDb: async () => ({ query }),
    getJob: (async (id: number) => {
      const { rows } = await query("SELECT * FROM quant.jobs WHERE id=$1", [id]);
      return rows[0] ?? null;
    }) as unknown as typeof actual.getJob,
  };
});

const bridge = vi.hoisted(() => ({ runBridge: vi.fn() }));
vi.mock("../bridge", async (importOriginal) => {
  const actual = await importOriginal<typeof BridgeModule>();
  return { ...actual, runBridge: bridge.runBridge };
});

import { persistSymbolNames } from "../jobs";

const JOB_ID = 733;

async function payloadOf(id: number): Promise<Record<string, unknown>> {
  const row = (await db.query("SELECT payload FROM quant.jobs WHERE id=$1", [id])).rows[0] as { payload: string };
  return JSON.parse(row.payload) as Record<string, unknown>;
}

beforeAll(async () => {
  db = new PGlite();
  await db.exec(fs.readFileSync("db/schema.sql", "utf8"));
}, 120_000);

beforeEach(async () => {
  vi.clearAllMocks();
  await db.exec(`DELETE FROM quant.jobs WHERE id=${JOB_ID}`);
});

describe("persistSymbolNames", () => {
  it("合并写入 symbol_names，且不动并发的 progress 与其它键", async () => {
    await db.query(
      "INSERT INTO quant.jobs(id,kind,status,payload,created_at) OVERRIDING SYSTEM VALUE VALUES($1,'analyze','running',$2,$3)",
      [JOB_ID, JSON.stringify({ symbols: ["600750.SH"], progress: { processed: 3, total: 10 }, analysis_version: 2 }), "2026-10-14T00:00:00"],
    );
    bridge.runBridge.mockResolvedValue({ ok: true, data: { names: { "600750": "华润江中" } } });

    await persistSymbolNames(JOB_ID, ["600750.SH"]);

    const payload = await payloadOf(JOB_ID);
    expect(payload.symbol_names).toEqual({ "600750": "华润江中" });
    expect(payload.progress).toEqual({ processed: 3, total: 10 }); // 覆盖式写法会把它丢掉
    expect(payload.symbols).toEqual(["600750.SH"]);
    expect(payload.analysis_version).toBe(2);
  });

  it("解析失败时保持 payload 原样（名称只是展示信息）", async () => {
    const original = { symbols: ["600750.SH"], progress: { processed: 1, total: 2 } };
    await db.query(
      "INSERT INTO quant.jobs(id,kind,status,payload,created_at) OVERRIDING SYSTEM VALUE VALUES($1,'analyze','running',$2,$3)",
      [JOB_ID, JSON.stringify(original), "2026-10-14T00:00:00"],
    );

    for (const outcome of [
      { ok: false, error: "bridge 挂了" },
      { ok: true, data: {} },
      { ok: true, data: { names: null } },
      { ok: true, data: { names: ["不是对象"] } },
    ]) {
      bridge.runBridge.mockResolvedValue(outcome);
      await persistSymbolNames(JOB_ID, ["600750.SH"]);
      expect(await payloadOf(JOB_ID)).toEqual(original);
    }

    bridge.runBridge.mockRejectedValue(new Error("子进程起不来"));
    await persistSymbolNames(JOB_ID, ["600750.SH"]);
    expect(await payloadOf(JOB_ID)).toEqual(original);
  });

  it("不传 symbols 时从任务 payload 里取标的", async () => {
    await db.query(
      "INSERT INTO quant.jobs(id,kind,status,payload,created_at) OVERRIDING SYSTEM VALUE VALUES($1,'monitor-cycle','running',$2,$3)",
      [JOB_ID, JSON.stringify({ symbols: ["600750.SH", "600755.SH"] }), "2026-10-14T00:00:00"],
    );
    bridge.runBridge.mockResolvedValue({ ok: true, data: { names: { "600750": "华润江中", "600755": "厦门国贸" } } });

    await persistSymbolNames(JOB_ID);

    expect(bridge.runBridge).toHaveBeenCalledWith("resolve-names", { symbols: ["600750.SH", "600755.SH"] }, { timeoutMs: 20_000 });
    expect((await payloadOf(JOB_ID)).symbol_names).toEqual({ "600750": "华润江中", "600755": "厦门国贸" });
  });

  it("标的目标为空时不调用 bridge", async () => {
    await db.query(
      "INSERT INTO quant.jobs(id,kind,status,payload,created_at) OVERRIDING SYSTEM VALUE VALUES($1,'monitor-cycle','running',$2,$3)",
      [JOB_ID, JSON.stringify({ symbols: [] }), "2026-10-14T00:00:00"],
    );
    await persistSymbolNames(JOB_ID);
    expect(bridge.runBridge).not.toHaveBeenCalled();
  });

  it("重复调用是幂等的（payload 不会被反复包一层）", async () => {
    await db.query(
      "INSERT INTO quant.jobs(id,kind,status,payload,created_at) OVERRIDING SYSTEM VALUE VALUES($1,'analyze','running',$2,$3)",
      [JOB_ID, JSON.stringify({ symbols: ["600750.SH"] }), "2026-10-14T00:00:00"],
    );
    bridge.runBridge.mockResolvedValue({ ok: true, data: { names: { "600750": "华润江中" } } });

    await persistSymbolNames(JOB_ID, ["600750.SH"]);
    await persistSymbolNames(JOB_ID, ["600750.SH"]);

    expect(await payloadOf(JOB_ID)).toEqual({ symbols: ["600750.SH"], symbol_names: { "600750": "华润江中" } });
  });
});
