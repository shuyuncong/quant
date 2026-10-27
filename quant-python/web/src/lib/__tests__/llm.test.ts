import { describe, expect, it } from "vitest";
import { buildCandidateReviewContext, buildInterpretationContext, readStreamContent, resolveProxy, INTERPRET_SYSTEM_PROMPT, extractStandpoints } from "../llm";

function bars(prefix: string, count: number) {
  return Array.from({ length: count }, (_, index) => ({
    datetime: `${prefix}-${String(index).padStart(2, "0")}`,
    open: index,
    high: index + 1,
    low: index - 1,
    close: index + 0.5,
    volume: 1000 + index,
  }));
}

function sseBody(pieces: string[]): { body: globalThis.ReadableStream } {
  let index = 0;
  return {
    body: new ReadableStream({
      pull(controller) {
        if (index < pieces.length) {
          controller.enqueue(new TextEncoder().encode(pieces[index++]));
          return;
        }
        controller.close();
      },
    }),
  };
}

describe("resolveProxy", () => {
  const profile = { proxy: "http://db-proxy:7890" } as never;

  it("prefers MODEL_PROXY over the DB proxy", () => {
    process.env.MODEL_PROXY = "http://env-model:1";
    delete process.env.HTTPS_PROXY;
    delete process.env.HTTP_PROXY;
    expect(resolveProxy(profile)).toBe("http://env-model:1");
    delete process.env.MODEL_PROXY;
  });

  it("falls back to HTTPS_PROXY then HTTP_PROXY", () => {
    delete process.env.MODEL_PROXY;
    process.env.HTTPS_PROXY = "http://env-https:2";
    expect(resolveProxy(profile)).toBe("http://env-https:2");
    delete process.env.HTTPS_PROXY;
    process.env.HTTP_PROXY = "http://env-http:3";
    expect(resolveProxy(profile)).toBe("http://env-http:3");
    delete process.env.HTTP_PROXY;
  });

  it("uses the DB proxy when no env var is set", () => {
    delete process.env.MODEL_PROXY;
    delete process.env.HTTPS_PROXY;
    delete process.env.HTTP_PROXY;
    expect(resolveProxy(profile)).toBe("http://db-proxy:7890");
  });
});

describe("readStreamContent", () => {
  it("accumulates content across SSE chunks and stops at [DONE]", async () => {
    // Chunk boundaries deliberately split an event across reads.
    const response = sseBody([
      'data: {"choices":[{"delta":{"content":"你"}}]}\n\ndata: {"choices":[{"de',
      'lta":{"content":"好"}}]}\n\ndata: [DONE]\n\n',
    ]);
    await expect(readStreamContent(response as never)).resolves.toBe("你好");
  });

  it("ignores heartbeat and non-data lines", async () => {
    const response = sseBody([
      ": keep-alive\n\n",
      'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
      'event: ping\ndata: {"choices":[{"delta":{"content":""}}]}\n\n',
      "data: [DONE]\n\n",
    ]);
    await expect(readStreamContent(response as never)).resolves.toBe("ok");
  });

  it("throws without a body", async () => {
    await expect(readStreamContent({ body: null } as never)).rejects.toThrow("模型流式响应无 body");
  });

  it("throws when the stream carries no content", async () => {
    await expect(readStreamContent(sseBody(["data: [DONE]\n\n"]) as never)).rejects.toThrow(
      "模型流式返回内容为空",
    );
  });
});

describe("buildInterpretationContext", () => {
  it("keeps every stock and timeframe while compacting long bar arrays", () => {
    const report = {
      mode: "analyze",
      results: ["000001.SZ", "600036.SH"].map((symbol) => ({
        symbol,
        name: symbol,
        timeframes: {
          "1m": { status: "ok", indicators: { dif: 1 }, chan: {}, recent_bars: bars(symbol, 100) },
          "1d": { status: "ok", indicators: { dif: 2 }, chan: {}, recent_bars: bars(symbol, 100) },
        },
      })),
    };
    const context = buildInterpretationContext(JSON.stringify(report), 20_000);
    const parsed = JSON.parse(context) as typeof report;
    expect(parsed.results.map((item) => item.symbol)).toEqual(["000001.SZ", "600036.SH"]);
    for (const result of parsed.results) {
      expect(Object.keys(result.timeframes)).toEqual(["1m", "1d"]);
      expect(result.timeframes["1m"].recent_bars.length).toBeLessThan(100);
    }
  });

  it("keeps strategy results so the model can quote reduction levels instead of guessing", () => {
    // 档位触发价/建议股数/保本止损位只存在于 strategies 里：压缩时丢掉它，
    // AI 解读就只能自己估仓位，与引擎分批发出的指令对不上。
    const report = {
      mode: "analyze",
      results: [{
        symbol: "600036.SH",
        name: "招商银行",
        strategies: [{
          strategy_id: "macd_zero_axis",
          name: "日线零轴金叉",
          sell: true,
          exit_plan_advice: { taken: 0, pending: 1, next: { trigger_price: 11.5, quantity: 500 }, summary: "下一档 +15% 触发价 11.500，卖出 500 股" },
        }],
        timeframes: { "1d": { status: "ok", indicators: { dif: 2 }, chan: {}, recent_bars: bars("600036.SH", 100) } },
      }],
    };
    const parsed = JSON.parse(buildInterpretationContext(JSON.stringify(report), 20_000)) as typeof report;
    const advice = parsed.results[0].strategies[0].exit_plan_advice;
    expect(advice.next.trigger_price).toBe(11.5);
    expect(advice.next.quantity).toBe(500);
  });

  it("keeps the read-only regime guidance for the AI explanation", () => {
    const guidance = {
      regime: "bear",
      regime_label: "熊市/下行",
      primary_name: "零轴＋底背离",
      summary: "深熊优先底背离。",
      caveats: ["没有正 Sharpe CI。"],
    };
    const report = {
      mode: "analyze",
      regime_guidance: guidance,
      results: [{
        symbol: "600036.SH",
        name: "招商银行",
        regime_guidance: guidance,
        timeframes: { "1d": { status: "ok", indicators: { dif: 2 }, chan: {}, recent_bars: bars("600036.SH", 100) } },
      }],
    };
    const parsed = JSON.parse(buildInterpretationContext(JSON.stringify(report), 20_000)) as typeof report;
    expect(parsed.regime_guidance.primary_name).toBe("零轴＋底背离");
    expect(parsed.results[0].regime_guidance.regime).toBe("bear");
  });
});

