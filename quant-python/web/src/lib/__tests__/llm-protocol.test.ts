import { beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("undici", async (original) => ({ ...await original<typeof import("undici")>(), fetch: vi.fn() }));
vi.mock("../db", () => ({ listModels: vi.fn(), getModel: vi.fn(), getSetting: vi.fn() }));
import { fetch as fetchApi, type Response as ApiResponse } from "undici";
import { listModels } from "../db";
import { chatWithFallback, readStreamContent, recognizeSymbols, testProfile } from "../llm";
import { modelEndpoint } from "../model-protocol";
import { responsesRequest } from "../llm-protocol";
import type { ModelProfile } from "../types";

const profile: ModelProfile = { id: 1, name: "astra", model: "gpt-6-astra", protocol: "responses",
  base_url: "https://models.test/v1", api_key: "secret-test-key", env_key: "", proxy: "",
  enabled: true, vision_supported: true, priority: 0, created_at: "", updated_at: "" };
const apiResponse = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } }) as unknown as ApiResponse;
const output = (text: string) => ({ status: "completed", output: [
  { type: "reasoning", summary: [] },
  { type: "message", role: "assistant", content: [{ type: "output_text", text }] },
] });
const stream = (...events: unknown[]) => new Response(events.map(e => `data: ${JSON.stringify(e)}`).join("\r\n\r\n")) as unknown as ApiResponse;
beforeEach(() => { vi.clearAllMocks(); vi.mocked(listModels).mockResolvedValue([profile]); });

