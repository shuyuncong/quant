import { NextResponse } from "next/server";
import { getDb } from "@/lib/db";
import { startBacktest } from "@/lib/backtest-service";
import { localResearchAvailable } from "@/lib/local-research";
export async function GET() {
  const available=localResearchAvailable();
  const jobs=available?(await(await getDb()).query("SELECT * FROM quant.jobs WHERE kind='backtest' ORDER BY id DESC LIMIT 30")).rows:[];
  return NextResponse.json({available,jobs});
}
export async function POST(request:Request) {
  try {return NextResponse.json({ok:true,jobId:await startBacktest(await request.json())},{status:202});}
  catch(error){return NextResponse.json({error:error instanceof Error?error.message:String(error)},{status:422});}
}
