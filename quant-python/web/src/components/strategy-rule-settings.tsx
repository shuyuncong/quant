"use client";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
const fields = [
  ["risk.stop_loss_pct","固定止损比例",.08], ["risk.stop_profit_pct","固定止盈比例",.30],
  ["backtest.chan_zero_axis.max_holding_bars","最多持有日线数",40],
  ["macd_divergence.min_volume_ratio","底背离最低量比",1.5], ["macd_divergence.volume_window","底背离均量窗口",20],
  ["macd_divergence.long_ma_slope_window","底背离年线斜率窗口",20], ["macd_divergence.min_macd_segment_bars","最短 MACD 柱区间",2],
] as const;
function at(value:unknown,path:string):unknown {return path.split('.').reduce<unknown>((item,key)=>item&&typeof item==='object'?(item as Record<string,unknown>)[key]:undefined,value);}
export function StrategyRuleSettings(){
  const [values,setValues]=useState<Record<string,string>>({});const [enabled,setEnabled]=useState(false);const [mode,setMode]=useState("");const [ready,setReady]=useState(false);const [saving,setSaving]=useState(false);
  useEffect(()=>{const abort=new AbortController();void fetch('/api/config',{signal:abort.signal}).then(async response=>{const data=await response.json();if(!response.ok)throw new Error(data.error);setValues(Object.fromEntries(fields.map(([path,,fallback])=>[path,String(at(data.config,path)??fallback)])));setEnabled(at(data.config,'macd_divergence.enabled')===true);setMode(JSON.stringify(at(data.config,'backtest.exit_rules')??{mode:'fixed'}));setReady(true);}).catch(error=>{if(!abort.signal.aborted)toast.error(String(error));});return()=>abort.abort();},[]);
  async function save(){setSaving(true);try{const response=await fetch('/api/config/strategies',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({...Object.fromEntries(Object.entries(values).map(([key,value])=>[key,Number(value)])),"macd_divergence.enabled":enabled})});const data=await response.json();if(!response.ok)throw new Error(data.errors?.join('；')??data.error);toast.success('规则参数已保存，新任务生效');}catch(error){toast.error(String(error));}finally{setSaving(false);}}
  return <section className="space-y-4 rounded-lg border p-4"><div><h2 className="font-semibold">三策略规则与退出参数</h2><p className="mt-1 text-sm text-muted-foreground">规则版本 2026-10-05.1。年线使用 MA250 回踩六条件，六个入场条件固定在代码中，不通过本页调整。本页的 MACD 参数、底背离参数与止损止盈比例用于个股分析、扫描和三策略回测；任务使用提交时的规则快照，修改配置不追溯已完成报告。</p></div><div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">{fields.map(([key,label])=><label key={key} className="grid gap-1 text-sm">{label}<Input aria-label={label} type="number" step="any" disabled={!ready||saving} value={values[key]??''} onChange={event=>setValues({...values,[key]:event.target.value})}/></label>)}</div><label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={!ready||saving} checked={enabled} onChange={event=>setEnabled(event.target.checked)}/>启用零轴＋底背离研究池</label><p className="text-xs text-muted-foreground">止损止盈比例填小数，如 0.08 表示 8%。退出作用范围沿用当前配置：<code className="break-all">{mode}</code>。研究池启用不会扩展交易信号通知。</p><Button disabled={!ready||saving} onClick={()=>void save()}>{saving?'保存中…':'保存规则参数'}</Button></section>;
}
