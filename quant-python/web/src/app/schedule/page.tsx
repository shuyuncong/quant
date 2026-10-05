"use client";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { AnalysisScopePicker } from "@/components/analysis-scope-picker";
import { DEFAULT_ANALYSIS_SCOPE } from "@/lib/analysis-types";
import type { ScheduleRow } from "@/lib/types";
interface ScheduleData { rows: ScheduleRow[]; calendar: { is_trading_day: boolean; is_trading_session: boolean }; next_runs: Record<string,string|null> }
const definitions = [
  { kind: "daily_scan", title: "每日三策略筛选", description: "指定时刻筛选全市场并更新三个指标池，同日只触发一次。" },
  { kind: "monitor_cycle", title: "盘中间隔分析", description: "交易时段每隔 N 分钟分析所选范围，每只股票生成五页签报告。" },
  { kind: "monitor_fixed", title: "固定时点分析", description: "独立于盘中间隔，可设置多个时点；超过五分钟的延迟会记录为错过。" },
] as const;
export default function SchedulePage() {
  const [data,setData]=useState<ScheduleData|null>(null);
  const [saving,setSaving]=useState(false);
  const [running,setRunning]=useState<string|null>(null);
  const [fixedText,setFixedText]=useState("");
  const load=async()=>{ const response=await fetch("/api/schedule"); if(!response.ok) throw new Error("加载定时任务失败"); const value=await response.json() as ScheduleData; setData(value); setFixedText(value.rows.find(row=>row.kind==="monitor_fixed")?.fixed_times.join(", ") ?? "15:20"); };
  useEffect(()=>{ const timer=setTimeout(()=>void load().catch(error=>toast.error(String(error))),0); return()=>clearTimeout(timer); },[]);
  const patch=(kind:string,patch:Partial<ScheduleRow>)=>setData(current=>current?{...current,rows:current.rows.map(row=>row.kind===kind?{...row,...patch}:row)}:current);
  const save=async()=>{ if(!data)return; setSaving(true); try { const rows=data.rows.map(row=>row.kind==="monitor_fixed"?{...row,fixed_times:fixedText.split(/[,，;；\s]+/).filter(Boolean)}:row); const response=await fetch("/api/schedule",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({rows})}); const result=await response.json(); if(!response.ok)throw new Error(result.error); setData(result); toast.success("定时设置已保存"); }catch(error){toast.error(String(error));}finally{setSaving(false);} };
  const run=async(row:ScheduleRow)=>{setRunning(row.kind);try {const response=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kind:row.kind==="daily_scan"?"daily-scan":"monitor-once",scope:row.scope,notify:true})});const result=await response.json();if(!response.ok)throw new Error(result.error);toast.success(`任务 #${result.jobId} 已启动`);}catch(error){toast.error(String(error));}finally{setRunning(null);} };
  return <div className="max-w-5xl space-y-4"><div className="flex items-center justify-between"><div><h1 className="text-xl font-semibold">定时任务</h1><p className="mt-1 text-sm text-muted-foreground">所有时刻均为北京时间 · {data?.calendar.is_trading_day?"今天是交易日":"以交易日历为准"}</p></div><Button onClick={()=>void save()} disabled={saving||!data}>{saving?"保存中…":"保存设置"}</Button></div>
    {definitions.map(definition=>{const row=data?.rows.find(row=>row.kind===definition.kind);return <section key={definition.kind} className="space-y-4 rounded-lg border bg-card p-4"><div className="flex items-center justify-between gap-3"><div><h2 className="font-semibold">{definition.title}</h2><p className="mt-1 text-sm text-muted-foreground">{definition.description}</p></div><div className="flex items-center gap-2"><Label htmlFor={`enable-${definition.kind}`}>启用</Label><Switch id={`enable-${definition.kind}`} disabled={!row||saving} checked={row?.enabled??false} onCheckedChange={enabled=>patch(definition.kind,{enabled})}/></div></div>
    <div className="flex flex-wrap items-end gap-4">{definition.kind==="daily_scan"?<div><Label htmlFor="daily-time">执行时刻</Label><Input id="daily-time" type="time" className="w-36" value={row?.time??"15:20"} onChange={event=>patch(definition.kind,{time:event.target.value})}/></div>:definition.kind==="monitor_cycle"?<div><Label htmlFor="interval-minutes">间隔（分钟）</Label><Input id="interval-minutes" className="w-36" type="number" min="1" max="1440" value={(row?.interval_seconds??300)/60} onChange={event=>patch(definition.kind,{interval_seconds:Number(event.target.value)*60})}/></div>:<div className="min-w-72"><Label htmlFor="fixed-times">固定时点（逗号分隔）</Label><Input id="fixed-times" value={fixedText} onChange={event=>setFixedText(event.target.value)} placeholder="10:30, 14:30, 15:20"/></div>}
    <label className="flex h-9 items-center gap-2 text-sm"><input type="checkbox" checked={row?.trading_days_only??true} onChange={event=>patch(definition.kind,{trading_days_only:event.target.checked})}/>仅交易日</label><Button variant="outline" disabled={!row||running!==null} onClick={()=>row&&void run(row)}>{running===definition.kind?"启动中…":"执行一次"}</Button></div>
    {definition.kind!=="daily_scan"&&<AnalysisScopePicker value={row?.scope??DEFAULT_ANALYSIS_SCOPE} onChange={scope=>patch(definition.kind,{scope})}/>}
    <p className="border-t pt-3 text-xs text-muted-foreground">下次预计：{data?.next_runs[definition.kind]??"未启用"} · 实际执行以交易日历和任务队列为准</p></section>;})}
  </div>;
}
