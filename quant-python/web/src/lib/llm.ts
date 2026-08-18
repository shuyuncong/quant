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

/** 任选一个已启用且有 API Key 的模型用于文本类任务（AI 解读等）。 */
export function pickChatModel(): ModelProfile | null {
  return enabledModels()[0] ?? null;
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

type JsonObject = Record<string, unknown>;

function asObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

function compactTimeframe(value: unknown, barsPerTimeframe: number): JsonObject {
  const report = asObject(value);
  const recentBars = Array.isArray(report.recent_bars) ? report.recent_bars : [];
  return {
    status: report.status,
    latest_time: report.latest_time,
    latest_price: report.latest_price,
    buy_score: report.buy_score,
    sell_score: report.sell_score,
    indicators: report.indicators,
    chan: report.chan,
    events: report.events,
    recent_bars: barsPerTimeframe > 0 ? recentBars.slice(-barsPerTimeframe) : [],
    error: report.error,
  };
}

function compactAnalysisResult(value: unknown, barsPerTimeframe: number): JsonObject {
  const result = asObject(value);
  const timeframes = asObject(result.timeframes);
  return {
    symbol: result.symbol,
    name: result.name,
    status: result.status,
    error: result.error,
    analyzed_at: result.analyzed_at,
    events: result.events,
    timeframes: Object.fromEntries(
      Object.entries(timeframes).map(([key, report]) => [key, compactTimeframe(report, barsPerTimeframe)])
    ),
  };
}

function compactReport(report: JsonObject, barsPerTimeframe: number, candidateLimit: number): JsonObject {
  const results = Array.isArray(report.results)
    ? report.results.map((item) => compactAnalysisResult(item, barsPerTimeframe))
    : undefined;
  const candidates = Array.isArray(report.candidates)
    ? [...report.candidates]
        .sort((left, right) => Number(asObject(right).score ?? 0) - Number(asObject(left).score ?? 0))
        .slice(0, candidateLimit)
    : undefined;
  return {
    mode: report.mode,
    analyzed_at: report.analyzed_at,
    scanned_at: report.scanned_at,
    universe_mode: report.universe_mode,
    universe_size: report.universe_size,
    batch_start: report.batch_start,
    batch_size: report.batch_size,
    coverage: report.coverage,
    completed_round: report.completed_round,
    new_events: report.new_events,
    candidate_count: report.candidate_count,
    delivery: report.delivery,
    output_file: report.output_file,
    results,
    candidates,
    errors: Array.isArray(report.errors) ? report.errors.slice(0, 50) : report.errors,
  };
}

export function buildInterpretationContext(reportText: string, maxChars = 60_000): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(reportText);
  } catch {
    return reportText.length <= maxChars ? reportText : reportText.slice(-maxChars);
  }
  const report = asObject(parsed);
  for (const [bars, candidates] of [
    [24, 100],
    [8, 50],
    [0, 30],
  ] as const) {
    const text = JSON.stringify(compactReport(report, bars, candidates), null, 2);
    if (text.length <= maxChars || bars === 0) return text;
  }
  return JSON.stringify(compactReport(report, 0, 30));
}

export async function interpretReport(
  profile: ModelProfile,
  reportText: string,
  supplementalContext?: string | null,
): Promise<string> {
  // Compact the structured report first.  Holdings and other supplemental
  // context are appended after compaction so they cannot invalidate JSON
  // parsing or cause the report to be truncated from its beginning.
  const supplemental = supplementalContext?.trim() ?? "";
  const reportBudget = Math.max(1_000, 60_000 - supplemental.length - 256);
  const compacted = buildInterpretationContext(reportText, reportBudget);
  const context = supplemental
    ? `${compacted}\n\n补充上下文：\n${supplemental}`
    : compacted;
  return chatCompletion(
    profile,
    [
      {
        role: "system",
        content:
          "你是资深 A 股量化分析助手。MACD 金叉定义为 DIF 上穿 DEA，并按 0轴上方、0轴附近、0轴下方排序；结合温和放量、突破 MA5/MA10、红柱连续放大确认。基于报告逐股输出主要信号、缠论买卖点、多周期一致性、风险和建议动作（观察/买入候选/减仓候选/规避）。不要承诺胜率，使用简洁 Markdown。",
      },
      { role: "user", content: context },
    ],
    90_000
  );
}
