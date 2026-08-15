import { NextResponse } from "next/server";
import { listPendingImports } from "@/lib/db";

export async function GET() {
  const pending = listPendingImports().map((item) => ({
    ...item,
    candidates: (() => {
      try {
        return JSON.parse(item.candidates) as unknown;
      } catch {
        return [];
      }
    })(),
  }));
  return NextResponse.json({ pending });
}
