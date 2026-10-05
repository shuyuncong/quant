import type { Response } from "undici";
import type { ModelProtocol, ReasoningEffort } from "./model-protocol";

type Json = Record<string, unknown>;
const object = (value: unknown): Json => value && typeof value === "object" ? value as Json : {};

/** Applied only after a gateway explicitly rejects a normal Responses request as an invalid Codex request. */
export function codexCompatibleRequest(body: Json, cacheKey: string): Json {
  return {
    ...body,
    input: Array.isArray(body.input) ? body.input.map((item: unknown) => ({ type: "message", ...object(item) })) : body.input,
    stream: true,
    store: false,
    text: { ...object(body.text), verbosity: "low" },
    include: [...new Set([...(Array.isArray(body.include) ? body.include : []), "reasoning.encrypted_content"])],
    prompt_cache_key: cacheKey,
  };
}

export function responsesInput(messages: Array<{ role: string; content: unknown }>) {
  return messages.map(({ role, content }) => ({
    role,
    content: Array.isArray(content) ? content.map((part: unknown) => {
      const item = object(part);
      if (item.type === "text" && typeof item.text === "string") {
        return { type: role === "assistant" ? "output_text" : "input_text", text: item.text };
      }
      const image = object(item.image_url);
      if (item.type === "image_url" && typeof image.url === "string") {
        return { type: "input_image", image_url: image.url, detail: image.detail ?? "auto" };
      }
      throw new Error("Responses 协议收到不支持的消息内容类型");
    }) : typeof content === "string"
      ? [{ type: role === "assistant" ? "output_text" : "input_text", text: content }]
      : content,
  }));
}

export function responsesRequest(messages: Array<{ role: string; content: unknown }>, effort: ReasoningEffort = "default") {
  const instructions = messages.filter(message => message.role === "system" || message.role === "developer").map(message => {
    if (typeof message.content === "string") return message.content;
    if (Array.isArray(message.content)) return message.content.map((part: unknown) => {
      const item = object(part);
      if (item.type !== "text" || typeof item.text !== "string") throw new Error("系统提示仅支持文本");
      return item.text;
    }).join("\n");
    throw new Error("系统提示仅支持文本");
  }).join("\n\n");
  return {
    instructions,
    input: responsesInput(messages.filter(message => message.role !== "system" && message.role !== "developer")),
    store: false,
    stream: true,
    ...(effort === "default" ? {} : { reasoning: { effort } }),
  };
}

function checkError(data: Json) {
  if (data.error) {
    throw new Error(String(object(data.error).message ?? "模型服务返回错误"));
  }
  if (data.status === "failed" || data.status === "incomplete" || data.status === "cancelled") {
    throw new Error(`模型响应未完成（${data.status}）：${String(object(data.incomplete_details).reason ?? "服务未成功完成生成")}`);
  }
}

export function responseText(value: unknown, protocol: ModelProtocol): string {
  const data = object(value);
  checkError(data);
  if (protocol === "chat_completions") {
    const choice = object(Array.isArray(data.choices) ? data.choices[0] : null);
    const content = object(choice.message).content;
    return typeof content === "string" ? content : "";
  }
  if (typeof data.output_text === "string" && data.output_text) return data.output_text;
  if (!Array.isArray(data.output)) return "";
  return data.output.flatMap((raw: unknown) => {
    const item = object(raw);
    if (item.type !== "message" || !Array.isArray(item.content)) return [];
    return item.content.map((part: unknown) => {
      const block = object(part);
      return block.type === "output_text" && typeof block.text === "string" ? block.text : "";
    });
  }).join("");
}

/** Decode SSE frames, including CRLF, split UTF-8 and a final frame without a newline. */
export async function protocolStreamText(response: Response, protocol: ModelProtocol): Promise<string> {
  if (!response.body) throw new Error("模型流式响应无 body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let content = "";
  let completed = false;
  let finished = false;
  const consume = (line: string) => {
    if (!line.startsWith("data:")) return;
    const payload = line.slice(5).trim();
    if (!payload) return;
    if (payload === "[DONE]") { finished = true; return; }
    let chunk: Json;
    try { chunk = object(JSON.parse(payload)); } catch { return; }
    checkError(chunk);
    if (protocol === "chat_completions") {
      const choice = object(Array.isArray(chunk.choices) ? chunk.choices[0] : null);
      const delta = object(choice.delta).content;
      if (typeof delta === "string") content += delta;
      return;
    }
    if (chunk.type === "error") throw new Error(String(chunk.message ?? "Responses 流式服务错误"));
    if (chunk.type === "response.failed" || chunk.type === "response.incomplete") {
      checkError(object(chunk.response));
      throw new Error(`模型响应未完成（${chunk.type}）`);
    }
    if (chunk.type === "response.output_text.delta" && typeof chunk.delta === "string") content += chunk.delta;
    if (chunk.type === "response.completed") {
      const full = responseText(chunk.response, "responses");
      if (full) content = full;
      completed = true;
      finished = true;
    }
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        consume(line.replace(/\r$/, ""));
        if (finished) break;
      }
      if (finished) break;
      if (done) { if (buffer) consume(buffer); break; }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  if (protocol === "responses" && !completed) throw new Error("Responses 流提前结束，未收到 response.completed");
  if (!content) throw new Error("模型流式返回内容为空");
  return content;
}
