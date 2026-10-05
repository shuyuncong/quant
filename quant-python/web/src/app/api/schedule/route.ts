import { NextResponse } from "next/server";
import { upsertScheduleRow, getDb } from "@/lib/db";
import type { ScheduleRow } from "@/lib/types";
import { ensureScheduler, getSchedulerStatus } from "@/lib/scheduler";
import { normalizeScope } from "@/lib/analysis-service";

function validateTime(value: unknown): boolean {
  return typeof value === "string" && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);
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
  const validated: { kind: ScheduleRow["kind"]; input: Parameters<typeof upsertScheduleRow>[1] }[] = [];
  for (const row of rows) {
    const kind = String(row.kind ?? "");
    if (kind !== "daily_scan" && kind !== "monitor_cycle" && kind !== "monitor_fixed") {
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
      if (!Number.isInteger(interval) || interval < 60 || interval > 86400) {
        return NextResponse.json({ error: "监控间隔应为 1-1440 分钟" }, { status: 422 });
      }
      input.interval_seconds = interval;
    }
    if (row.fixed_times !== undefined) {
      const fixedTimes = row.fixed_times;
      if (
        !Array.isArray(fixedTimes) ||
        fixedTimes.some((item) => !validateTime(item))
      ) {
        return NextResponse.json(
          { error: "fixed_times 应为 HH:MM 字符串数组" },
          { status: 422 }
        );
      }
      input.fixed_times = [...new Set(fixedTimes)].sort();
    }
    if (typeof row.trading_days_only === "boolean") input.trading_days_only = row.trading_days_only;
    if (typeof row.enabled === "boolean") input.enabled = row.enabled;
    if (row.scope !== undefined) {
      try { input.scope = normalizeScope(row.scope); }
      catch (error) { return NextResponse.json({ error: String(error) }, { status: 422 }); }
    }
    if (kind === "monitor_fixed" && row.enabled === true && Array.isArray(input.fixed_times) && !input.fixed_times.length) return NextResponse.json({ error: "请至少设置一个固定时点" }, { status: 422 });
    validated.push({ kind, input });
  }
  if (new Set(validated.map(row => row.kind)).size !== validated.length) return NextResponse.json({ error: "定时类型不可重复" }, { status: 422 });
  const client = await (await getDb()).connect();
  try {
    await client.query("BEGIN");
    for (const row of validated) await upsertScheduleRow(row.kind, row.input, client);
    await client.query("COMMIT");
  } catch (error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
  await ensureScheduler();
  const status = await getSchedulerStatus();
  return NextResponse.json({ ok: true, ...status });
}
