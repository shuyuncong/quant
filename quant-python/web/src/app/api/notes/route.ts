import { NextResponse } from "next/server";
import { listNotes } from "@/lib/db";

export async function GET() {
  try {
    const notes = listNotes(300);
    return NextResponse.json({ notes });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "读取 AI 解读失败" },
      { status: 500 }
    );
  }
}
