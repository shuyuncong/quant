import { NextResponse } from "next/server";
import { createModel, listModels } from "@/lib/db";

export async function GET() {
  const models = listModels().map((model) => ({
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
  const baseUrl = String(body.base_url ?? "").trim().replace(/\/$/, "");
  const model = String(body.model ?? "").trim();
  if (!name || !baseUrl || !model) {
    return NextResponse.json({ error: "名称、Base URL、模型名必填" }, { status: 422 });
  }
  if (!/^https?:\/\//.test(baseUrl)) {
    return NextResponse.json({ error: "Base URL 必须以 http(s):// 开头" }, { status: 422 });
  }
  const id = createModel({
    name,
    base_url: baseUrl,
    model,
    api_key: String(body.api_key ?? ""),
    env_key: String(body.env_key ?? "").trim(),
    enabled: Boolean(body.enabled),
    vision_supported: body.vision_supported !== false,
  });
  return NextResponse.json({ ok: true, id }, { status: 201 });
}
