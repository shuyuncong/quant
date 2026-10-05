"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FiveTabAnalysis } from "./five-tab-analysis";
import type { AnalysisRecord } from "@/lib/analysis-types";
export function AnalysisRecords({ jobId, fallback }: { jobId?: number; fallback?: React.ReactNode }) {
  const [records, setRecords] = useState<AnalysisRecord[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [symbol, setSymbol] = useState("");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const requestVersion = useRef(0);
  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    const search = new URLSearchParams({ page: String(page), symbol: query });
    if (jobId) search.set("job_id", String(jobId));
    try {
      const response = await fetch(`/api/analyses?${search}`);
      const result = await response.json();
      if (version !== requestVersion.current) return;
      if (!response.ok) throw new Error(result.error ?? "读取分析记录失败");
      setRecords(result.records); setTotal(result.total); setError(""); setLoaded(true);
      setOpen(current => current ?? result.records[0]?.id ?? null);
    } catch (err) { if (version === requestVersion.current) setError(err instanceof Error ? err.message : "读取失败"); }
  }, [page, jobId, query]);
  useEffect(() => {
    // requestVersion is a monotonically increasing request token, not a DOM ref.
    const invalidate = () => { ++requestVersion.current; };
    let active = true; const timer = setTimeout(() => { if (active) void load(); }, 0); const poll = setInterval(() => { if (active) void load(); }, 5000); return () => { active = false; invalidate(); clearTimeout(timer); clearInterval(poll); }; }, [load]);
  const retry = async (id: number) => {
    const response = await fetch(`/api/analyses/${id}/retry`, { method: "POST" });
    if (!response.ok) { toast.error("重试启动失败"); return; }
    toast.success("正在重试未完成阶段"); void load();
  };
  if (loaded && !total && fallback) return <>{fallback}</>;
  return <section className="space-y-3" aria-label="个股分析记录">
    {!jobId && <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-base font-semibold">个股分析记录</h2><form className="flex gap-2" onSubmit={event => { event.preventDefault(); setQuery(symbol.trim()); setPage(1); }}><Input aria-label="按股票代码查询报告" className="w-40" placeholder="股票代码" value={symbol} onChange={event => setSymbol(event.target.value)} /><Button type="submit" variant="outline">查询</Button></form></div>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {!records.length && <p className="rounded border p-6 text-sm text-muted-foreground">{loaded ? "暂无个股报告。运行分析后，每只股票会生成一条包含五个页签的记录。" : "正在加载分析记录…"}</p>}
    {records.map(record => <article key={record.id} className="rounded-lg border bg-card"><button type="button" className="flex w-full flex-wrap items-center justify-between gap-2 p-3 text-left hover:bg-muted/40" aria-expanded={open === record.id} onClick={() => setOpen(open === record.id ? null : record.id)}><span className="font-medium">{record.name || record.symbol} <span className="text-xs font-normal text-muted-foreground">{record.name ? record.symbol : ""} · 任务 #{record.job_id}</span></span><span className="text-xs text-muted-foreground">{record.created_at} · {record.status === "success" ? "已完成" : record.status === "partial_failed" ? "部分阶段失败" : "生成中"} · {open === record.id ? "收起" : "展开"}</span></button>{open === record.id && <div className="border-t p-4"><FiveTabAnalysis document={record.document} onRetry={() => void retry(record.id)} /></div>}</article>)}
    <div className="flex items-center justify-between text-xs text-muted-foreground"><span>共 {total} 条 · 每页 5 条</span><div className="flex items-center gap-3"><Button size="sm" variant="outline" disabled={page === 1} onClick={() => { setPage(page-1); setOpen(null); }}>上一页</Button><span>{page} / {Math.max(1, Math.ceil(total/5))}</span><Button size="sm" variant="outline" disabled={page*5 >= total} onClick={() => { setPage(page+1); setOpen(null); }}>下一页</Button></div></div>
  </section>;
}
