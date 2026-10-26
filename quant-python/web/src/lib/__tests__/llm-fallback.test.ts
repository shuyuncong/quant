import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("undici", async (importOriginal) => {
  const mod = (await importOriginal()) as typeof import("undici");
  return { ...mod, fetch: vi.fn() };
});

vi.mock("../db", () => ({
  listModels: vi.fn(),
  getModel: vi.fn(),
}));

import { fetch as undiciFetch } from "undici";
import type { Response as UndiciResponse } from "undici";
import { listModels } from "../db";
import type { ModelProfile } from "../types";
import {
  chatWithFallback,
  interpretReportWithFallback,
  recognizeSymbolsWithFallback,
} from "../llm";

const fetchMock = vi.mocked(undiciFetch);
const listModelsMock = vi.mocked(listModels);

const model = (overrides: Partial<ModelProfile> = {}): ModelProfile => ({
  id: 1,
  name: "test-model",
  base_url: "https://x.test/v1",
  model: "gpt-4o-mini",
  api_key: "test-key",
  env_key: "",
  proxy: "",
  enabled: true,
  vision_supported: true,
  priority: 0,
  created_at: "",
  updated_at: "",
  ...overrides,
});

const okResponse = (content: string): UndiciResponse =>
  new Response(
    JSON.stringify({ choices: [{ message: { content } }] }),
    { status: 200, headers: { "Content-Type": "application/json" } }
  ) as unknown as UndiciResponse;

// HTTP 500 不触发 chatCompletion 内部的一次性网络重试，直接抛错 → 交给降级链。
const failResponse = (): UndiciResponse =>
  new Response("boom", { status: 500, statusText: "boom" }) as unknown as UndiciResponse;

const timeoutError = (): Error =>
  Object.assign(new Error("The operation was aborted due to timeout"), { name: "TimeoutError" });

const connectionError = (): Error =>
  Object.assign(new Error("fetch failed"), { cause: { code: "ECONNRESET" } });

// 所有 describe 共享：避免前序测试的 fetch 调用与模型列表污染后续断言。
beforeEach(() => {
  fetchMock.mockReset();
  listModelsMock.mockReset();
  listModelsMock.mockResolvedValue([]);
});

