"use client";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "./ui/button";
export function ModelPurposeSettings({models}:{models:{id:number;name:string;enabled:boolean}[]}) {
  const [values,setValues]=useState({technical:0,synthesis:0});
  const [ready,setReady]=useState(false);
  const [saving,setSaving]=useState(false);
  useEffect(()=>{const abort=new AbortController();void fetch("/api/models/purposes",{signal:abort.signal}).then(async response=>{if(!response.ok)throw new Error("加载用途设置失败");setValues(await response.json());setReady(true);}).catch(error=>{if(!abort.signal.aborted)toast.error(String(error));});return()=>abort.abort();},[]);
  async function save(){setSaving(true);try{const response=await fetch("/api/models/purposes",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(values)});const data=await response.json();if(!response.ok)throw new Error(data.error);toast.success("模型用途已保存");}catch(error){toast.error(String(error));}finally{setSaving(false);}}
  return <section className="space-y-3 rounded-lg border p-4"><div><h2 className="font-semibold">分析模型用途</h2><p className="text-sm text-muted-foreground">每只股票调用技术分析与综合归纳各一次。首选失败时继续使用现有模型降级链。</p></div><div className="flex flex-wrap items-end gap-4">{([['technical','AI 技术分析'],['synthesis','综合结论']] as const).map(([key,label])=><label key={key} className="grid gap-1 text-sm">{label}<select className="h-9 min-w-48 rounded border bg-background px-3" disabled={!ready||saving} value={values[key]} onChange={event=>setValues({...values,[key]:Number(event.target.value)})}><option value={0}>自动（按模型排序）</option>{models.filter(model=>model.enabled).map(model=><option key={model.id} value={model.id}>{model.name}</option>)}</select></label>)}<Button disabled={!ready||saving} onClick={()=>void save()}>{saving?"保存中…":"保存用途"}</Button></div></section>;
}
