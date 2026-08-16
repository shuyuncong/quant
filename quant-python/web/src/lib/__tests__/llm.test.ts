import { afterEach, describe, expect, it, vi } from "vitest";
import { interpretReport, recognizeSymbols, resolveApiKey, testProfile } from "../llm";
import type { ModelProfile } from "../types";

const profile: ModelProfile = {
  id: 1,
  name: "test",
  base_url: "https://api.example.com/v1",
  model: "gpt-test",
  api_key: "sk-test",
  env_key: "",
  proxy: "",
  enabled: true,
  vision_supported: true,
  created_at: "",
  updated_at: "",
};

function jsonResponse(payload: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("resolveApiKey", () => {
  it("prefers env key over stored key", () => {
    process.env.MODEL_TEST_KEY = "env-key";
    expect(resolveApiKey({ ...profile, env_key: "MODEL_TEST_KEY", api_key: "db-key" })).toBe("env-key");
    delete process.env.MODEL_TEST_KEY;
  });
  it("falls back to stored key", () => {
    expect(resolveApiKey(profile)).toBe("sk-test");
  });
});

describe("recognizeSymbols", () => {
  it("parses fenced JSON and normalizes symbols", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        choices: [
          {
            message: {
              content: '\u0060\u0060\u0060json\n{"symbols":[{"symbol":"600036","name":"招商银行"},{"symbol":"000001","name":"平安银行"}]}\n\u0060\u0060\u0060',
            },
          },
        ],
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    const items = await recognizeSymbols(profile, "data:image/png;base64,AAAA");
    expect(items).toEqual([
      { symbol: "600036.SH", name: "招商银行" },
      { symbol: "000001.SZ", name: "平安银行" },
    ]);
  });
});

describe("interpretReport", () => {
  it("retries once on transient failure", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("fetch failed"))
      .mockResolvedValueOnce(
        jsonResponse({ choices: [{ message: { content: "信号偏强，注意风险。" } }] })
      );
    vi.stubGlobal("fetch", fetchMock);
    const content = await interpretReport(profile, "report text");
    expect(content).toContain("风险");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("testProfile", () => {
  it("uses proxy dispatcher when proxy is configured", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ choices: [{ message: { content: "OK" } }] })
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = await testProfile({ ...profile, proxy: "http://127.0.0.1:7890" });
    expect(result.ok).toBe(true);
    const init = fetchMock.mock.calls[0][1] as RequestInit | undefined;
    expect((init as { dispatcher?: unknown })?.dispatcher).toBeDefined();
  });

  it("returns failure detail on HTTP error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ error: { message: "invalid key" } }, 401))
    );
    const result = await testProfile(profile);
    expect(result.ok).toBe(false);
    expect(result.detail).toContain("401");
  });
});
