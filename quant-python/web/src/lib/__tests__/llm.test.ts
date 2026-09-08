import { describe, expect, it } from "vitest";
import { buildInterpretationContext, readStreamContent, resolveProxy, INTERPRET_SYSTEM_PROMPT, extractStandpoints } from "../llm";

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
