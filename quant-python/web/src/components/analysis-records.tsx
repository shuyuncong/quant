"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { FiveTabAnalysis } from "./five-tab-analysis";
import type { AnalysisRecord } from "@/lib/analysis-types";
function getConsensusBadge(record: AnalysisRecord) {
  const strategies = record.document?.strategies;
  if (!Array.isArray(strategies) || strategies.length === 0) return null;

  const buyCount = strategies.filter((s) => s.buy).length;
  const sellCount = strategies.filter((s) => s.sell).length;

  if (sellCount > 0) {
    return (
      <span className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-600 dark:text-emerald-400">
        建议离场 ({sellCount}策略)
      </span>
    );
  }
  if (buyCount >= 2) {
    return (
      <span className="rounded-full bg-rose-500/10 px-2 py-0.5 text-[11px] font-medium text-rose-600 dark:text-rose-400">
        买入共振 ({buyCount}策略)
      </span>
    );
  }
  if (buyCount === 1) {
    return (
      <span className="rounded-full bg-rose-500/10 px-2 py-0.5 text-[11px] font-medium text-rose-600 dark:text-rose-400">
        买点关注
      </span>
    );
  }
  return (
    <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
      观望持有
    </span>
  );
}

export function AnalysisRecords({ jobId, fallback }: { jobId?: number; fallback?: React.ReactNode }) {
  const [records, setRecords] = useState<AnalysisRecord[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [symbol, setSymbol] = useState("");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
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
      setRecords((prev) => {
        if (
          prev.length === result.records.length &&
          prev.every((p: AnalysisRecord, i: number) => {
            const n = result.records[i];
            return n && p.id === n.id && p.updated_at === n.updated_at;
          })
        ) {
          return prev;
        }
        return result.records;
      });
      setTotal((prev) => (prev === result.total ? prev : result.total));
      setError("");
      setLoaded(true);
      // 在弹窗中（有 jobId）默认打开首个记录；在首页大列表（!jobId）保持折叠防止撑爆页面
      setOpen((current) => {
        if (jobId) {
          if (current !== null && result.records.some((record: AnalysisRecord) => record.id === current)) {
            return current;
          }
          return result.records[0]?.id ?? null;
        }
        return current !== null && result.records.some((record: AnalysisRecord) => record.id === current)
          ? current
          : null;
      });
    } catch (err) {
      if (version === requestVersion.current) setError(err instanceof Error ? err.message : "读取失败");
    }
  }, [page, jobId, query]);

  const handleManualRefresh = async () => {
    setRefreshing(true);
    try {
      await load();
      toast.success("个股分析记录已刷新");
    } finally {
      setRefreshing(false);
    }
  };

  // 生产/开发浏览器中 10 分钟静默轮询一次，测试环境维持 5 秒
  const pollInterval = process.env.NODE_ENV === "test" ? 5000 : 10 * 60 * 1000;

  useEffect(() => {
    // requestVersion is a monotonically increasing request token, not a DOM ref.
    const invalidate = () => { ++requestVersion.current; };
    let active = true;
    const timer = setTimeout(() => { if (active) void load(); }, 0);
    const poll = setInterval(() => {
      if (active && (typeof document === "undefined" || document.visibilityState !== "hidden")) {
        void load();
      }
    }, pollInterval);
    return () => {
      active = false;
      invalidate();
      clearTimeout(timer);
      clearInterval(poll);
    };
  }, [load, pollInterval]);

  const retry = async (id: number) => {
    const response = await fetch(`/api/analyses/${id}/retry`, { method: "POST" });
    if (!response.ok) { toast.error("重试启动失败"); return; }
    toast.success("正在重试未完成阶段"); void load();
  };

  if (loaded && !total && fallback) return <>{fallback}</>;

  // 单个股票在弹窗内直接平铺展示，免去不必要的额外点击展开
  if (jobId && records.length === 1) {
    const record = records[0];
    const isSuccess = record.status === "success";
    const isPartialFailed = record.status === "partial_failed";

    return (
      <section className="space-y-4" aria-label="个股分析记录">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-card p-3 shadow-xs">
          <div className="flex items-center gap-2.5">
            <span className="text-base font-semibold">
              {record.name || record.symbol}
            </span>
            {record.name && (
              <span className="font-mono text-xs text-muted-foreground">
                {record.symbol}
              </span>
            )}
            <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
              任务 #{record.job_id}
            </span>
            {getConsensusBadge(record)}
          </div>

          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span>{record.created_at}</span>
            <span
              className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                isSuccess
                  ? "bg-rose-500/10 text-rose-600 dark:text-rose-400"
                  : isPartialFailed
                  ? "bg-amber-500/10 text-amber-600 dark:text-amber-400"
                  : "bg-muted text-muted-foreground"
              }`}
            >
              {isSuccess
                ? "已完成"
                : isPartialFailed
                ? "部分阶段失败"
                : "生成中"}
            </span>
            {record.status !== "success" && (
              <Button
                size="sm"
                variant="outline"
                className="h-7 text-xs"
                onClick={() => void retry(record.id)}
              >
                重试未完成阶段
              </Button>
            )}
          </div>
        </div>

        <FiveTabAnalysis
          document={record.document}
          onRetry={() => void retry(record.id)}
        />
      </section>
    );
  }

  return (
    <section className="space-y-3" aria-label="个股分析记录">
      {!jobId && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-base font-semibold">个股分析记录</h2>
            <p className="text-xs text-muted-foreground">按股票查看历次分析报告；一次任务可生成多条记录。</p>
          </div>
          <div className="flex items-center gap-2">
            <form
              className="flex gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                setQuery(symbol.trim());
                setPage(1);
                setOpen(null);
              }}
            >
              <Input
                aria-label="按股票代码查询报告"
                className="w-40"
                placeholder="股票代码"
                value={symbol}
                onChange={(event) => setSymbol(event.target.value)}
              />
              <Button type="submit" variant="outline">查询</Button>
            </form>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-9 gap-1.5 text-xs text-muted-foreground hover:text-foreground"
              onClick={() => void handleManualRefresh()}
              disabled={refreshing}
            >
              <RefreshCw className={cn("size-3.5", refreshing && "animate-spin")} />
              {refreshing ? "刷新中..." : "手动刷新"}
            </Button>
          </div>
        </div>
      )}

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {!loaded ? (
        <div className="space-y-2.5">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="flex items-center justify-between rounded-xl border bg-card p-3.5 shadow-xs"
            >
              <div className="flex items-center gap-3">
                <Skeleton className="h-5 w-20" />
                <Skeleton className="h-4 w-16" />
                <Skeleton className="h-4 w-14 rounded-full" />
              </div>
              <div className="flex items-center gap-3">
                <Skeleton className="h-4 w-28" />
                <Skeleton className="h-5 w-12 rounded-full" />
                <Skeleton className="h-4 w-8" />
              </div>
            </div>
          ))}
        </div>
      ) : !records.length ? (
        <div className="rounded-xl border bg-card p-8 text-center text-sm text-muted-foreground shadow-xs">
          暂无个股报告。运行分析后，每只股票会生成一条包含五个页签的完整记录。
        </div>
      ) : null}

      <div className="space-y-2.5">
        {records.map((record) => {
          const isOpen = open === record.id;
          const isSuccess = record.status === "success";
          const isPartialFailed = record.status === "partial_failed";

          return (
            <article
              key={record.id}
              className="rounded-xl border bg-card shadow-xs transition-shadow hover:shadow-sm"
            >
              <button
                type="button"
                className="flex w-full flex-wrap items-center justify-between gap-3 p-3.5 text-left transition-colors hover:bg-muted/30"
                aria-expanded={isOpen}
                onClick={() => setOpen(isOpen ? null : record.id)}
              >
                <div className="flex items-center gap-2.5">
                  <span className="font-semibold text-sm">
                    {record.name || record.symbol}
                  </span>
                  {record.name && (
                    <span className="font-mono text-xs text-muted-foreground">
                      {record.symbol}
                    </span>
                  )}
                  <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
                    任务 #{record.job_id}
                  </span>
                  {getConsensusBadge(record)}
                </div>

                <div className="flex items-center gap-3 text-xs text-muted-foreground">
                  <span>{record.created_at}</span>
                  <span
                    className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                      isSuccess
                        ? "bg-rose-500/10 text-rose-600 dark:text-rose-400"
                        : isPartialFailed
                        ? "bg-amber-500/10 text-amber-600 dark:text-amber-400"
                        : "bg-muted text-muted-foreground"
                    }`}
                  >
                    {isSuccess
                      ? "已完成"
                      : isPartialFailed
                      ? "部分阶段失败"
                      : "生成中"}
                  </span>
                  <span className="text-xs font-medium text-primary">
                    {isOpen ? "收起" : "展开"}
                  </span>
                </div>
              </button>

              {isOpen && (
                <div className="border-t p-4 bg-background/50">
                  <FiveTabAnalysis
                    document={record.document}
                    onRetry={() => void retry(record.id)}
                  />
                </div>
              )}
            </article>
          );
        })}
      </div>

      {(!jobId || total > 5) && (
        <div className="flex items-center justify-between text-xs text-muted-foreground pt-1">
          <span>共 {total} 条 · 每页 5 条</span>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={page === 1}
              onClick={() => {
                setPage(page - 1);
                setOpen(null);
              }}
              className="h-8 text-xs"
            >
              上一页
            </Button>
            <span className="px-1 font-mono">
              {page} / {Math.max(1, Math.ceil(total / 5))}
            </span>
            <Button
              size="sm"
              variant="outline"
              disabled={page * 5 >= total}
              onClick={() => {
                setPage(page + 1);
                setOpen(null);
              }}
              className="h-8 text-xs"
            >
              下一页
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
