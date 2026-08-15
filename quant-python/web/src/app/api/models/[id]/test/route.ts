import { NextResponse } from "next/server";
import { testModelById } from "@/lib/llm";

export async function POST(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const result = await testModelById(Number(id));
  return NextResponse.json(result, { status: result.ok ? 200 : 422 });
}
