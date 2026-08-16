import { getModel, listModels } from "./db";
import type { ModelProfile } from "./types";
import { normalizeSymbol } from "./symbols";
import { fetch as undiciFetch, ProxyAgent } from "undici";

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: unknown;
}

export function resolveApiKey(profile: ModelProfile): string {
  const fromEnv = profile.env_key ? process.env[profile.env_key] : "";
  return fromEnv || profile.api_key;
}

export function enabledModels(): ModelProfile[] {
  return listModels().filter((model) => model.enabled && resolveApiKey(model));
}

export function pickVisionModel(): ModelProfile | null {
  return enabledModels().find((model) => model.vision_supported) ?? null;
}

function stripJsonFences(text: string): string {
  return text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
}

async function chatCompletion(
  profile: ModelProfile,
  messages: ChatMessage[],
  timeoutMs = 60_000
): Promise<string> {
  const apiKey = resolveApiKey(profile);
  if (!apiKey) throw new Error("模型未配置 API Key");
  const url = `${profile.base_url.replace(/\/$/, "")}/chat/completions`;
  const body = {
    model: profile.model,
    messages,
    temperature: 0.2,
  };
  const doFetch = async () => {
    const response = await undiciFetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
      ...(dispatcher ? { dispatcher } : {}),
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      throw new Error(`HTTP ${response.status}: ${detail.slice(0, 300)}`);
    }
    const data = (await response.json()) as {
      choices?: Array<{ message?: { content?: string } }>;
      error?: { message?: string };
    };
    if (data.error?.message) throw new Error(data.error.message);
    const content = data.choices?.[0]?.message?.content;
    if (!content) throw new Error("模型返回内容为空");
    return content;
  };
  const dispatcher = profile.proxy?.trim() ? new ProxyAgent(profile.proxy.trim()) : undefined;
  try {
    return await doFetch();
  } catch (error) {
    // one retry for transient network/timeout failures
    if (error instanceof Error && /timeout|ECONNRESET|fetch failed|ETIMEDOUT|ENOTFOUND/i.test(error.message)) {
      return doFetch();
    }
    throw error;
  } finally {
    if (dispatcher) dispatcher.close().catch(() => undefined);
  }
}

export async function testProfile(profile: ModelProfile): Promise<{ ok: boolean; detail: string }> {
  try {
    const content = await chatCompletion(profile, [
      { role: "user", content: "请回复 OK" },
    ], 30_000);
    return { ok: true, detail: content.slice(0, 200) };
  } catch (error) {
    return { ok: false, detail: error instanceof Error ? error.message : String(error) };
  }
}

export async function testModelById(id: number): Promise<{ ok: boolean; detail: string }> {
  const profile = getModel(id);
  if (!profile) return { ok: false, detail: "模型不存在" };
  return testProfile(profile);
}

export async function recognizeSymbols(profile: ModelProfile, dataUrl: string): Promise<Array<{ symbol: string; name: string }>> {
  const content = await chatCompletion(
    profile,
    [
      {
        role: "system",
        content:
          "你是A股股票代码识别助手。只输出 JSON，不要输出任何其他文字、解释或 Markdown。",
      },
      {
        role: "user",
        content: [
          {
            type: "text",
            text: "识别图片中的 A 股股票代码列表。返回 JSON 格式：{\"symbols\":[{\"symbol\":\"600036\",\"name\":\"招商银行\"}]}。symbol 只写 6 位数字；name 写股票名称，看不清就留空字符串。",
          },
          { type: "image_url", image_url: { url: dataUrl } },
        ],
      },
    ],
    90_000
  );
  const parsed = JSON.parse(stripJsonFences(content)) as {
    symbols?: Array<{ symbol?: string; name?: string; code?: string }>;
  };
  const items = Array.isArray(parsed.symbols) ? parsed.symbols : [];
  return items
    .map((item) => {
      const raw = String(item.symbol ?? item.code ?? "").trim();
      const digits = raw.match(/\d{6}/)?.[0] ?? "";
      return digits ? { symbol: normalizeSymbol(digits), name: String(item.name ?? "").trim() } : null;
    })
    .filter((item): item is { symbol: string; name: string } => item !== null);
}

export async function interpretReport(profile: ModelProfile, reportText: string): Promise<string> {
  const truncated = reportText.length > 12_000 ? `${reportText.slice(0, 12_000)}...（已截断）` : reportText;
  return chatCompletion(
    profile,
    [
      {
        role: "system",
        content:
          "你是资深 A 股量化分析助手。基于给定的信号监控报告，输出简洁中文解读：主要信号、风险点、建议动作（观察/买入候选/规避）。300 字以内，使用 Markdown。",
      },
      { role: "user", content: truncated },
    ],
    90_000
  );
}
