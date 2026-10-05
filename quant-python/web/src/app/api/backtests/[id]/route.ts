import path from "node:path";
import { NextResponse } from "next/server";
import { getJob,getDb } from "@/lib/db";
import { executeBacktest,readBacktestFile } from "@/lib/backtest-service";
import { assertLocalResearch } from "@/lib/local-research";
export async function GET(_request:Request,{params}:{params:Promise<{id:string}>}) {
  try {
    assertLocalResearch();
    const job=await getJob(Number((await params).id));
    if(!job||job.kind!=="backtest")return NextResponse.json({error:"回测不存在"},{status:404});
    const payload=JSON.parse(job.payload);
    const progress=await readBacktestFile(path.join(payload.options.output_dir,"progress.json")).catch(()=>null);
    const report=job.result_path?await readBacktestFile(job.result_path):null;
    return NextResponse.json({job,progress,report});
  }catch(error){return NextResponse.json({error:String(error)},{status:400});}
}
export async function POST(_request:Request,{params}:{params:Promise<{id:string}>}) {
  try {
    assertLocalResearch();
    const id=Number((await params).id),job=await getJob(id);
    if(!job||job.kind!=="backtest"||job.status!=="failed")return NextResponse.json({error:"仅失败的回测可重试"},{status:409});
    const claimed=await(await getDb()).query("UPDATE quant.jobs SET status='pending',error=NULL,finished_at=NULL WHERE id=$1 AND status='failed' RETURNING id",[id]);
    if(!claimed.rowCount)return NextResponse.json({error:"任务已被重试"},{status:409});
    void executeBacktest(id).catch(console.error);
    return NextResponse.json({ok:true},{status:202});
  }catch(error){return NextResponse.json({error:String(error)},{status:400});}
}
