import { NextResponse } from "next/server";
import { createPendingImport } from "@/lib/db";
import { pickVisionModel, recognizeSymbols } from "@/lib/llm";

const MAX_DATA_URL_LENGTH = 30 * 1024 * 1024;

export async function POST(request: Request) {
  let body: { dataUrl?: unknown };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const dataUrl = String(body.dataUrl ?? "");
  if (!dataUrl.startsWith("data:image/")) {
    return NextResponse.json({ error: "请上传图片（data URL）" }, { status: 422 });
  }
  if (dataUrl.length > MAX_DATA_URL_LENGTH) {
    return NextResponse.json({ error: "图片过大（最大约 20MB）" }, { status: 422 });
  }
  const profile = pickVisionModel();
  if (!profile) {
    return NextResponse.json(
      { error: "未配置可用的视觉模型（需启用并配置 API Key），请改用文本导入" },
      { status: 409 }
    );
  }
  try {
    const candidates = await recognizeSymbols(profile, dataUrl);
    if (candidates.length === 0) {
      return NextResponse.json({ error: "模型未能识别出股票代码，请尝试文本导入" }, { status: 422 });
    }
    const pendingId = createPendingImport("image", dataUrl.slice(0, 500), { symbols: candidates });
    return NextResponse.json({ ok: true, pending_id: pendingId, candidates, model: profile.name });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? `图片识别失败: ${error.message}` : "图片识别失败" },
      { status: 502 }
    );
  }
}