describe("buildCandidateReviewContext", () => {
  it("keeps gates, pool, candidate counts and data freshness", () => {
    const report = {
      mode: "scan",
      scanned_at: "2026-10-24T18:04:31+08:00",
      universe_mode: "all_a",
      universe_size: 5027,
      coverage: 1,
      completed_round: true,
      candidate_count: 1,
      observed_count: 0,
      market_gate: { today: { allows_entries: false, blocked_by: ["ma10_latch_bear"] } },
      market_gate_signals: { this_run: { signals_screened: 1, blocked_by_market_gate: 1 } },
      stock_pool: { config: { min_market_cap: 50 }, rejection_details: [], data_errors: [] },
      fundamental: { enabled: false, details: [] },
      stale_symbol_count: 2,
      expected_trade_date: "2026-10-23",
      errors: [],
      candidates: [
        {
          symbol: "002100",
          name: "天康生物",
          execution_mode: "observe_only",
          entry_gate_blocked: true,
          observe_reason: "market_gate_closed",
          stock_pool_warnings: [],
          fundamental_warnings: [],
        },
      ],
      observed_candidates: [],
    };
    const parsed = JSON.parse(
      buildCandidateReviewContext(JSON.stringify(report), 20_000)
    ) as Record<string, unknown>;
    expect(parsed.coverage).toBe(1);
    expect(parsed.market_gate).toEqual(report.market_gate);
    expect(parsed.market_gate_signals).toEqual(report.market_gate_signals);
    expect(parsed.stock_pool).toMatchObject(report.stock_pool);
    expect(parsed.candidate_summary).toMatchObject({
      count: 1,
      observe_only_count: 1,
      entry_gate_blocked_count: 1,
      symbols: ["002100"],
    });
    expect(parsed.data_freshness).toMatchObject({
      stale_symbol_count: 2,
      expected_trade_date: "2026-10-23",
    });
  });
});

describe("INTERPRET_SYSTEM_PROMPT", () => {
  it("requires explicit action stances tied to holdings", () => {
    expect(INTERPRET_SYSTEM_PROMPT).toContain("加仓");
    expect(INTERPRET_SYSTEM_PROMPT).toContain("减仓");
    expect(INTERPRET_SYSTEM_PROMPT).toContain("持有");
    expect(INTERPRET_SYSTEM_PROMPT).toContain("清仓");
    expect(INTERPRET_SYSTEM_PROMPT).toContain("可建仓");
    expect(INTERPRET_SYSTEM_PROMPT).toContain("暂不建仓");
    expect(INTERPRET_SYSTEM_PROMPT).toContain("操作主张");
  });
});

describe("extractStandpoints", () => {
  it("parses the action standpoint section with symbols and reasons", () => {
    const content = `## 操作主张
- **600036.SH 招商银行**：持有（日线仍在均线上方，浮盈 +10%）
- **000001.SZ 平安银行**：可建仓（回踩确认，建议仓位 10%）

### 详细分析
...`;
    expect(extractStandpoints(content)).toEqual([
      "招商银行(600036.SH)：持有，日线仍在均线上方，浮盈 +10%",
      "平安银行(000001.SZ)：可建仓，回踩确认，建议仓位 10%",
    ]);
  });

  it("returns empty when no standpoint section exists", () => {
    expect(extractStandpoints("## 详细分析\n无主张内容")).toEqual([]);
    expect(extractStandpoints("")).toEqual([]);
  });

  it("ignores lines without a recognized stance word", () => {
    const content = `## 操作主张
- **600036.SH 招商银行**：持有
- 一些其他说明
- 暂无操作主张`;
    expect(extractStandpoints(content)).toEqual(["招商银行(600036.SH)：持有"]);
  });

  it("keeps a stance in a bullet without symbol", () => {
    const content = `## 操作主张
- 招商银行：持有（逻辑类似）
`;
    expect(extractStandpoints(content)).toEqual(["招商银行：持有，逻辑类似"]);
  });
});
