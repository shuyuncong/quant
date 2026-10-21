"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Loader2, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { FiveTabAnalysis } from "./five-tab-analysis";
import { STRATEGIES, ANALYSIS_PAGE_SIZE, type AnalysisRecordSummary, type StrategyId } from "@/lib/analysis-types";

function getConsensusBadge(record: AnalysisRecordSummary) {
  const strategies = record.document?.strategies;
  if (!Array.isArray(strategies) || strategies.length === 0) return null;

  const buyStrategies = strategies.filter((s) => s.buy);
  const buyCount = buyStrategies.length;
  const buyNames = buyStrategies.map((s) => s.name || s.strategy_id).join("、");
  const sellStrategies = strategies.filter((s) => s.sell);
  const sellCount = sellStrategies.length;
  const sellNames = sellStrategies.map((s) => s.name || s.strategy_id).join("、");

  if (sellCount > 0) {
    if (record.held === true) {
      return (
        <span
          className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-600 dark:text-emerald-400"
          title={`触发策略：${sellNames}`}
        >
          建议离场 · {sellNames}
        </span>
      );
    }
    return (
      <span
        className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-400"
        title={
          record.held === false
            ? `分析时点未持有该股票；退出条件仅作为建仓后的规则参考。触发策略：${sellNames}`
            : `记录缺少持仓快照，无法判断是否持仓。触发策略：${sellNames}`
        }
      >
        {record.held === false ? "退出条件触发 · 未持仓" : "退出条件触发 · 持仓未知"} · {sellNames}
      </span>
    );
  }
  if (buyCount >= 2) {
    return (
      <span
        className="rounded-full bg-rose-500/10 px-2 py-0.5 text-[11px] font-medium text-rose-600 dark:text-rose-400"
        title={`触发策略：${buyNames}`}
      >
        买入共振 · {buyNames}
      </span>
    );
  }
  if (buyCount === 1) {
    return (
      <span
        className="rounded-full bg-rose-500/10 px-2 py-0.5 text-[11px] font-medium text-rose-600 dark:text-rose-400"
        title={`触发策略：${buyNames}`}
      >
        买点 · {buyNames}
      </span>
    );
  }
  return (
    <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
      观望持有
    </span>
  );
}

