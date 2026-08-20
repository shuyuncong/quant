import { NextResponse } from "next/server";
import { listJobs } from "@/lib/db";

export async function GET() {
  const jobs = (await listJobs(100)).map((job) => ({
    ...job,
    payload: (() => {
      try {
        return JSON.parse(job.payload) as unknown;
      } catch {
        return {};
      }
    })(),
  }));
  return NextResponse.json({ jobs });
}