describe("chatWithFallback 模型降级链", () => {
  it("第一个模型成功时只调用它", async () => {
    listModelsMock.mockResolvedValue([
      model({ id: 1, name: "first" }),
      model({ id: 2, name: "second" }),
    ]);
    fetchMock.mockResolvedValue(okResponse("hello"));

    const { content, model: used } = await chatWithFallback([{ role: "user", content: "hi" }]);

    expect(content).toBe("hello");
    expect(used.name).toBe("first");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("x.test");
  });

  it("第一个失败时自动切换第二个", async () => {
    listModelsMock.mockResolvedValue([
      model({ id: 1, name: "first" }),
      model({ id: 2, name: "second" }),
    ]);
    fetchMock
      .mockResolvedValueOnce(failResponse())
      .mockResolvedValueOnce(okResponse("fallback ok"));

    const { content, model: used } = await chatWithFallback([{ role: "user", content: "hi" }]);

    expect(content).toBe("fallback ok");
    expect(used.name).toBe("second");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("全部失败时抛出最后一个错误", async () => {
    listModelsMock.mockResolvedValue([
      model({ id: 1, name: "first" }),
      model({ id: 2, name: "second" }),
    ]);
    fetchMock.mockResolvedValue(failResponse());

    await expect(chatWithFallback([{ role: "user", content: "hi" }])).rejects.toThrow(/HTTP 500/);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("超时不重试同一模型，直接切换到下一个", async () => {
    listModelsMock.mockResolvedValue([
      model({ id: 1, name: "slow" }),
      model({ id: 2, name: "fast", base_url: "https://fast.test/v1" }),
    ]);
    fetchMock.mockRejectedValueOnce(timeoutError()).mockResolvedValueOnce(okResponse("fallback"));

    const { content, model: used } = await chatWithFallback([{ role: "user", content: "hi" }]);

    expect(content).toBe("fallback");
    expect(used.name).toBe("fast");
    // 慢模型只调用一次：不再像旧逻辑那样先重试一次再降级。
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("连接类错误仍然重试一次", async () => {
    listModelsMock.mockResolvedValue([model({ id: 1, name: "flaky" })]);
    fetchMock.mockRejectedValueOnce(connectionError()).mockResolvedValueOnce(okResponse("retried"));

    const { content } = await chatWithFallback([{ role: "user", content: "hi" }]);

    expect(content).toBe("retried");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("批次熔断后跳过已超时模型", async () => {
    listModelsMock.mockResolvedValue([
      model({ id: 1, name: "slow", base_url: "https://slow.test/v1" }),
      model({ id: 2, name: "fast", base_url: "https://fast.test/v1" }),
    ]);
    fetchMock
      .mockRejectedValueOnce(timeoutError())
      .mockResolvedValueOnce(okResponse("fast"))
      .mockResolvedValueOnce(okResponse("fast"));
    const timedOutModelIds = new Set<number>();

    const first = await chatWithFallback([{ role: "user", content: "hi" }], { timedOutModelIds });
    const second = await chatWithFallback([{ role: "user", content: "hi" }], { timedOutModelIds });

    expect(first.model.name).toBe("fast");
    expect(second.model.name).toBe("fast");
    expect(timedOutModelIds.has(1)).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    // 第二次调用没有再次访问慢模型。
    expect(String(fetchMock.mock.calls[2][0])).toContain("fast.test");
  });

  it("所有可用模型都已熔断时快速失败", async () => {
    listModelsMock.mockResolvedValue([
      model({ id: 1, name: "first" }),
      model({ id: 2, name: "second" }),
    ]);
    const timedOutModelIds = new Set([1, 2]);

    await expect(
      chatWithFallback([{ role: "user", content: "hi" }], { timedOutModelIds }),
    ).rejects.toThrow(/本批次所有可用模型均已超时/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("visionOnly 时跳过不支持视觉的模型", async () => {
    listModelsMock.mockResolvedValue([
      model({ id: 1, name: "text-only", vision_supported: false }),
      model({ id: 2, name: "vision", vision_supported: true }),
    ]);
    fetchMock.mockResolvedValue(okResponse("ok"));

    const { model: used } = await chatWithFallback([{ role: "user", content: "img?" }], {
      visionOnly: true,
    });

    expect(used.name).toBe("vision");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("无已启用模型时抛出友好错误", async () => {
    listModelsMock.mockResolvedValue([model({ id: 1, enabled: false })]);

    await expect(chatWithFallback([{ role: "user", content: "hi" }])).rejects.toThrow(
      "未配置可用的模型"
    );
  });
});

describe("interpretReportWithFallback", () => {
  it("构建解读消息并返回内容与实际模型", async () => {
    listModelsMock.mockResolvedValue([model({ id: 7, name: "interpreter" })]);
    const sseBody = [
      'data: {"choices":[{"delta":{"content":"持有 600036，"}}]}',
      'data: {"choices":[{"delta":{"content":"建议加仓"}}]}',
      "data: [DONE]",
      "",
    ].join("\n");
    fetchMock.mockResolvedValue(
      new Response(sseBody, { status: 200 }) as unknown as UndiciResponse
    );

    const { content, model: used } = await interpretReportWithFallback(
      JSON.stringify({ candidates: [] }),
      "【我的持仓】600036 成本 30.0"
    );

    expect(content).toBe("持有 600036，建议加仓");
    expect(used.name).toBe("interpreter");
  });
});

describe("recognizeSymbolsWithFallback", () => {
  it("解析图片识别结果并返回实际模型", async () => {
    listModelsMock.mockResolvedValue([
      model({ id: 1, name: "bad-vision" }),
      model({ id: 2, name: "good-vision" }),
    ]);
    fetchMock
      .mockResolvedValueOnce(failResponse())
      .mockResolvedValueOnce(
        okResponse(JSON.stringify({ symbols: [{ symbol: "600036", name: "招商银行" }, { code: "000001" }] }))
      );

    const { candidates, model: used } = await recognizeSymbolsWithFallback("data:image/png;base64,xxx");

    expect(used.name).toBe("good-vision");
    // normalizeSymbol 会补交易所后缀：6 开头 → .SH，0 开头 → .SZ
    expect(candidates).toEqual([
      { symbol: "600036.SH", name: "招商银行" },
      { symbol: "000001.SZ", name: "" },
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});