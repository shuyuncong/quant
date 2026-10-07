"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Check, Copy, Database, Download, Eye, Loader2, Play, RefreshCw, Sparkles, X } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { MarkdownContent } from "@/components/markdown-content";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { SymbolCombobox } from "@/components/symbol-combobox";
import { StockReportLoader } from "@/components/stock-analysis-report";
import { type DataBar, type DataTimeframe, type DataResult, type DataSource } from "@/lib/stock-report";
import "./report.css";
import { AnalysisRecords } from "@/components/analysis-records";
import { AnalysisScopePicker } from "@/components/analysis-scope-picker";
import { DEFAULT_ANALYSIS_SCOPE } from "@/lib/analysis-types";

interface NoteRow {
  id: number;
  content: string;
  model: string;
  created_at: string;
}

interface JobRow {
  id: number;
  kind: string;
  status: string;
  payload: Record<string, unknown>;
  symbol_names: string;
  result_path: string | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  note: NoteRow | null;
}
const KIND_LABEL: Record<string, string> = {
  analyze: "个股分析",
  scan: "日线扫描",
  "daily-scan": "每日扫描",
  "monitor-once": "盘中监控",
  "monitor-cycle": "定时监控",
  "test-notify": "测试通知",
  "dispatch-outbox": "补投队列",
};

const STATUS_LABEL: Record<string, string> = {
  success: "成功",
  running: "运行中",
  pending: "等待中",
  failed: "失败",
};

const STATUS_VARIANT: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  success: "default",
  running: "secondary",
  pending: "outline",
  failed: "destructive",
};

function JobStatusBadge({ status }: { status: string }) {
  if (status === "running") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-0.5 text-xs font-medium text-emerald-600 dark:text-emerald-400">
        <span className="relative flex size-2">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75"></span>
          <span className="relative inline-flex size-2 rounded-full bg-emerald-500"></span>
        </span>
        运行中
      </span>
    );
  }
  if (status === "pending") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/30 bg-amber-500/10 px-2.5 py-0.5 text-xs font-medium text-amber-600 dark:text-amber-400">
        <Loader2 className="size-3 animate-spin opacity-80" />
        等待中
      </span>
    );
  }
  if (status === "success") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-border bg-muted/40 px-2 py-0.5 text-xs text-muted-foreground">
        <Check className="size-3 text-emerald-500" />
        成功
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-destructive/30 bg-destructive/10 px-2.5 py-0.5 text-xs font-medium text-destructive">
        <X className="size-3" />
        失败
      </span>
    );
  }
  return (
    <Badge variant={STATUS_VARIANT[status] ?? "outline"}>
      {STATUS_LABEL[status] ?? status}
    </Badge>
  );
}

function fileName(resultPath: string | null): string {
  if (!resultPath) return "-";
  return resultPath.split(/[\\/]/).pop() ?? resultPath;
}

async function postJson(url: string, body: unknown): Promise<Record<string, unknown>> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const parsed: unknown = await response.json().catch(() => ({}));
  // 边界：服务端 JSON 形状不由前端保证，先收窄成对象，字段用 typeof 读取。
  const data =
    parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : {};
  const error = typeof data.error === "string" ? data.error : "";
  if (!response.ok) throw new Error(error || `请求失败: ${response.status}`);
  return data;
}

function formatNumber(value: number | null): string {
  if (value === null || value === undefined) return "-";
  const text = value.toFixed(4);
  return text.replace(/0+$/, "").replace(/\.$/, "");
}

