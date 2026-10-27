import { getModel, listModels, getSetting } from "./db";
import type { ModelProfile } from "./types";
import { normalizeSymbol } from "./symbols";
import { fetch as undiciFetch, ProxyAgent } from "undici";
import type { Response } from "undici";
import { modelEndpoint, MODEL_PROTOCOL_LABELS, type ModelProtocol } from "./model-protocol";
import { responsesRequest, responseText, protocolStreamText, codexCompatibleRequest } from "./llm-protocol";
import { randomUUID } from "node:crypto";

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: unknown;
}

export function resolveApiKey(profile: ModelProfile): string {
  const fromEnv = profile.env_key ? process.env[profile.env_key] : "";
  return fromEnv || profile.api_key;
}

/**
 * 模型代理解析，与 resolveApiKey 相同的"环境变量优先"约定：
 * MODEL_PROXY → HTTPS_PROXY → HTTP_PROXY → DB proxy。
 * DB 只存中性值，本地/线上各自通过环境变量指定出口，避免共享库互相踩配置。
 * 用 MODEL_PROXY 而非直接依赖 HTTPS_PROXY：该变量不会被子进程（Python 数据桥）继承。
 */
export function resolveProxy(profile: ModelProfile): string {
  return (
    process.env.MODEL_PROXY?.trim() ||
    process.env.HTTPS_PROXY?.trim() ||
    process.env.HTTP_PROXY?.trim() ||
    profile.proxy.trim()
  );
}

export async function enabledModels(visionOnly = false): Promise<ModelProfile[]> {
  const all = (await listModels()).filter((model) => model.enabled && resolveApiKey(model));
  return visionOnly ? all.filter((model) => model.vision_supported) : all;
}

export async function pickVisionModel(): Promise<ModelProfile | null> {
  return (await enabledModels(true))[0] ?? null;
}

/** 任选一个已启用且有 API Key 的模型用于文本类任务（AI 解读等）。 */
export async function pickChatModel(): Promise<ModelProfile | null> {
  return (await enabledModels())[0] ?? null;
}

export interface ChatWithFallbackResult {
  content: string;
  model: ModelProfile;
}

export interface ChatWithFallbackOptions {
  timeoutMs?: number;
  stream?: boolean;
  visionOnly?: boolean;
  purpose?: "technical" | "synthesis";
  /**
   * 本次批次内已经超时的模型 id。
   *
   * 调用方按批次创建并复用（个股分析一次任务 = 一个批次）：
   *   - 进入循环前跳过集合里的模型；
   *   - 某个模型超时后写入集合，供同批次后续阶段直接跳过。
   * 这样第一个阶段支付一次超时后，后续 7 个阶段不再重复等待同一个慢模型。
   */
  timedOutModelIds?: Set<number>;
}

type ErrorWithCause = Error & { code?: string; cause?: { code?: string; message?: string } };

/** 超时类错误：本地 AbortSignal.timeout、连接/读写超时、上游 HTTP 524。 */
function isTimeoutError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  if (error.name === "TimeoutError" || error.name === "AbortError") return true;
  const typed = error as ErrorWithCause;
  const code = String(typed.cause?.code ?? typed.code ?? "");
  if (/TIMEOUT|TIMEDOUT/i.test(code)) return true;
  return /timeout|timed out|timedout|HTTP 524/i.test(`${error.message} ${typed.cause?.message ?? ""}`);
}

/** 只对连接类错误重试；超时和 HTTP 4xx/5xx 直接交给降级链。 */
function isRetryableConnectionError(error: unknown): boolean {
  if (!(error instanceof Error) || isTimeoutError(error)) return false;
  const typed = error as ErrorWithCause;
  const code = String(typed.cause?.code ?? typed.code ?? "");
  if (/^(ECONNRESET|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|EPIPE|UND_ERR_SOCKET)$/.test(code)) return true;
  return /ECONNRESET|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|EPIPE|socket hang up|UND_ERR_SOCKET/i.test(
    `${error.message} ${typed.cause?.message ?? ""}`,
  );
}

