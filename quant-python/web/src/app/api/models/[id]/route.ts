import { NextResponse } from "next/server";
import { deleteModel, getModel, updateModel } from "@/lib/db";

export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const model = await getModel(Number(id));
  if (!model) return NextResponse.json({ error: "Model not found" }, { status: 404 });
  return NextResponse.json({ model: { ...model, api_key: model.api_key ? "****" : "" } });
}

export async function PUT(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const modelId = Number(id);
  const current = await getModel(modelId);
  if (!current) return NextResponse.json({ error: "Model not found" }, { status: 404 });
  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "Request body must be JSON" }, { status: 400 });
  }
  const patch: Record<string, unknown> = {};
  if (typeof body.name === "string" && body.name.trim()) patch.name = body.name.trim();
  if (typeof body.base_url === "string" && body.base_url.trim()) patch.base_url = body.base_url.trim().replace(/\/$/, "");
  if (typeof body.model === "string" && body.model.trim()) patch.model = body.model.trim();
  if (typeof body.api_key === "string" && body.api_key !== "****") patch.api_key = body.api_key;
  if (typeof body.env_key === "string") patch.env_key = body.env_key.trim();
  if (typeof body.proxy === "string") patch.proxy = body.proxy.trim();
  if (typeof body.enabled === "boolean") patch.enabled = body.enabled;
  if (typeof body.vision_supported === "boolean") patch.vision_supported = body.vision_supported;
  await updateModel(modelId, patch);
  return NextResponse.json({ ok: true });
}

export async function DELETE(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const modelId = Number(id);
  if (!(await getModel(modelId))) return NextResponse.json({ error: "Model not found" }, { status: 404 });
  await deleteModel(modelId);
  return NextResponse.json({ ok: true });
}