function BarTable({ bars }: { bars: DataBar[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>时间</TableHead>
            <TableHead>开盘</TableHead>
            <TableHead>最高</TableHead>
            <TableHead>最低</TableHead>
            <TableHead>收盘</TableHead>
            <TableHead>成交量</TableHead>
            <TableHead>DIF</TableHead>
            <TableHead>DEA</TableHead>
            <TableHead>HIST</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {bars.map((bar) => (
            <TableRow key={bar.datetime}>
              <TableCell className="font-mono text-xs">{bar.datetime}</TableCell>
              <TableCell className="text-xs tabular-nums">{formatNumber(bar.open)}</TableCell>
              <TableCell className="text-xs tabular-nums">{formatNumber(bar.high)}</TableCell>
              <TableCell className="text-xs tabular-nums">{formatNumber(bar.low)}</TableCell>
              <TableCell className="text-xs tabular-nums">{formatNumber(bar.close)}</TableCell>
              <TableCell className="text-xs tabular-nums">{formatNumber(bar.volume)}</TableCell>
              <TableCell className="text-xs tabular-nums">{formatNumber(bar.dif)}</TableCell>
              <TableCell className="text-xs tabular-nums">{formatNumber(bar.dea)}</TableCell>
              <TableCell className="text-xs tabular-nums">{formatNumber(bar.hist)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function TimeframeBlock({ timeframe }: { timeframe: DataTimeframe }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="font-mono font-medium text-foreground">{timeframe.timeframe}</span>
        <Badge variant={timeframe.status === "ok" ? "secondary" : "destructive"}>
          {timeframe.status}
        </Badge>
        <span>最新 {timeframe.latest_time ?? "-"}</span>
        <span>收盘 {formatNumber(timeframe.latest_price)}</span>
        <span>K线 {timeframe.bar_count} 根</span>
        <span>买入分 {timeframe.buy_score ?? "-"}</span>
        <span>卖出分 {timeframe.sell_score ?? "-"}</span>
        {timeframe.error && <span className="text-destructive">{timeframe.error}</span>}
      </div>
      {timeframe.bars.length > 0 ? (
        <BarTable bars={timeframe.bars} />
      ) : (
        <p className="text-xs text-muted-foreground">该周期无K线数据</p>
      )}
    </div>
  );
}

function ResultBlock({ result }: { result: DataResult }) {
  if (result.timeframes.length === 0) {
    return <p className="text-sm text-muted-foreground">无周期数据</p>;
  }
  return (
    <div className="flex flex-col gap-3 rounded-lg border p-4">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">
          {result.name || result.symbol}/{result.symbol}
        </span>
        {result.status && <Badge variant="outline">{result.status}</Badge>}
        {result.analyzed_at && (
          <span className="text-xs text-muted-foreground">{result.analyzed_at}</span>
        )}
      </div>
      <Tabs defaultValue={result.timeframes[0].timeframe}>
        <TabsList className="flex-wrap">
          {result.timeframes.map((tf) => (
            <TabsTrigger key={tf.timeframe} value={tf.timeframe}>
              {tf.timeframe}
            </TabsTrigger>
          ))}
        </TabsList>
        {result.timeframes.map((tf) => (
          <TabsContent key={tf.timeframe} value={tf.timeframe}>
            <TimeframeBlock timeframe={tf} />
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}

function DataSourceView({ source }: { source: DataSource }) {
  const jsonText = JSON.stringify(source, null, 2);
  const [copied, setCopied] = useState(false);
  const copyJson = () => {
    void navigator.clipboard.writeText(jsonText).then(() => {
      setCopied(true);
      toast.success("JSON 已复制");
      setTimeout(() => setCopied(false), 2000);
    });
  };
  const downloadJson = () => {
    const blob = new Blob([jsonText], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `data-source-${(source.analyzed_at ?? "").replace(/[^\w.-]+/g, "-") || Date.now()}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };
  return (
    <Tabs defaultValue="data">
      <TabsList variant="line">
        <TabsTrigger value="data">数据</TabsTrigger>
        <TabsTrigger value="json">JSON</TabsTrigger>
      </TabsList>
      <TabsContent value="data" className="flex flex-col gap-4">
      {source.market_context && (
        <div className="rounded-lg border bg-muted/30 p-3 text-xs">
          <span className="font-medium text-foreground">市场环境</span>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
            <span>指数 {String(source.market_context.index_code ?? "-")}</span>
            <span>环境 {String(source.market_context.regime ?? "-")}</span>
            <span>允许开仓 {String(source.market_context.allows_entries ?? "-")}</span>
            <span>均线多头 {String(source.market_context.above_ma_long ?? "-")}</span>
          </div>
        </div>
      )}
      {source.results.length > 0 ? (
        source.results.map((result) => <ResultBlock key={result.symbol} result={result} />)
      ) : source.candidates.length > 0 ? (
        <div className="flex flex-col gap-2">
          <p className="text-sm text-muted-foreground">候选 {source.candidates.length} 只（最多展示 100 只）</p>
          <div className="overflow-x-auto rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>代码</TableHead>
                  <TableHead>名称</TableHead>
                  <TableHead>评分</TableHead>
                  <TableHead>价格</TableHead>
                  <TableHead>区域</TableHead>
                  <TableHead>确认时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {source.candidates.map((candidate) => (
                  <TableRow key={String(candidate.symbol ?? "")}>
                    <TableCell className="font-mono text-xs">{String(candidate.symbol ?? "")}</TableCell>
                    <TableCell className="text-xs">{String(candidate.name ?? "")}</TableCell>
                    <TableCell className="text-xs tabular-nums">{String(candidate.score ?? "")}</TableCell>
                    <TableCell className="text-xs tabular-nums">{String(candidate.price ?? "")}</TableCell>
                    <TableCell className="text-xs">{String(candidate.golden_cross_zone_label ?? "")}</TableCell>
                    <TableCell className="text-xs">{String(candidate.confirmed_at ?? "")}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">该报告没有可展示的数据源。</p>
      )}
      {source.errors.length > 0 && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive">
          <span className="font-medium">数据获取失败 {source.errors.length} 条：</span>
          <ul className="mt-1 list-inside list-disc">
            {source.errors.slice(0, 10).map((error, index) => (
              <li key={index}>
                {String((error as Record<string, unknown>).symbol ?? "")}{" "}
                {String((error as Record<string, unknown>).error ?? "")}
              </li>
            ))}
          </ul>
        </div>
      )}
      </TabsContent>
      <TabsContent value="json" className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={copyJson}>
            {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
            {copied ? "已复制" : "复制"}
          </Button>
          <Button variant="outline" size="sm" onClick={downloadJson}>
            <Download className="size-4" />
            下载 JSON
          </Button>
        </div>
        <pre className="max-h-[70vh] overflow-auto rounded-lg border bg-muted/30 p-3 font-mono text-xs leading-relaxed">
          {jsonText}
        </pre>
      </TabsContent>
    </Tabs>
  );
}

export default function ResultsPage() {
  const [scope, setScope] = useState(DEFAULT_ANALYSIS_SCOPE);
  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [jobsLoaded, setJobsLoaded] = useState(false);
  const [poolSymbols, setPoolSymbols] = useState<Array<{ symbol: string; name: string }>>([]);
  const [symbolsInput, setSymbolsInput] = useState("");
  const [notify, setNotify] = useState(true);
  const [selectedJob, setSelectedJob] = useState<JobRow | null>(null);
  const [interpreting, setInterpreting] = useState(false);
  const [dataJob, setDataJob] = useState<JobRow | null>(null);
  const [dataSource, setDataSource] = useState<DataSource | null>(null);
  const [dataLoading, setDataLoading] = useState(false);
  const [dataError, setDataError] = useState<string | null>(null);
  const [reportRetry, setReportRetry] = useState(0);
  const [startingKind, setStartingKind] = useState<string | null>(null);
  const dataRequest = useRef(0);
  // 连点保护：从点击到新任务出现在最近任务列表之前，启动按钮保持禁用（只靠轮询状态会留出空档）。
  const startGuard = useRef(false);
  const pendingJobId = useRef<number | null>(null);

  useEffect(() => {
    fetch("/api/pool")
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data?.pool && Array.isArray(data.pool) && data.pool.length > 0) {
          setPoolSymbols(
            data.pool.slice(0, 8).map((p: { symbol: string; name: string }) => ({
              symbol: p.symbol,
              name: p.name,
            }))
          );
        }
      })
      .catch(() => {});
  }, []);

  const DEFAULT_QUICK_SYMBOLS = [
    { symbol: "600036.SH", name: "招商银行" },
    { symbol: "000001.SZ", name: "平安银行" },
    { symbol: "600519.SH", name: "贵州茅台" },
    { symbol: "300750.SZ", name: "宁德时代" },
    { symbol: "002594.SZ", name: "比亚迪" },
  ];
  const quickSymbols = poolSymbols.length > 0 ? poolSymbols : DEFAULT_QUICK_SYMBOLS;

  const toggleQuickSymbol = (sym: string) => {
    const list = symbolsInput.split(/[\s,，;；]+/).filter(Boolean);
    if (list.includes(sym)) {
      setSymbolsInput(list.filter((s) => s !== sym).join(" "));
    } else {
      setSymbolsInput([...list, sym].join(" "));
    }
  };

  const releaseStart = useCallback(() => {
    startGuard.current = false;
    pendingJobId.current = null;
    setStartingKind(null);
  }, []);

  const loadJobs = useCallback(async () => {
    try {
      const response = await fetch("/api/jobs");
      if (!response.ok) return;
      const data = (await response.json()) as { jobs: JobRow[] };
      setJobs((prev) => {
        if (
          prev.length === data.jobs.length &&
          prev.every((p, i) => {
            const n = data.jobs[i];
            return (
              n &&
              p.id === n.id &&
              p.status === n.status &&
              p.finished_at === n.finished_at &&
              p.error === n.error &&
              Boolean(p.note) === Boolean(n.note)
            );
          })
        ) {
          return prev;
        }
        return data.jobs;
      });
      setJobsLoaded(true);
      // 刚启动的任务一旦出现在列表里，「启动中」锁交回给任务自身的运行状态。
      if (pendingJobId.current !== null && data.jobs.some((job) => job.id === pendingJobId.current)) {
        releaseStart();
      }
    } catch {
      /* ignore polling errors */
    }
  }, [releaseStart]);

  const openDataSource = useCallback(async (job: JobRow) => {
    const requestId = ++dataRequest.current;
    setDataJob(job);
    setDataSource(null);
    setDataError(null);
    setDataLoading(true);
    try {
      const response = await fetch(`/api/jobs/${job.id}/data`);
      const data = (await response.json().catch(() => ({}))) as DataSource & { error?: string };
      if (requestId !== dataRequest.current) return;
      if (!response.ok) {
        setDataError(data.error || `请求失败: ${response.status}`);
      } else {
        setDataSource(data);
      }
    } catch {
      if (requestId === dataRequest.current) setDataError("加载数据源失败");
    } finally {
      if (requestId === dataRequest.current) setDataLoading(false);
    }
  }, []);

  const [refreshingJobs, setRefreshingJobs] = useState(false);
  const handleManualRefresh = async () => {
    setRefreshingJobs(true);
    try {
      await loadJobs();
      toast.success("任务列表已刷新");
    } finally {
      setRefreshingJobs(false);
    }
  };

  const hasRunningJobs = jobs.some((job) => job.status === "running" || job.status === "pending");
  // 测试环境固定 5 秒；开发/生产环境：有运行中任务时 5 秒快速同步，平时 10 分钟静默刷新
  const jobsPollInterval =
    process.env.NODE_ENV === "test"
      ? 5000
      : hasRunningJobs
      ? 5000
      : 10 * 60 * 1000;

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadJobs();
  }, [loadJobs]);

  useEffect(() => {
    const timer = setInterval(() => {
      if (typeof document === "undefined" || document.visibilityState !== "hidden") {
        void loadJobs();
      }
    }, jobsPollInterval);
    return () => clearInterval(timer);
  }, [loadJobs, jobsPollInterval]);

  const runJob = useCallback(
    async (kind: string, extra: Record<string, unknown> = {}) => {
      if (startGuard.current) return;
      startGuard.current = true;
      setStartingKind(kind);
      try {
        const data = await postJson("/api/run", { kind, notify, ...extra });
        const jobId = typeof data.jobId === "number" ? data.jobId : null;
        if (jobId === null) {
          releaseStart();
          toast.error("启动失败：服务端未返回任务号");
          return;
        }
        toast.success(`任务已启动 #${jobId}`);
        pendingJobId.current = jobId;
        void loadJobs();
      } catch (error) {
        releaseStart();
        toast.error(error instanceof Error ? error.message : "启动失败");
      }
    },
    [notify, loadJobs, releaseStart]
  );

  const selectedJobView = selectedJob
    ? jobs.find((job) => job.id === selectedJob.id) ?? selectedJob
    : null;

  const interpret = useCallback(async () => {
    if (!selectedJobView) return;
    setInterpreting(true);
    try {
      const data = await postJson(`/api/jobs/${selectedJobView.id}/interpret`, {});
      toast.success("AI 解读已生成");
      const note: NoteRow = {
        id: Date.now(),
        content: typeof data.content === "string" ? data.content : "",
        model: typeof data.model === "string" ? data.model : "",
        created_at: new Date().toISOString(),
      };
      setSelectedJob((prev) => (prev?.id === selectedJobView.id ? { ...prev, note } : prev));
      setJobs((prev) => prev.map((job) => (job.id === selectedJobView.id ? { ...job, note } : job)));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "AI 解读失败");
    } finally {
      setInterpreting(false);
    }
  }, [selectedJobView]);

  const running = jobs.some((job) => job.status === "running" || job.status === "pending");
  const busy = running || startingKind !== null;
  const openAnalysis = (job: JobRow) => {
    setReportRetry(0);
    setSelectedJob(job);
  };

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>个股分析</CardTitle>
          <CardDescription>三策略分别计算买卖条件，AI 结合持仓与成交记录形成技术分析和综合结论。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex min-w-72 flex-1 flex-col gap-1.5">
              <Label htmlFor="symbols">个股代码（逗号或空格分隔，留空使用下方监控范围）</Label>
              <SymbolCombobox
                id="symbols"
                placeholder="600036.SH 000001.SZ"
                value={symbolsInput}
                onChange={setSymbolsInput}
              />
              <div className="flex flex-wrap items-center gap-1.5 pt-1">
                <span className="text-[11px] text-muted-foreground flex items-center gap-1 mr-1">
                  <Sparkles className="size-3 text-primary" /> 自选快捷:
                </span>
                {quickSymbols.map((item) => {
                  const isSelected = symbolsInput.split(/[\s,，;；]+/).includes(item.symbol);
                  return (
                    <button
                      key={item.symbol}
                      type="button"
                      onClick={() => toggleQuickSymbol(item.symbol)}
                      className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-mono transition-colors ${
                        isSelected
                          ? "bg-primary text-primary-foreground font-semibold shadow-xs"
                          : "bg-muted/60 text-muted-foreground hover:bg-muted hover:text-foreground"
                      }`}
                      title={isSelected ? "点击取消选择" : "点击添加到输入框"}
                    >
                      <span>{item.name || item.symbol}</span>
                      <span className="text-[10px] opacity-75">{item.symbol.split(".")[0]}</span>
                    </button>
                  );
                })}
                {symbolsInput.trim() && (
                  <button
                    type="button"
                    onClick={() => setSymbolsInput("")}
                    className="text-[11px] text-muted-foreground hover:text-destructive underline ml-1 cursor-pointer"
                  >
                    清空
                  </button>
                )}
              </div>
            </div>
            <div className="flex items-center gap-2 pb-2">
              <Switch id="notify" checked={notify} onCheckedChange={setNotify} />
              <Label htmlFor="notify">启用推送</Label>
            </div>
          </div>
          <AnalysisScopePicker value={scope} onChange={setScope} />
          <p className="text-xs text-muted-foreground">每只股票生成五个页签，通常调用 AI 两次；无新推送信号也会保留本轮分析记录。</p>
          <div className="flex flex-wrap gap-2">
            <TooltipProvider delay={300}>
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Button
                      onClick={() =>
                        runJob(
                          "analyze",
                          symbolsInput.trim()
                            ? { symbols: symbolsInput.split(/[\s,，;；]+/).filter(Boolean) }
                            : { scope }
                        )
                      }
                      disabled={busy}
                    >
                      {startingKind === "analyze" ? (
                        <Loader2 className="size-4 animate-spin" />
                      ) : (
                        <Play className="size-4" />
                      )}{" "}
                      {startingKind === "analyze" ? "启动中..." : "个股分析"}
                    </Button>
                  }
                />
                <TooltipContent side="bottom">
                  <p className="font-medium">个股分析</p>
                  <p className="mt-0.5 text-background/70">
                    分析指定股票（留空用自选池）：多周期缠论买卖点 + MACD 信号，结果写入 output 目录。
                    开启推送时，新鲜且达阈值的新信号会进入通知队列。
                  </p>
                </TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Button variant="secondary" onClick={() => runJob("monitor-once", { scope })} disabled={busy}>
                      {startingKind === "monitor-once" ? "启动中..." : "监控一次"}
                    </Button>
                  }
                />
                <TooltipContent side="bottom">
                  <p className="font-medium">监控一次</p>
                  <p className="mt-0.5 text-background/70">
                    对上方选定范围完成一轮分析，每只股票生成一份五页签报告；重复股票自动合并。
                  </p>
                </TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Button variant="outline" onClick={() => runJob("test-notify")} disabled={busy}>
                      {startingKind === "test-notify" ? "启动中..." : "测试通知"}
                    </Button>
                  }
                />
                <TooltipContent side="bottom">
                  <p className="font-medium">测试通知</p>
                  <p className="mt-0.5 text-background/70">
                    向所有已启用的通知通道（微信/Webhook/邮件/Bark）发送一条测试信号，验证推送配置是否可用。
                  </p>
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </div>
        </CardContent>
      </Card>

      <AnalysisRecords />

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <CardTitle>最近任务</CardTitle>
              <CardDescription>
                查看分析、扫描和监控任务的执行状态与数据源；具体个股结论见上方分析记录。
              </CardDescription>
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 gap-1.5 text-xs text-muted-foreground hover:text-foreground"
              onClick={() => void handleManualRefresh()}
              disabled={refreshingJobs}
            >
              <RefreshCw className={cn("size-3.5", refreshingJobs && "animate-spin")} />
              {refreshingJobs ? "刷新中..." : "手动刷新"}
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>类型</TableHead>
                <TableHead>股票名称</TableHead>
                <TableHead>模型</TableHead>
                <TableHead>执行结果</TableHead>
                <TableHead>创建时间</TableHead>
                <TableHead className="w-44">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {!jobsLoaded && (
                <>
                  {[1, 2, 3].map((i) => (
                    <TableRow key={i}>
                      <TableCell><Skeleton className="h-4 w-10" /></TableCell>
                      <TableCell><Skeleton className="h-4 w-16" /></TableCell>
                      <TableCell><Skeleton className="h-4 w-24" /></TableCell>
                      <TableCell><Skeleton className="h-4 w-20" /></TableCell>
                      <TableCell><Skeleton className="h-5 w-16 rounded-full" /></TableCell>
                      <TableCell><Skeleton className="h-4 w-28" /></TableCell>
                      <TableCell><Skeleton className="h-8 w-36 rounded-md" /></TableCell>
                    </TableRow>
                  ))}
                </>
              )}
              {jobs.slice(0, 50).map((job) => (
                <TableRow key={job.id} className="hover:bg-muted/40">
                  <TableCell className="font-mono text-xs font-semibold">#{job.id}</TableCell>
                  <TableCell>{KIND_LABEL[job.kind] ?? job.kind}</TableCell>
                  <TableCell className="max-w-40 truncate text-xs text-muted-foreground">
                    {job.symbol_names || "-"}
                  </TableCell>
                  <TableCell className="max-w-32 truncate text-xs text-muted-foreground">
                    {job.note?.model ?? "-"}
                  </TableCell>
                  <TableCell>
                    <JobStatusBadge status={job.status} />
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{job.created_at}</TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={!job.result_path}
                        onClick={() => void openDataSource(job)}
                      >
                        <Database className="size-4" /> 数据源
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => openAnalysis(job)}>
                        <Eye className="size-4" /> 查看 AI 分析
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
              {jobsLoaded && jobs.length === 0 && (
                <TableRow>
                  <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                    暂无执行任务，可通过上方控制台发起分析或监控
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={selectedJobView !== null} onOpenChange={(open) => !open && setSelectedJob(null)}>
        <DialogContent className="flex h-[92vh] max-h-[92vh] w-[96vw] max-w-[1600px] flex-col gap-0 overflow-hidden p-0">
          <DialogHeader className="shrink-0 gap-1 border-b pb-4 pl-6 pr-16 pt-4">
            <DialogTitle>
              分析详情 #{selectedJobView?.id}（{selectedJobView ? KIND_LABEL[selectedJobView.kind] ?? selectedJobView.kind : ""}）
            </DialogTitle>
            <DialogDescription>
              {selectedJobView?.created_at}
              {selectedJobView?.note?.model ? " · 模型：" + selectedJobView.note.model : ""}
              {selectedJobView?.result_path ? " · " + fileName(selectedJobView.result_path) : ""}
            </DialogDescription>
          </DialogHeader>
          {selectedJobView?.payload.analysis_version === 2 ? <div className="min-h-0 flex-1 overflow-y-auto p-5"><AnalysisRecords key={selectedJobView.id} jobId={selectedJobView.id} /></div> : <Tabs
            key={`${selectedJobView?.id}:${selectedJobView?.status}:${selectedJobView?.result_path ?? ""}`}
            defaultValue="ai"
            className="flex min-h-0 flex-1 flex-col gap-0"
          >
            <TabsList variant="line" className="w-full shrink-0 justify-start rounded-none border-b px-6">
              <TabsTrigger value="ai" className="flex-none px-3">AI 解读</TabsTrigger>
              {selectedJobView?.kind === "analyze" && <TabsTrigger value="report" className="flex-none px-3">研究报告</TabsTrigger>}
            </TabsList>
            {selectedJobView?.kind === "analyze" && (
              <TabsContent value="report" className="min-h-0 flex-1 overflow-y-auto p-3 sm:p-5">
                <div className="stock-report-page">
                  {selectedJobView.status === "running" || selectedJobView.status === "pending" ? (
                    <div className="report-panel text-sm text-muted-foreground">任务正在运行，完成后可查看研究报告。</div>
                  ) : selectedJobView.status === "failed" ? (
                    <div className="report-panel text-sm text-destructive">{selectedJobView.error || "任务失败，无研究报告"}</div>
                  ) : selectedJobView.result_path ? (
                    <StockReportLoader
                      key={`${selectedJobView.id}:${reportRetry}`}
                      jobId={selectedJobView.id}
                      onRetry={() => setReportRetry((value) => value + 1)}
                    />
                  ) : (
                    <div className="report-panel text-sm text-muted-foreground">该任务没有可读取的结果文件。</div>
                  )}
                </div>
              </TabsContent>
            )}
            <TabsContent value="ai" className="min-h-0 flex-1 overflow-y-auto p-6">
              {selectedJobView?.status === "running" || selectedJobView?.status === "pending" ? (
                <p className="text-sm text-muted-foreground">任务正在运行，结果生成后会自动解读并在本页出现。</p>
              ) : selectedJobView?.status === "failed" ? (
                <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
                  {selectedJobView.error || "任务失败，无解读"}
                </div>
              ) : selectedJobView?.note ? (
                <MarkdownContent content={selectedJobView.note.content} />
              ) : (
                <div className="flex flex-col items-start gap-3">
                  <p className="text-sm text-muted-foreground">该任务暂无 AI 解读，点击下方按钮手动生成。</p>
                  <Button onClick={() => void interpret()} disabled={interpreting}>
                    <Sparkles className="size-4" />
                    {interpreting ? "生成中..." : "生成 AI 解读"}
                  </Button>
                  {selectedJobView?.result_path && (
                    <>
                      <Separator />
                      <pre className="text-xs text-muted-foreground">{selectedJobView.result_path}</pre>
                    </>
                  )}
                </div>
              )}
            </TabsContent>
          </Tabs>}
        </DialogContent>
      </Dialog>

      <Dialog open={dataJob !== null} onOpenChange={(open) => { if (!open) { dataRequest.current++; setDataJob(null); } }}>
        <DialogContent className="flex max-h-[85vh] w-[95vw] max-w-[1200px] flex-col gap-0 overflow-hidden p-0">
          <DialogHeader className="shrink-0 gap-1 border-b pb-4 pl-6 pr-16 pt-4">
            <DialogTitle>
              数据源 #{dataJob?.id}（{dataJob ? KIND_LABEL[dataJob.kind] ?? dataJob.kind : ""}）
            </DialogTitle>
            <DialogDescription>
              {dataJob?.created_at}
              {dataJob?.result_path ? " · " + fileName(dataJob.result_path) : ""}
              {dataSource?.analyzed_at ? " · 分析时间 " + dataSource.analyzed_at : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto p-6">
            {dataLoading ? (
              <p className="text-sm text-muted-foreground">正在读取结果文件...</p>
            ) : dataError ? (
              <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
                {dataError}
              </div>
            ) : dataSource ? (
              <DataSourceView source={dataSource} />
            ) : (
              <p className="text-sm text-muted-foreground">该任务暂无结果文件。</p>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