describe("model protocols", () => {
  it.each([
    ["https://models.test/v1/", "responses", "https://models.test/v1/responses"],
    ["https://models.test/v1/chat/completions", "responses", "https://models.test/v1/responses"],
    ["https://models.test/v1/responses/", "chat_completions", "https://models.test/v1/chat/completions"],
  ] as const)("normalizes endpoint %s", (base, protocol, expected) => expect(modelEndpoint(base, protocol)).toBe(expected));

  it("tests Responses with input and reads output message blocks", async () => {
    vi.mocked(fetchApi).mockResolvedValue(apiResponse(output("OK")));
    expect(await testProfile(profile)).toEqual({ ok: true, detail: "OK" });
    const [url, init] = vi.mocked(fetchApi).mock.calls[0];
    expect(url).toBe("https://models.test/v1/responses");
    expect(JSON.parse(String(init?.body))).toEqual({ model: "gpt-6-astra", instructions: "", input: [{ role: "user", content: [{ type: "input_text", text: "请回复 OK" }] }], store: false, stream: true });
  });

  it("preserves old profiles and omits unsupported sampling parameters", async () => {
    vi.mocked(fetchApi).mockResolvedValue(apiResponse({ choices: [{ message: { content: "OK" } }] }));
    expect((await testProfile({ ...profile, protocol: undefined })).ok).toBe(true);
    expect(vi.mocked(fetchApi).mock.calls[0][0]).toBe("https://models.test/v1/chat/completions");
    const body = JSON.parse(String(vi.mocked(fetchApi).mock.calls[0][1]?.body));
    expect(body.messages).toBeDefined();
    expect(body).not.toHaveProperty("temperature");
    expect(body).not.toHaveProperty("input");
  });

  it("converts image recognition to input_text and input_image", async () => {
    vi.mocked(fetchApi).mockResolvedValue(apiResponse(output('{"symbols":[{"symbol":"600036","name":"招商银行"}]}')));
    expect(await recognizeSymbols(profile, "data:image/png;base64,abc")).toEqual([{ symbol: "600036.SH", name: "招商银行" }]);
    const body = JSON.parse(String(vi.mocked(fetchApi).mock.calls[0][1]?.body));
    expect(body.instructions).toContain("股票代码识别助手");
    expect(body.input[0].content).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: "input_text" }),
      expect.objectContaining({ type: "input_image", image_url: "data:image/png;base64,abc" }),
    ]));
  });

  it("matches the working reasoning configuration and excludes developer messages", async () => {
    const body = responsesRequest([
      { role: "system", content: "system rules" },
      { role: "developer", content: "application rules" },
      { role: "user", content: "hello" },
      { role: "assistant", content: "previous answer" },
    ], "xhigh");
    expect(body).toMatchObject({ instructions: "system rules\n\napplication rules", store: false, stream: true, reasoning: { effort: "xhigh" } });
    expect(body.input.map(message => message.role)).toEqual(["user", "assistant"]);
    expect(body.input[1].content).toEqual([{ type: "output_text", text: "previous answer" }]);
    vi.mocked(fetchApi).mockResolvedValue(apiResponse(output("OK")));
    await testProfile({ ...profile, reasoning_effort: "xhigh" });
    expect(JSON.parse(String(vi.mocked(fetchApi).mock.calls[0][1]?.body)).reasoning).toEqual({ effort: "xhigh" });
  });

  it("reads a Responses stream including its final unterminated frame", async () => {
    vi.mocked(fetchApi).mockResolvedValue(stream(
      { type: "response.output_text.delta", delta: "你" },
      { type: "response.output_text.delta", delta: "好" },
      { type: "response.completed", response: output("你好") },
    ));
    expect((await chatWithFallback([{ role: "user", content: "hi" }], { stream: true })).content).toBe("你好");
    expect(JSON.parse(String(vi.mocked(fetchApi).mock.calls[0][1]?.body)).stream).toBe(true);
  });

  it("rejects a truncated stream instead of accepting a partial report", async () => {
    await expect(readStreamContent(stream({ type: "response.output_text.delta", delta: "partial" }), "responses"))
      .rejects.toThrow("未收到 response.completed");
  });

  it("falls back across different protocols after a streamed failure", async () => {
    vi.mocked(listModels).mockResolvedValue([profile, { ...profile, id: 2, protocol: "chat_completions" }]);
    vi.mocked(fetchApi).mockResolvedValueOnce(stream(
      { type: "response.output_text.delta", delta: "partial" },
      { type: "response.failed", response: { status: "failed", error: { message: "upstream failed" } } },
    )).mockResolvedValueOnce(stream({ choices: [{ delta: { content: "fallback" } }] }));
    const result = await chatWithFallback([{ role: "user", content: "hi" }], { stream: true });
    expect(result.content).toBe("fallback");
    expect(result.model.id).toBe(2);
    expect(vi.mocked(fetchApi).mock.calls[1][0]).toBe("https://models.test/v1/chat/completions");
  });

  it("rejects incomplete non-stream output", async () => {
    vi.mocked(fetchApi).mockResolvedValue(apiResponse({ ...output("partial"), status: "incomplete", incomplete_details: { reason: "max_output_tokens" } }));
    expect(await testProfile(profile)).toEqual({ ok: false, detail: expect.stringContaining("max_output_tokens") });
  });

  it("reports protocol and HTTP status for endpoint mismatches", async () => {
    vi.mocked(fetchApi).mockResolvedValue(new Response("Not Found", { status: 404 }) as unknown as ApiResponse);
    expect(await testProfile(profile)).toEqual({ ok: false, detail: expect.stringMatching(/Responses HTTP 404.*接口协议/) });
  });

  it("negotiates Codex compatibility after the gateway rejects standard Responses", async () => {
    vi.mocked(fetchApi).mockResolvedValueOnce(new Response(
      JSON.stringify({ error: { message: "invalid codex request", code: "invalid_responses_request" } }), { status: 400 },
    ) as unknown as ApiResponse).mockResolvedValueOnce(stream(
      { type: "response.output_text.delta", delta: "OK" },
      { type: "response.completed", response: output("OK") },
    ));
    expect(await testProfile({ ...profile, reasoning_effort: "xhigh" })).toEqual({ ok: true, detail: "OK" });
    expect(fetchApi).toHaveBeenCalledTimes(2);
    const first = JSON.parse(String(vi.mocked(fetchApi).mock.calls[0][1]?.body));
    const retry = JSON.parse(String(vi.mocked(fetchApi).mock.calls[1][1]?.body));
    expect(first).not.toHaveProperty("prompt_cache_key");
    expect(retry).toMatchObject({ instructions: "", reasoning: { effort: "xhigh" }, text: { verbosity: "low" },
      include: ["reasoning.encrypted_content"], input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "请回复 OK" }] }] });
    expect(retry.prompt_cache_key).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("bounds compatibility retries and does not loop on invalid requests", async () => {
    vi.mocked(fetchApi).mockImplementation(async () => new Response("invalid codex request", { status: 400 }) as unknown as ApiResponse);
    expect((await testProfile(profile)).ok).toBe(false);
    expect(fetchApi).toHaveBeenCalledTimes(2);
  });

  it("preserves the adapted request on one transient upstream retry", async () => {
    vi.mocked(fetchApi).mockResolvedValueOnce(new Response("invalid codex request", { status: 400 }) as unknown as ApiResponse)
      .mockResolvedValueOnce(new Response('{"error":{"code":"do_request_failed"}}', { status: 500 }) as unknown as ApiResponse)
      .mockResolvedValueOnce(apiResponse(output("OK")));
    expect((await testProfile(profile)).ok).toBe(true);
    expect(fetchApi).toHaveBeenCalledTimes(3);
    expect(vi.mocked(fetchApi).mock.calls[1][1]?.body).toBe(vi.mocked(fetchApi).mock.calls[2][1]?.body);
  });
});
