import { NextResponse } from "next/server";
import { createModel, listModels } from "@/lib/db";
import { isModelProtocol, isReasoningEffort, modelEndpoint } from "@/lib/model-protocol";

export async function GET() {
  const models = (await listModels()).map((model) => ({
    ...model,
    api_key: model.api_key ? "****" : "",
    env_present: Boolean(model.env_key && process.env[model.env_key]),
  }));
  return NextResponse.json({ models });
}

export async function POST(request: Request) {
  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const name = String(body.name ?? "").trim();
  const protocol = body.protocol === undefined ? "chat_completions" : body.protocol;
  if (!isModelProtocol(protocol)) return NextResponse.json({ error: "不支持的模型协议" }, { status: 422 });
  const reasoning_effort = body.reasoning_effort === undefined ? "default" : body.reasoning_effort;
  if (!isReasoningEffort(reasoning_effort)) return NextResponse.json({ error: "不支持的推理强度" }, { status: 422 });
  const baseUrl = String(body.base_url ?? "").trim().replace(/\/$/, "");
  const model = String(body.model ?? "").trim();
  if (!name || !baseUrl || !model) {
    return NextResponse.json({ error: "名称、Base URL、模型名必填" }, { status: 422 });
  }
  if (!/^https?:\/\//.test(baseUrl)) {
    return NextResponse.json({ error: "Base URL 必须以 http(s):// 开头" }, { status: 422 });
  }
  try { modelEndpoint(baseUrl, protocol); } catch (error) {
    return NextResponse.json({ error: (error as Error).message }, { status: 422 });
  }
  const proxy = String(body.proxy ?? "").trim();
  if (proxy && !/^https?:\/\//.test(proxy)) {
    return NextResponse.json({ error: "代理地址必须以 http(s):// 开头" }, { status: 422 });
  }
  const id = await createModel({
    protocol,
    reasoning_effort,
    name,
    base_url: baseUrl,
    model,
    api_key: String(body.api_key ?? ""),
    env_key: String(body.env_key ?? "").trim(),
    proxy,
    enabled: Boolean(body.enabled),
    vision_supported: body.vision_supported !== false,
  });
  return NextResponse.json({ ok: true, id }, { status: 201 });
}
