import { NextResponse } from "next/server";
import { getTotalCapital, setTotalCapital } from "@/lib/db";

export async function GET() {
  return NextResponse.json({ total_capital: getTotalCapital() });
}

export async function POST(request: Request) {
  let body: { total_capital?: unknown };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const value = Number(body.total_capital);
  if (!Number.isFinite(value) || value < 0) {
    return NextResponse.json({ error: "账户总资金必须是合法的非负数字" }, { status: 422 });
  }
  setTotalCapital(value);
  return NextResponse.json({ ok: true, total_capital: getTotalCapital() });
}