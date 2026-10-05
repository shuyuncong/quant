import { NextResponse } from "next/server";
import { getDb, getJob, updateJob } from "@/lib/db";
import { executeScanBatch } from "@/lib/scan-service";
import { executeAnalysisBatch } from "@/lib/analysis-service";
import { nowIso } from "@/lib/time";
export async function POST(_request:Request,{params}:{params:Promise<{id:string}>}){
  const id=Number((await params).id);
  if(!Number.isSafeInteger(id)||id<1)return NextResponse.json({error:"任务编号无效"},{status:422});
  const job=await getJob(id);
  if(!job)return NextResponse.json({error:"任务不存在"},{status:404});
  const payload=JSON.parse(job.payload);
  if(job.status!=="failed"||(!payload.scan_version&&!payload.analysis_version))return NextResponse.json({error:"只支持重试失败的新分析或筛选任务"},{status:409});
  const claimed=await(await getDb()).query("UPDATE quant.jobs SET status='pending',error=NULL,finished_at=NULL WHERE id=$1 AND status='failed' RETURNING id",[id]);
  if(!claimed.rowCount)return NextResponse.json({error:"任务已被重试"},{status:409});
  void (payload.scan_version===2?executeScanBatch(id):executeAnalysisBatch(id)).catch(error=>updateJob(id,{status:"failed",error:String(error),finished_at:nowIso()}));
  return NextResponse.json({ok:true,jobId:id},{status:202});
}
