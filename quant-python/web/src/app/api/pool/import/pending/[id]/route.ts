import { NextResponse } from "next/server";
import { setPendingStatus } from "@/lib/db";

export async function DELETE(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  setPendingStatus(Number(id), "cancelled");
  return NextResponse.json({ ok: true });
}
