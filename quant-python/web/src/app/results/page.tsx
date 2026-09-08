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
import { Check, Copy, Database, Download, Eye, Play, Sparkles } from "lucide-react";
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

function fileName(resultPath: string | null): string {
  if (!resultPath) return "-";
  return resultPath.split(/[\\/]/).pop() ?? resultPath;
}

async function postJson(url: string, body: unknown) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = (await response.json().catch(() => ({}))) as { error?: string };
  if (!response.ok) throw new Error(data.error || `请求失败: ${response.status}`);
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
  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [symbolsInput, setSymbolsInput] = useState("");
  const [notify, setNotify] = useState(true);
  const [selectedJob, setSelectedJob] = useState<JobRow | null>(null);
  const [interpreting, setInterpreting] = useState(false);
  const [dataJob, setDataJob] = useState<JobRow | null>(null);
  const [dataSource, setDataSource] = useState<DataSource | null>(null);
  const [dataLoading, setDataLoading] = useState(false);
  const [dataError, setDataError] = useState<string | null>(null);
  const [reportRetry, setReportRetry] = useState(0);
  const dataRequest = useRef(0);

  const loadJobs = useCallback(async () => {
    try {
      const response = await fetch("/api/jobs");
      if (!response.ok) return;
      const data = (await response.json()) as { jobs: JobRow[] };
      setJobs(data.jobs);
    } catch {
      /* ignore polling errors */
    }
  }, []);

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

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadJobs();
    const timer = setInterval(() => {
      void loadJobs();
    }, 5000);
    return () => clearInterval(timer);
  }, [loadJobs]);

  const runJob = useCallback(
    async (kind: string, extra: Record<string, unknown> = {}) => {
      try {
        const data = (await postJson("/api/run", { kind, notify, ...extra })) as { jobId?: number };
        toast.success(`任务已启动 #${data.jobId ?? ""}`);
        void loadJobs();
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "启动失败");
      }
    },
    [notify, loadJobs]
  );

  const selectedJobView = selectedJob
    ? jobs.find((job) => job.id === selectedJob.id) ?? selectedJob
    : null;

  const interpret = useCallback(async () => {
    if (!selectedJobView) return;
    setInterpreting(true);
    try {
      const data = (await postJson(`/api/jobs/${selectedJobView.id}/interpret`, {})) as {
        content?: string;
        model?: string;
      };
      toast.success("AI 解读已生成");
      const note: NoteRow = {
        id: Date.now(),
        content: data.content ?? "",
        model: data.model ?? "",
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
  const openAnalysis = (job: JobRow) => {
    setReportRetry(0);
    setSelectedJob(job);
  };

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>手动运行</CardTitle>
          <CardDescription>立即触发一次分析/扫描/监控，完成后自动生成 AI 解读，在下方最近任务中查看。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex min-w-72 flex-1 flex-col gap-1.5">
              <Label htmlFor="symbols">个股代码（可输入或从持仓下拉选择，逗号/空格分隔，留空使用自选股）</Label>
              <SymbolCombobox
                id="symbols"
                placeholder="600036.SH 000001.SZ"
                value={symbolsInput}
                onChange={setSymbolsInput}
              />
            </div>
            <div className="flex items-center gap-2 pb-2">
              <Switch id="notify" checked={notify} onCheckedChange={setNotify} />
              <Label htmlFor="notify">启用推送</Label>
            </div>
          </div>
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
                            : {}
                        )
                      }
                      disabled={running}
                    >
                      <Play className="size-4" /> 个股分析
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
                    <Button variant="secondary" onClick={() => runJob("scan")} disabled={running}>
                      日线扫描
                    </Button>
                  }
                />
                <TooltipContent side="bottom">
                  <p className="font-medium">日线扫描</p>
                  <p className="mt-0.5 text-background/70">
                    与股票池页的筛选同一引擎，范围按配置 scan.universe_mode（当前 all_a 全市场）。
                    扫描结果写入候选池并输出报告；开启推送时扫完推一条汇总通知。
                  </p>
                </TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Button variant="secondary" onClick={() => runJob("monitor-once")} disabled={running}>
                      监控一次
                    </Button>
                  }
                />
                <TooltipContent side="bottom">
                  <p className="font-medium">监控一次</p>
                  <p className="mt-0.5 text-background/70">
                    对「持仓 + 自选池 + 候选池」执行一次盘中监控：从轮询游标处取一批（默认 20 只）
                    做多周期分析。开启推送时只推「日线 0 轴上方金叉且买入分≥60」的新鲜信号。
                  </p>
                </TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Button variant="secondary" onClick={() => runJob("dispatch-outbox")} disabled={running}>
                      补投队列
                    </Button>
                  }
                />
                <TooltipContent side="bottom">
                  <p className="font-medium">补投队列</p>
                  <p className="mt-0.5 text-background/70">
                    重试通知 outbox 里投递失败/未发出的消息（每次最多 100 条）。
                    通知通道恢复后，用它把积压的推送补发出去。
                  </p>
                </TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Button variant="outline" onClick={() => runJob("test-notify")} disabled={running}>
                      测试通知
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

      <Card>
        <CardHeader>
          <CardTitle>最近任务</CardTitle>
          <CardDescription>
            任务完成后自动调用模型生成 AI 解读，点「查看 AI 分析」直接阅读；未生成的可手动触发（每 5 秒自动刷新）。
          </CardDescription>
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
              {jobs.slice(0, 50).map((job) => (
                <TableRow key={job.id}>
                  <TableCell className="font-mono text-xs">#{job.id}</TableCell>
                  <TableCell>{KIND_LABEL[job.kind] ?? job.kind}</TableCell>
                  <TableCell className="max-w-40 truncate text-xs text-muted-foreground">
                    {job.symbol_names || "-"}
                  </TableCell>
                  <TableCell className="max-w-32 truncate text-xs text-muted-foreground">
                    {job.note?.model ?? "-"}
                  </TableCell>
                  <TableCell>
                    <Badge variant={STATUS_VARIANT[job.status] ?? "outline"}>
                      {STATUS_LABEL[job.status] ?? job.status}
                    </Badge>
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
              {jobs.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground">
                    暂无任务
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
          <Tabs
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
          </Tabs>
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