/**
 * 按优先级（priority 升序）顺序尝试已启用模型：排序靠前的调用失败
 * （网络/HTTP/超时/内容错误）自动切换下一个，全部失败抛出最后一次错误。
 * visionOnly 时只尝试支持视觉的模型（图片识别链）。
 * 传入 timedOutModelIds 时启用批次级熔断，超时模型不会在同一批次内再次尝试。
 */
export async function chatWithFallback(
  messages: ChatMessage[],
  options: ChatWithFallbackOptions = {},
): Promise<ChatWithFallbackResult> {
  const candidates = await enabledModels(options.visionOnly === true);
  if (options.purpose) {
    const preferred = Number(await getSetting(`models.purpose.${options.purpose}`));
    candidates.sort((a, b) => Number(b.id === preferred) - Number(a.id === preferred));
  }
  if (candidates.length === 0) {
    throw new Error(
      options.visionOnly
        ? "未配置可用的视觉模型（需启用并配置 API Key）"
        : "未配置可用的模型（需启用并配置 API Key）",
    );
  }
  const timedOut = options.timedOutModelIds;
  const available = candidates.filter((profile) => !timedOut?.has(profile.id));
  if (available.length === 0) {
    const names = candidates.map((profile) => profile.name).join("、");
    throw new Error(`本批次所有可用模型均已超时（${names}），跳过后续阶段`);
  }
  let lastError: unknown;
  for (const profile of available) {
    try {
      const content = await chatCompletion(
        profile,
        messages,
        options.timeoutMs,
        options.stream === true,
      );
      return { content, model: profile };
    } catch (error) {
      lastError = error;
      if (isTimeoutError(error)) {
        timedOut?.add(profile.id);
        console.warn(`[llm] 模型 ${profile.name} 超时，本批次后续阶段跳过`);
      }
    }
  }
  throw lastError instanceof Error ? lastError : new Error("所有可用模型调用均失败");
}

/**
 * AI 解读的超时。生成大报告解读时模型响应常超过 60s，
 * 用 MODEL_TIMEOUT_SECONDS 环境变量可调（默认 180，下限 30）。
 */
function interpretTimeoutMs(): number {
  const seconds = Number(process.env.MODEL_TIMEOUT_SECONDS);
  const valid = Number.isFinite(seconds) && seconds >= 30;
  return (valid ? seconds : 180) * 1_000;
}

function stripJsonFences(text: string): string {
  return text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
}

