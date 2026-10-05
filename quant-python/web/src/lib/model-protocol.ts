export type ModelProtocol = "chat_completions" | "responses";
export const REASONING_EFFORTS = ["default", "low", "medium", "high", "xhigh", "max"] as const;
export type ReasoningEffort = typeof REASONING_EFFORTS[number];
export function isReasoningEffort(value: unknown): value is ReasoningEffort {
  return typeof value === "string" && REASONING_EFFORTS.some(effort => effort === value);
}

export function isModelProtocol(value: unknown): value is ModelProtocol {
  return value === "chat_completions" || value === "responses";
}

export const MODEL_PROTOCOL_LABELS: Record<ModelProtocol, string> = {
  chat_completions: "Chat Completions",
  responses: "Responses",
};

export function modelEndpoint(baseUrl: string, protocol: ModelProtocol): string {
  const url = new URL(baseUrl);
  if (!/^https?:$/.test(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error("Base URL 必须是 HTTP(S) 地址，且不包含凭据、查询参数或片段");
  }
  const root = url.pathname.replace(/\/+$/, "").replace(/\/(?:chat\/completions|responses)$/, "");
  url.pathname = `${root}/${protocol === "responses" ? "responses" : "chat/completions"}`;
  return url.toString();
}
