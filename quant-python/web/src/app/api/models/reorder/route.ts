import { NextResponse } from "next/server";
import { listModels, reorderModels } from "@/lib/db";

/**
 * 全量重排模型使用顺序：body.ids 为模型 id 的完整顺序（从首选到末尾），
 * 服务端按该顺序重写 priority（0..n-1）。要求传入全部模型 id，
 * 防止部分排序把未包含的模型静默重置。
 */
export async function PUT(request: Request) {
  let body: { ids?: unknown };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const ids = Array.isArray(body.ids) ? body.ids : null;
  if (
    !ids ||
    ids.length === 0 ||
    !ids.every((id) => Number.isInteger(id) && Number(id) > 0)
  ) {
    return NextResponse.json({ error: "ids 必须是正整数数组" }, { status: 422 });
  }
  const models = await listModels();
  const existingIds = new Set(models.map((model) => model.id));
  const numericIds = ids.map(Number);
  const uniqueIds = [...new Set(numericIds)];
  if (
    numericIds.length !== existingIds.size ||
    uniqueIds.length !== numericIds.length ||
    !numericIds.every((id) => existingIds.has(id))
  ) {
    return NextResponse.json(
      { error: "ids 必须包含且仅包含全部模型（全量排序）" },
      { status: 422 }
    );
  }
  await reorderModels(uniqueIds);
  return NextResponse.json({ ok: true });
}