async function chatCompletion(
  profile: ModelProfile,
  messages: ChatMessage[],
  timeoutMs = 60_000,
  stream = false,
): Promise<string> {
  const apiKey = resolveApiKey(profile);
  if (!apiKey) throw new Error("模型未配置 API Key");
  const protocol = profile.protocol ?? "chat_completions";
  const requestStream = stream || protocol === "responses";
  const url = modelEndpoint(profile.base_url, protocol);
  let body: Record<string, unknown> = {
    model: profile.model,
    ...(protocol === "responses" ? responsesRequest(messages, profile.reasoning_effort) : { messages }),
    ...(requestStream ? { stream: true } : {}),
  };
  let codexCompatibilityApplied = false;
  const doFetch = async (): Promise<string> => {
    const response = await undiciFetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey}`,
        Accept: requestStream ? "text/event-stream" : "application/json",
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
      ...(dispatcher ? { dispatcher } : {}),
    });
    if (!response.ok) {
      const detail = (await response.text().catch(() => "")).split(apiKey).join("[redacted]");
      if (protocol === "responses" && !codexCompatibilityApplied && response.status === 400 &&
          /invalid codex request|invalid_responses_request/i.test(detail)) {
        codexCompatibilityApplied = true;
        body = codexCompatibleRequest(body, randomUUID());
        return await doFetch();
      }
      const hint = response.status === 404 || response.status === 405
        ? "；请核对 Base URL 和接口协议" : "";
      throw new Error(`${MODEL_PROTOCOL_LABELS[protocol]} HTTP ${response.status}: ${detail.slice(0, 300)}${hint}`);
    }
    const contentType = response.headers.get("content-type") ?? "";
    if (contentType.includes("text/event-stream") || (requestStream && !contentType.includes("application/json"))) {
      return await readStreamContent(response, protocol);
    }
    const content = responseText(await response.json(), protocol);
    if (!content) throw new Error("模型返回内容为空");
    return content;
  };
  const proxy = resolveProxy(profile);
  const dispatcher = proxy ? new ProxyAgent(proxy) : undefined;
  try {
    return await doFetch();
  } catch (error) {
    // 超时不再重试同一个模型：再发一次只会把同一个 180s 上限再付一遍。
    // 只有连接类错误最多重试一次；HTTP 4xx/5xx 也立即交给降级链。
    if (isRetryableConnectionError(error)) {
      return await doFetch();
    }
    throw error;
  } finally {
    if (dispatcher) dispatcher.close().catch(() => undefined);
  }
}

/**
 * OpenAI 兼容 SSE 流式返回解析。
 * 解读类请求用流式：首个 token 到达后连接保持活跃，
 * 可避开上游 Cloudflare 的源站响应超时（HTTP 524）即使完整生成耗时数分钟。
 */
export async function readStreamContent(response: Response, protocol: ModelProtocol = "chat_completions"): Promise<string> {
  return protocolStreamText(response, protocol);
}

export async function testProfile(profile: ModelProfile): Promise<{ ok: boolean; detail: string }> {
  try {
    const content = await chatCompletion(profile, [
      { role: "user", content: "请回复 OK" },
    ], 60_000);
    return { ok: true, detail: content.slice(0, 200) };
  } catch (error) {
    return { ok: false, detail: error instanceof Error ? error.message : String(error) };
  }
}

export async function testModelById(id: number): Promise<{ ok: boolean; detail: string }> {
  const profile = await getModel(id);
  if (!profile) return { ok: false, detail: "模型不存在" };
  return testProfile(profile);
}

const SYMBOL_RECOGNITION_PROMPT =
  "你是A股股票代码识别助手。只输出 JSON，不要输出任何其他文字、解释或 Markdown。";

function buildSymbolRecognitionMessages(dataUrl: string): ChatMessage[] {
  return [
    {
      role: "system",
      content: SYMBOL_RECOGNITION_PROMPT,
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
  ];
}

function parseSymbolRecognition(content: string): Array<{ symbol: string; name: string }> {
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

export async function recognizeSymbols(profile: ModelProfile, dataUrl: string): Promise<Array<{ symbol: string; name: string }>> {
  const content = await chatCompletion(profile, buildSymbolRecognitionMessages(dataUrl), 90_000);
  return parseSymbolRecognition(content);
}

/** 图片识别降级链：按优先级尝试视觉模型，失败自动切换下一个。返回实际使用的模型。 */
export async function recognizeSymbolsWithFallback(
  dataUrl: string,
): Promise<{ candidates: Array<{ symbol: string; name: string }>; model: ModelProfile }> {
  const { content, model } = await chatWithFallback(buildSymbolRecognitionMessages(dataUrl), {
    timeoutMs: 90_000,
    visionOnly: true,
  });
  return { candidates: parseSymbolRecognition(content), model };
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
    // 分批减仓的执行数字（档位触发价/建议股数/保本止损位）只存在于策略结果里；
    // 解读压缩时若丢掉 strategies，模型就只能凭感觉估仓位，和引擎口径不一致。
    strategies: Array.isArray(result.strategies) ? result.strategies : undefined,
    // 环境适配是只读参考；保留它，AI 解释时才能说明“当前环境适合哪套策略”，
    // 而不是只看单只股票的买卖条件。
    regime_guidance: result.regime_guidance,
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
    market_context: report.market_context,
    regime_guidance: report.regime_guidance,
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

const CANDIDATE_REVIEW_REPORT_KEYS = [
  "mode",
  "pool_type",
  "scanned_at",
  "universe_mode",
  "universe_size",
  "batch_start",
  "batch_size",
  "coverage",
  "completed_round",
  "ineligible_symbols",
  "eligible_symbol_count",
  "deferred_symbol_count",
  "insufficient_symbol_count",
  "stale_symbol_count",
  "expected_trade_date",
  "retryable_error_count",
  "snapshot_histories_updated",
  "market_context",
  "market_gate",
  "market_gate_signals",
  "signal_execution_policy",
  "candidate_count",
  "observed_count",
  "condition_funnel",
  "volume_ratio_distribution",
  "timings",
  "delivery",
  "research_only",
  "entry_reference",
  "config",
  "paper_trade",
  "notify_enabled",
  "new_events",
] as const;

function pickReviewFields(report: JsonObject): JsonObject {
  const picked: JsonObject = {};
  for (const key of CANDIDATE_REVIEW_REPORT_KEYS) {
    if (key in report) picked[key] = report[key];
  }
  return picked;
}

function boardOf(value: unknown): string {
  const symbol = String(value ?? "");
  if (symbol.startsWith("68")) return "科创板";
  if (symbol.startsWith("60")) return "沪市主板";
  if (symbol.startsWith("30")) return "创业板";
  if (symbol.startsWith("00")) return "深市主板";
  if (/^[48]/.test(symbol)) return "北交所";
  return "未知";
}

function reviewCandidate(value: unknown): JsonObject {
  const candidate = { ...asObject(value) };
  // 顶层 market_context 已保留，逐候选重复一份只会挤掉复核需要的明细。
  delete candidate.market_context;
  if (Array.isArray(candidate.chan_signals)) {
    candidate.chan_signals = candidate.chan_signals.slice(0, 5);
  }
  return candidate;
}

function summarizeCandidates(values: unknown[], symbolLimit = 200): JsonObject {
  const rows = values.map(asObject);
  const modes: Record<string, number> = {};
  const boards: Record<string, number> = {};
  const symbols: unknown[] = [];
  const confirmedDates = new Set<string>();
  let entryGateBlocked = 0;
  let observeOnly = 0;
  let executable = 0;
  let withStockPoolWarnings = 0;
  let withFundamentalWarnings = 0;
  for (const row of rows) {
    const mode = typeof row.execution_mode === "string" ? row.execution_mode : "";
    if (mode) modes[mode] = (modes[mode] ?? 0) + 1;
    if (mode === "observe_only") observeOnly += 1;
    if (mode === "enabled" && row.entry_gate_blocked !== true) executable += 1;
    if (row.entry_gate_blocked === true) entryGateBlocked += 1;
    const board = boardOf(row.symbol);
    boards[board] = (boards[board] ?? 0) + 1;
    if (symbols.length < symbolLimit && row.symbol) symbols.push(row.symbol);
    if (Array.isArray(row.stock_pool_warnings) && row.stock_pool_warnings.length > 0) {
      withStockPoolWarnings += 1;
    }
    if (Array.isArray(row.fundamental_warnings) && row.fundamental_warnings.length > 0) {
      withFundamentalWarnings += 1;
    }
    const date = row.confirmed_at ?? row.signal_date;
    if (typeof date === "string" && date) confirmedDates.add(date.slice(0, 10));
  }
  const hasExecutionMode = rows.some((row) => typeof row.execution_mode === "string");
  return {
    count: rows.length,
    executable_count: hasExecutionMode ? executable : null,
    observe_only_count: hasExecutionMode ? observeOnly : null,
    entry_gate_blocked_count: entryGateBlocked,
    execution_mode_counts: Object.keys(modes).length > 0 ? modes : null,
    board_counts: boards,
    symbols,
    symbols_truncated: rows.length > symbols.length,
    confirmed_dates: [...confirmedDates].sort(),
    candidates_with_stock_pool_warnings: withStockPoolWarnings,
    candidates_with_fundamental_warnings: withFundamentalWarnings,
  };
}

function reviewDataFreshness(
  report: JsonObject,
  candidates: JsonObject[],
  observed: JsonObject[]
): JsonObject {
  const marketContext = asObject(report.market_context);
  const missing = [
    "stale_symbol_count",
    "expected_trade_date",
    "market_gate",
    "market_gate_signals",
    "stock_pool",
    "fundamental",
    "condition_funnel",
  ].filter((key) => report[key] === null || report[key] === undefined);
  const dataDates = new Set<string>();
  for (const row of [...candidates, ...observed]) {
    for (const value of [
      row.confirmed_at,
      row.signal_date,
      asObject(row.stock_pool_metrics).as_of,
    ]) {
      if (typeof value === "string" && value) dataDates.add(value.slice(0, 10));
    }
  }
  return {
    scanned_at: report.scanned_at,
    scanned_at_timezone: "Asia/Shanghai",
    expected_trade_date: report.expected_trade_date ?? null,
    market_index_last_day: marketContext.index_last_day ?? null,
    candidate_data_dates: [...dataDates].sort(),
    stale_symbol_count: report.stale_symbol_count ?? null,
    insufficient_symbol_count: report.insufficient_symbol_count ?? null,
    deferred_symbol_count: report.deferred_symbol_count ?? null,
    eligible_symbol_count: report.eligible_symbol_count ?? null,
    error_count: Array.isArray(report.errors) ? report.errors.length : null,
    missing_fields: missing,
  };
}

function buildCandidateReviewView(report: JsonObject, candidateLimit: number): JsonObject {
  const candidates = Array.isArray(report.candidates) ? report.candidates : [];
  const observed = Array.isArray(report.observed_candidates) ? report.observed_candidates : [];
  const selected = candidates.slice(0, candidateLimit).map(reviewCandidate);
  const selectedObserved = observed.slice(0, candidateLimit).map(reviewCandidate);
  const errors = Array.isArray(report.errors) ? report.errors : [];
  const stockPool = asObject(report.stock_pool);
  const stockPoolDetails = Array.isArray(stockPool.rejection_details)
    ? stockPool.rejection_details
    : [];
  const stockPoolErrors = Array.isArray(stockPool.data_errors) ? stockPool.data_errors : [];
  const fundamental = asObject(report.fundamental);
  const fundamentalDetails = Array.isArray(fundamental.details) ? fundamental.details : [];
  return {
    ...pickReviewFields(report),
    candidate_count: report.candidate_count ?? candidates.length,
    candidates_total: candidates.length,
    candidates_truncated: candidates.length > selected.length,
    observed_count: report.observed_count ?? observed.length,
    observed_total: observed.length,
    observed_candidates_truncated: observed.length > selectedObserved.length,
    candidates: selected,
    observed_candidates: selectedObserved,
    candidate_summary: summarizeCandidates(candidates),
    observed_summary: summarizeCandidates(observed),
    data_freshness: reviewDataFreshness(report, candidates.map(asObject), observed.map(asObject)),
    stock_pool: {
      ...stockPool,
      rejection_details: stockPoolDetails.slice(0, 100),
      rejection_details_truncated: stockPoolDetails.length > 100,
      data_errors: stockPoolErrors.slice(0, 50),
      data_errors_truncated: stockPoolErrors.length > 50,
    },
    fundamental: {
      ...fundamental,
      details: fundamentalDetails.slice(0, 100),
      details_truncated: fundamentalDetails.length > 100,
    },
    errors: errors.slice(0, 100),
    errors_truncated: errors.length > 100,
  };
}

/**
 * 候选复核上下文：与 AI 解读不同，复核必须拿到闸门、股票池、候选分类和
 * 数据时效等完整字段；通用压缩会把这些“非行情”字段全部丢掉。
 */
export function buildCandidateReviewContext(reportText: string, maxChars = 60_000): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(reportText);
  } catch {
    return reportText.length <= maxChars ? reportText : reportText.slice(0, maxChars);
  }
  const report = asObject(parsed);
  for (const limit of [200, 100, 50, 20, 0]) {
    const text = JSON.stringify(buildCandidateReviewView(report, limit), null, 2);
    if (text.length <= maxChars) return text;
  }
  const fallback = buildCandidateReviewView(report, 0);
  delete fallback.candidates;
  delete fallback.observed_candidates;
  const text = JSON.stringify(fallback, null, 2);
  return text.length <= maxChars ? text : text.slice(0, maxChars);
}

/** 候选复核页签的系统提示词：只做结论/风险/数据缺口/复核清单，不给交易指令。 */
export const CANDIDATE_REVIEW_SYSTEM_PROMPT =
  "你是 A 股量化候选池的复核助手。你的任务不是推荐买卖，而是检查扫描结果是否可信、有哪些风险和数据缺口，并给出人工复核清单。\n\n" +
  "严格只输出 Markdown，且必须包含以下四个二级标题，顺序固定：\n" +
  "## 结论\n## 风险\n## 数据缺口\n## 复核清单\n\n" +
  "要求：\n" +
  "- 结论：1-3 句话，说明本次扫描覆盖多少标的、候选多少、其中可执行与仅观察各多少；若候选为 0，明确指出是扫描未完成、过滤条件还是数据问题。\n" +
  "- 风险：按市场闸门、仓位闸门、量能、数据时效、集中度、异常值分类；只使用报告中的字段，不臆测。\n" +
  "- 数据缺口：列出 coverage 不足、errors/stale_symbol_count、缺失字段、不可用基本面或新闻等；没有缺口就写“未发现”。\n" +
  "- 复核清单：5-10 条可执行检查项，用 Markdown checklist（- [ ]），每条包含具体股票代码、字段、日期或检查方法。\n" +
  "- 不输出买入/卖出/加仓/减仓/仓位比例等交易指令；不预测涨跌；不改变任何信号。\n" +
  "- 如果报告显示扫描尚未完成（completed_round=false），把“等待扫描完成”列为复核清单第一条。\n" +
  "- 引用数字时保留报告原值，不要四舍五入到失真。\n" +
  "- 优先引用服务端已算好的 candidate_summary / observed_summary / data_freshness；其中的 executable_count、observe_only_count、entry_gate_blocked_count、board_counts、symbols 都是确定性结果，不要重新猜测。\n" +
  "- 市场闸门、仓位闸门、量能必须从 market_gate、market_gate_signals、signal_execution_policy、stock_pool、fundamental 中找依据；不要因为没看到某个词就写“未提供”。\n" +
  "- 只有 data_freshness.missing_fields 列出的字段才能写“报告未提供”；errors=[] 不等于 stale_symbol_count=0。";

/** AI 解读的系统提示词：必须结合【我的持仓】给出明确操作主张。 */
export const INTERPRET_SYSTEM_PROMPT =
  "你是资深 A 股量化分析助手。MACD 金叉定义为 DIF 上穿 DEA，并按 0轴上方、0轴附近、0轴下方排序；结合温和放量、突破 MA5/MA10、红柱连续放大确认。基于报告逐股输出主要信号、缠论买卖点、多周期一致性、风险。若用户提供【我的持仓】，必须结合持仓与现价浮盈浮亏给出明确操作主张：对已持仓股票，在「加仓 / 持有 / 减仓 / 清仓」中选一个并说明理由（结合仓位占比、盈亏与信号强度）；对未持仓股票，明确建议「可建仓 / 暂不建仓」，可建仓时给出建议仓位比例。未提供持仓信息时，仅给候选动作（观察/买入候选/减仓候选/规避），不要虚构持仓。分批减仓必须照抄报告里 exit_plan_advice 的数字：当持仓股的主张是「减仓」时，直接引用 next 档位的触发价与建议股数（例如「涨到 11.50 卖 500 股」）并说明它占原始仓位的比例；若 breakeven_active 为真，还要说明尾仓止损已上移到成本价、跌破即清掉剩余股数；若各档已执行完，说明尾仓按止损/超时管理。不要自行估算减仓比例或股数，也不要建议卖得比该档位更多；有更激进的看法时写在理由里，但执行数字以报告为准。报告里没有 exit_plan_advice 时，只按原有的加仓/持有/减仓/清仓主张作答，不要编造档位。不要承诺胜率，使用简洁 Markdown。\n\n输出格式：解读正文开头必须有一个「## 操作主张」小节，按股票逐行列出，每行格式为「- **代码 名称**：主张（一句话理由）」，主张只能是 加仓/持有/减仓/清仓/可建仓/暂不建仓 之一；该小节之后才是详细分析。若没有任何可给主张的股票，该小节写「暂无操作主张」。";

/** 主张关键词：模型允许给出的操作主张。 */
const STANCE_WORDS = ["加仓", "持有", "减仓", "清仓", "可建仓", "暂不建仓"];

/**
 * 从解读正文提取「## 操作主张」小节中每只股票的 `代码 名称：主张（理由）` 行。
 * 找不到小节或没有有效行时返回空数组，不抛错。
 */
export function extractStandpoints(content: string): string[] {
  if (!content) return [];
  const sectionMatch = content.match(/##\s*操作主张\s*\n([\s\S]*?)(?=\n##\s|$)/);
  const section = sectionMatch?.[1] ?? "";
  const lines = section
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.startsWith("- "));
  const standpoints: string[] = [];
  for (const line of lines) {
    const body = line.replace(/^-\s+/, "").replace(/\*\*/g, "").trim();
    const stance = STANCE_WORDS.find((word) => body.includes(word));
    if (!stance) continue;
    const [head, ...reasonParts] = body.split(/[：:]/);
    const reason = reasonParts.join("：").trim();
    const reasonMatch = reason.match(/[（(]([^()（）]*)[)）]/);
    const reasonText = (reasonMatch?.[1] ?? reason.replace(new RegExp(`^${stance}`), "")).trim();
    const parts = head.trim();
    const match = parts.match(/(\d{6}(?:\.(?:SH|SZ|BJ))?)\s*(.*)/);
    const symbol = match?.[1] ?? "";
    const name = (match?.[2] ?? parts).trim();
    standpoints.push(`${name || symbol}${symbol ? `(${symbol})` : ""}：${stance}${reasonText ? `，${reasonText}` : ""}`);
  }
  return standpoints;
}

function buildInterpretMessages(reportText: string, supplementalContext?: string | null): ChatMessage[] {
  // Compact the structured report first.  Holdings and other supplemental
  // context are appended after compaction so they cannot invalidate JSON
  // parsing or cause the report to be truncated from its beginning.
  const supplemental = supplementalContext?.trim() ?? "";
  const reportBudget = Math.max(1_000, 60_000 - supplemental.length - 256);
  const compacted = buildInterpretationContext(reportText, reportBudget);
  const context = supplemental
    ? `${compacted}\n\n补充上下文：\n${supplemental}`
    : compacted;
  return [
    {
      role: "system",
      content: INTERPRET_SYSTEM_PROMPT,
    },
    { role: "user", content: context },
  ];
}

export async function interpretReport(
  profile: ModelProfile,
  reportText: string,
  supplementalContext?: string | null,
): Promise<string> {
  return chatCompletion(
    profile,
    buildInterpretMessages(reportText, supplementalContext),
    interpretTimeoutMs(),
    true  /* stream — avoid Cloudflare 524 origin timeout */
  );
}

/**
 * AI 解读降级链：按优先级依次尝试已启用模型，优先模型调用失败自动切换下一个。
 * 返回解读内容与实际使用的模型（用于记录 analysis_note.model）。
 */
export async function interpretReportWithFallback(
  reportText: string,
  supplementalContext?: string | null,
): Promise<ChatWithFallbackResult> {
  return chatWithFallback(buildInterpretMessages(reportText, supplementalContext), {
    timeoutMs: interpretTimeoutMs(),
    stream: true,
  });
}

function buildCandidateReviewMessages(
  reportText: string,
  supplementalContext?: string | null,
): ChatMessage[] {
  const supplemental = supplementalContext?.trim() ?? "";
  const reportBudget = Math.max(1_000, 60_000 - supplemental.length - 256);
  const compacted = buildCandidateReviewContext(reportText, reportBudget);
  const context = supplemental
    ? `${compacted}\n\n补充上下文：\n${supplemental}`
    : compacted;
  return [
    { role: "system", content: CANDIDATE_REVIEW_SYSTEM_PROMPT },
    { role: "user", content: context },
  ];
}

/** 候选复核：结构化输出结论/风险/数据缺口/复核清单，不参与信号生成。 */
export async function reviewCandidatesWithFallback(
  reportText: string,
  supplementalContext?: string | null,
): Promise<ChatWithFallbackResult> {
  return chatWithFallback(buildCandidateReviewMessages(reportText, supplementalContext), {
    timeoutMs: interpretTimeoutMs(),
    stream: true,
  });
}
