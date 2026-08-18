import fs from "node:fs";
import path from "node:path";
import { NextResponse } from "next/server";
import { getEffectiveConfig } from "@/lib/config";

export async function GET() {
  try {
    const effective = await getEffectiveConfig();
    const outputDir = effective.runtime.output_dir;
    if (!outputDir || !fs.existsSync(outputDir)) {
      return NextResponse.json({ results: [] });
    }
    const files = fs
      .readdirSync(outputDir)
      .filter((name) => name.endsWith(".json"))
      .map((name) => {
        const full = path.join(outputDir, name);
        let stat;
        try {
          stat = fs.statSync(full);
        } catch {
          return null;
        }
        let summary: Record<string, unknown> = {};
        try {
          const parsed = JSON.parse(fs.readFileSync(full, "utf8")) as Record<string, unknown>;
          summary = {
            mode: parsed.mode ?? "",
            scanned_at: parsed.scanned_at ?? parsed.analyzed_at ?? "",
            symbols: parsed.symbols ?? undefined,
            new_events: parsed.new_events ?? undefined,
            candidate_count: parsed.candidate_count ?? undefined,
            delivery: parsed.delivery ?? undefined,
            output_file: parsed.output_file ?? full,
          };
        } catch {
          summary = {};
        }
        return { file: name, mtime: stat.mtimeMs, path: full, summary };
      })
      .filter((item): item is NonNullable<typeof item> => item !== null)
      .sort((a, b) => b.mtime - a.mtime)
      .slice(0, 100);
    return NextResponse.json({ results: files });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "读取结果失败" },
      { status: 500 }
    );
  }
}
