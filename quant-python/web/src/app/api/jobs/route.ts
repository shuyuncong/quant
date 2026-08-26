import { NextResponse } from "next/server";
import { listJobsWithNote, listPool } from "@/lib/db";

export async function GET() {
  const pool = await listPool();
  const nameMap: Record<string, string> = {};
  for (const row of pool) {
    nameMap[row.symbol] = row.name;
  }

  const jobs = (await listJobsWithNote(100)).map((job) => {
    let payload: Record<string, unknown> = {};
    try {
      payload = JSON.parse(job.payload) as Record<string, unknown>;
    } catch {
      /* ignore */
    }
    const symbols: string[] = Array.isArray(payload.symbols) ? payload.symbols : [];
    const names = symbols.map((s) => nameMap[s] ?? s).join(", ");
    return {
      ...job,
      payload,
      symbol_names: names,
    };
  });
  return NextResponse.json({ jobs });
}
