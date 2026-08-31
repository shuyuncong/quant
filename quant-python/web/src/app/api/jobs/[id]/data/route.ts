import fs from "node:fs";
import { NextResponse } from "next/server";
import { getJob } from "@/lib/db";

const BAR_LIMIT = 30;
const CANDIDATE_LIMIT = 100;

interface BarRow {
  datetime: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  dif: number | null;
  dea: number | null;
  hist: number | null;
}

function compactBars(bars: unknown): BarRow[] {
  if (!Array.isArray(bars)) return [];
  return bars.slice(-BAR_LIMIT).map((bar) => {
    const row = (bar ?? {}) as Record<string, unknown>;
    const num = (v: unknown) => (typeof v === "number" ? v : null);
    return {
      datetime: String(row.datetime ?? ""),
      open: num(row.open),
      high: num(row.high),
      low: num(row.low),
      close: num(row.close),
      volume: num(row.volume),
      dif: num(row.dif),
      dea: num(row.dea),
      hist: num(row.hist),
    };
  });
}

function compactTimeframe(timeframe: string, value: unknown) {
  const tf = (value ?? {}) as Record<string, unknown>;
  const bars = Array.isArray(tf.recent_bars) ? tf.recent_bars : [];
  return {
    timeframe,
    status: String(tf.status ?? "unknown"),
    latest_time: tf.latest_time != null ? String(tf.latest_time) : null,
    latest_price: typeof tf.latest_price === "number" ? tf.latest_price : null,
    bar_count: bars.length,
    buy_score: typeof tf.buy_score === "number" ? tf.buy_score : null,
    sell_score: typeof tf.sell_score === "number" ? tf.sell_score : null,
    error: tf.error != null ? String(tf.error) : null,
    bars: compactBars(bars),
  };
}

export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  try {
    const jobId = Number(id);
    if (!Number.isInteger(jobId) || jobId <= 0) {
      return NextResponse.json({ error: "任务 ID 无效" }, { status: 400 });
    }
    const job = await getJob(jobId);
    if (!job) return NextResponse.json({ error: "任务不存在" }, { status: 404 });
    if (!job.result_path) {
      return NextResponse.json({ error: "该任务没有结果文件" }, { status: 409 });
    }
    const full = job.result_path;
    if (!full.endsWith(".json") || !fs.existsSync(full) || !fs.statSync(full).isFile()) {
      return NextResponse.json({ error: `结果文件不存在：${full}` }, { status: 404 });
    }
    const report = JSON.parse(fs.readFileSync(full, "utf8")) as Record<string, unknown>;
    const results = Array.isArray(report.results)
      ? report.results.map((item) => {
          const result = (item ?? {}) as Record<string, unknown>;
          const timeframes = (result.timeframes ?? {}) as Record<string, unknown>;
          return {
            symbol: String(result.symbol ?? ""),
            name: String(result.name ?? ""),
            status: result.status != null ? String(result.status) : null,
            analyzed_at: result.analyzed_at != null ? String(result.analyzed_at) : null,
            timeframes: Object.entries(timeframes).map(([key, value]) =>
              compactTimeframe(key, value)
            ),
          };
        })
      : [];
    const candidates = Array.isArray(report.candidates)
      ? report.candidates.slice(0, CANDIDATE_LIMIT)
      : [];
    return NextResponse.json({
      mode: String(report.mode ?? ""),
      analyzed_at: report.analyzed_at != null ? String(report.analyzed_at) : null,
      scanned_at: report.scanned_at != null ? String(report.scanned_at) : null,
      market_context: report.market_context ?? null,
      delivery: report.delivery ?? null,
      results,
      candidates,
      errors: Array.isArray(report.errors) ? report.errors.slice(0, 50) : [],
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "读取数据源失败" },
      { status: 500 }
    );
  }
}