export function AnalysisRecords({
  jobId,
  fallback,
  embedded = false,
}: {
  jobId?: number;
  fallback?: React.ReactNode;
  /** 嵌在页签里时由外层页签承担标题，避免重复一条「个股分析记录」。 */
  embedded?: boolean;
}) {
  const [records, setRecords] = useState<AnalysisRecordSummary[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [symbol, setSymbol] = useState("");
  const [query, setQuery] = useState("");
  const [strategy, setStrategy] = useState<StrategyId | "">("");
  const [signal, setSignal] = useState<"buy" | "sell" | "">("");
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const requestVersion = useRef(0);
  /**
   * 同步的"请求进行中"标记。
   *
   * `fetching` 是 state：同一 tick 内连点两次时，DOM 上的 disabled 还没更新，
   * 两次点击都会进入处理器，函数式更新各推进一页 → 用户点一下却跳到第 3 页。
   * 这个 ref 在点击瞬间上锁，把同一 tick 的第二次点击挡掉。
   */
  const requestInFlight = useRef(false);
  const listTop = useRef<HTMLElement | null>(null);
  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    const search = new URLSearchParams({ page: String(page), symbol: query });
    if (jobId) search.set("job_id", String(jobId));
    if (strategy) search.set("strategy", strategy);
    if (signal) search.set("signal", signal);
    requestInFlight.current = true;
    setFetching(true);
    try {
      const response = await fetch(`/api/analyses?${search}`);
      // 先解析文本再试 JSON：接口异常时响应体可能是 "Internal Server Error" 这类文本，
      // 直接 response.json() 只会抛出 "Unexpected token 'I'..." —— 看不到真实原因。
      const raw = await response.text();
      let result: { error?: string; records?: AnalysisRecordSummary[]; total?: number } = {};
      try {
        result = raw ? JSON.parse(raw) : {};
      } catch {
        if (version !== requestVersion.current) return;
        throw new Error(
          response.ok
            ? "分析记录接口返回了非 JSON 内容，请查看服务端日志"
            : `分析记录接口失败（HTTP ${response.status}）：${raw.slice(0, 200) || "响应体为空"}`,
        );
      }
      if (version !== requestVersion.current) return;
      if (!response.ok) throw new Error(result.error ?? `读取分析记录失败（HTTP ${response.status}）`);
      const incoming = result.records;
      if (!Array.isArray(incoming)) throw new Error("分析记录接口返回的数据缺少 records 字段");
      setRecords((prev) => {
        if (
          prev.length === incoming.length &&
          prev.every((p: AnalysisRecordSummary, i: number) => {
            const n = incoming[i];
            return n && p.id === n.id && p.updated_at === n.updated_at;
          })
        ) {
          return prev;
        }
        return incoming;
      });
      const incomingTotal = typeof result.total === "number" ? result.total : incoming.length;
      setTotal((prev) => (prev === incomingTotal ? prev : incomingTotal));
      setError("");
      setLoaded(true);
      // 在弹窗中（有 jobId）默认打开首个记录；在首页大列表（!jobId）保持折叠防止撑爆页面
      setOpen((current) => {
        if (jobId) {
          if (current !== null && incoming.some((record: AnalysisRecordSummary) => record.id === current)) {
            return current;
          }
          return incoming[0]?.id ?? null;
        }
        return current !== null && incoming.some((record: AnalysisRecordSummary) => record.id === current)
          ? current
          : null;
      });
    } catch (err) {
      if (version === requestVersion.current) setError(err instanceof Error ? err.message : "读取失败");
    } finally {
      // 无条件释放：若按 version 判定，被取代的请求会留下永久锁定，翻页就再也点不动。
      requestInFlight.current = false;
      if (version === requestVersion.current) setFetching(false);
    }
  }, [page, jobId, query, strategy, signal]);

  /**
   * 翻页：收起展开项、滚回列表顶部，并推进一页。
   *
   * 进入前先查同步锁——同一 tick 内连点两次时 DOM 的 disabled 尚未更新，
   * 两次点击都会到达这里，各推进一页，用户点一下会跳到第 3 页。
   */
  const goToPage = useCallback((delta: 1 | -1) => {
    if (requestInFlight.current) return;
    requestInFlight.current = true;
    setOpen(null);
    setPage((current) => Math.max(1, current + delta));
    // jsdom 等无布局环境没有 scrollIntoView；浏览器里始终存在。
    listTop.current?.scrollIntoView?.({ block: "start", behavior: "auto" });
  }, []);

  const filterByStrategy = (value: StrategyId | "") => {
    setStrategy(value);
    setPage(1);
    setOpen(null);
  };

  const filterBySignal = (value: "buy" | "sell" | "") => {
    setSignal(value);
    setPage(1);
    setOpen(null);
  };

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
          held={record.held}
          onRetry={() => void retry(record.id)}
        />
      </section>
    );
  }

  return (
    <section ref={listTop} className="space-y-3" aria-label="个股分析记录">
      {!jobId && (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            {!embedded && (
              <div>
                <h2 className="text-base font-semibold">个股分析记录</h2>
                <p className="text-xs text-muted-foreground">按股票查看历次分析报告；一次任务可生成多条记录。</p>
              </div>
            )}
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

          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <div
              className="flex flex-wrap items-center gap-1.5"
              role="group"
              aria-label="按买卖信号筛选"
            >
              <span className="mr-1 text-xs text-muted-foreground">信号</span>
              <Button
                type="button"
                size="sm"
                variant={signal === "" ? "default" : "outline"}
                className="h-7 px-2 text-xs"
                aria-pressed={signal === ""}
                onClick={() => filterBySignal("")}
              >
                全部
              </Button>
              <Button
                type="button"
                size="sm"
                variant={signal === "buy" ? "default" : "outline"}
                className="h-7 px-2 text-xs"
                aria-pressed={signal === "buy"}
                onClick={() => filterBySignal("buy")}
              >
                买点
              </Button>
              <Button
                type="button"
                size="sm"
                variant={signal === "sell" ? "default" : "outline"}
                className="h-7 px-2 text-xs"
                aria-pressed={signal === "sell"}
                onClick={() => filterBySignal("sell")}
              >
                卖点
              </Button>
            </div>

            <div
              className="flex flex-wrap items-center gap-1.5"
              role="group"
              aria-label="按策略筛选"
            >
              <span className="mr-1 text-xs text-muted-foreground">策略</span>
              <Button
                type="button"
                size="sm"
                variant={strategy === "" ? "default" : "outline"}
                className="h-7 px-2 text-xs"
                aria-pressed={strategy === ""}
                onClick={() => filterByStrategy("")}
              >
                全部
              </Button>
              {STRATEGIES.map((item) => (
                <Button
                  key={item.id}
                  type="button"
                  size="sm"
                  variant={strategy === item.id ? "default" : "outline"}
                  className="h-7 px-2 text-xs"
                  aria-pressed={strategy === item.id}
                  onClick={() => filterByStrategy(item.id)}
                >
                  {item.name}
                </Button>
              ))}
            </div>
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
          {signal || strategy
            ? "当前筛选条件下没有记录。"
            : "暂无个股报告。运行分析后，每只股票会生成一条包含五个页签的完整记录。"}
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
                    held={record.held}
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
          <span className="flex items-center gap-1.5">
            共 {total} 条 · 每页 {ANALYSIS_PAGE_SIZE} 条
            {fetching && (
              <span role="status" className="inline-flex items-center gap-1 text-primary">
                <Loader2 className="size-3 animate-spin" />
                读取中
              </span>
            )}
          </span>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={page === 1 || fetching}
              onClick={() => goToPage(-1)}
              className="h-8 text-xs"
            >
              上一页
            </Button>
            <span className="px-1 font-mono">
              {page} / {Math.max(1, Math.ceil(total / ANALYSIS_PAGE_SIZE))}
            </span>
            <Button
              size="sm"
              variant="outline"
              disabled={page * ANALYSIS_PAGE_SIZE >= total || fetching}
              onClick={() => goToPage(1)}
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
