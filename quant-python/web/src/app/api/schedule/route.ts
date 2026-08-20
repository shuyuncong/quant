import { NextResponse } from "next/server";
import { upsertScheduleRow } from "@/lib/db";
import { ensureScheduler, getSchedulerStatus } from "@/lib/scheduler";

function validateTime(value: unknown): boolean {
  return typeof value === "string" && /^\d{2}:\d{2}$/.test(value);
}

export async function GET() {
  await ensureScheduler();
  const status = await getSchedulerStatus();
  return NextResponse.json(status);
}

export async function PUT(request: Request) {
  let body: { rows?: Array<Record<string, unknown>> };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const rows = Array.isArray(body.rows) ? body.rows : null;
  if (!rows || rows.length === 0) {
    return NextResponse.json({ error: "缺少 rows" }, { status: 422 });
  }
  for (const row of rows) {
    const kind = String(row.kind ?? "");
    if (kind !== "daily_scan" && kind !== "monitor_cycle") {
      return NextResponse.json({ error: `未知定时类型: ${kind}` }, { status: 422 });
    }
    const input: Record<string, unknown> = {};
    if (row.time !== undefined) {
      if (!validateTime(row.time)) {
        return NextResponse.json({ error: "时间格式应为 HH:MM" }, { status: 422 });
      }
      input.time = row.time;
    }
    if (row.interval_seconds !== undefined) {
      const interval = Number(row.interval_seconds);
      if (!Number.isInteger(interval) || interval < 10 || interval > 86400) {
        return NextResponse.json({ error: "监控间隔应为 10-86400 秒的整数" }, { status: 422 });
      }
      input.interval_seconds = interval;
    }
    if (row.fixed_times !== undefined) {
      const fixedTimes = row.fixed_times;
      if (
        !Array.isArray(fixedTimes) ||
        fixedTimes.some((item) => typeof item !== "string" || !/^\d{2}:\d{2}$/.test(item))
      ) {
        return NextResponse.json(
          { error: "fixed_times 应为 HH:MM 字符串数组" },
          { status: 422 }
        );
      }
      input.fixed_times = fixedTimes;
    }
    if (typeof row.trading_days_only === "boolean") input.trading_days_only = row.trading_days_only;
    if (typeof row.enabled === "boolean") input.enabled = row.enabled;
    await upsertScheduleRow(kind, input);
  }
  await ensureScheduler();
  const status = await getSchedulerStatus();
  return NextResponse.json({ ok: true, ...status });
}
