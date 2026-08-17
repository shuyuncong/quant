import { NextResponse } from "next/server";
import { clearOperationLogs, listOperationLogs } from "@/lib/db";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const limit = Math.min(Number(url.searchParams.get("limit") ?? 300) || 300, 1000);
  const levelParam = url.searchParams.get("level");
  const level =
    levelParam === "info" || levelParam === "warning" || levelParam === "error" ? levelParam : undefined;
  try {
    const logs = listOperationLogs(limit, level);
    return NextResponse.json({ logs });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "读取操作日志失败" },
      { status: 500 }
    );
  }
}

export async function DELETE() {
  try {
    const cleared = clearOperationLogs();
    return NextResponse.json({ ok: true, cleared });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "清空操作日志失败" },
      { status: 500 }
    );
  }
}
