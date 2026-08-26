"use client";

import { useCallback, useEffect, useState } from "react";
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
import { Eye, Play, Sparkles } from "lucide-react";
import { MarkdownContent } from "@/components/markdown-content";
import { SymbolCombobox } from "@/components/symbol-combobox";

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

export default function ResultsPage() {
  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [symbolsInput, setSymbolsInput] = useState("");
  const [notify, setNotify] = useState(true);
  const [selectedJob, setSelectedJob] = useState<JobRow | null>(null);
  const [interpreting, setInterpreting] = useState(false);

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

  const interpret = useCallback(async () => {
    if (!selectedJob) return;
    setInterpreting(true);
    try {
      const data = (await postJson(`/api/jobs/${selectedJob.id}/interpret`, {})) as {
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
      setSelectedJob((prev) => (prev ? { ...prev, note } : prev));
      setJobs((prev) => prev.map((job) => (job.id === selectedJob.id ? { ...job, note } : job)));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "AI 解读失败");
    } finally {
      setInterpreting(false);
    }
  }, [selectedJob]);

  const running = jobs.some((job) => job.status === "running" || job.status === "pending");

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
                <TableHead className="w-28">操作</TableHead>
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
                    <Button variant="outline" size="sm" onClick={() => setSelectedJob(job)}>
                      <Eye className="size-4" /> 查看 AI 分析
                    </Button>
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

      <Dialog open={selectedJob !== null} onOpenChange={(open) => !open && setSelectedJob(null)}>
        <DialogContent className="flex max-h-[85vh] w-[95vw] max-w-[1600px] flex-col gap-0 overflow-hidden p-0">
          <DialogHeader className="shrink-0 gap-1 border-b pb-4 pl-6 pr-16 pt-4">
            <DialogTitle>
              AI 分析 #{selectedJob?.id}（{selectedJob ? KIND_LABEL[selectedJob.kind] ?? selectedJob.kind : ""}）
            </DialogTitle>
            <DialogDescription>
              {selectedJob?.created_at}
              {selectedJob?.note?.model ? " · 模型：" + selectedJob.note.model : ""}
              {selectedJob?.result_path ? " · " + fileName(selectedJob.result_path) : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto p-6">
            {selectedJob?.status === "running" || selectedJob?.status === "pending" ? (
              <p className="text-sm text-muted-foreground">任务正在运行，结果生成后会自动解读并在本页出现。</p>
            ) : selectedJob?.status === "failed" ? (
              <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
                {selectedJob.error || "任务失败，无解读"}
              </div>
            ) : selectedJob?.note ? (
              <MarkdownContent content={selectedJob.note.content} />
            ) : (
              <div className="flex flex-col items-start gap-3">
                <p className="text-sm text-muted-foreground">该任务暂无 AI 解读，点击下方按钮手动生成。</p>
                <Button onClick={() => void interpret()} disabled={interpreting}>
                  <Sparkles className="size-4" />
                  {interpreting ? "生成中..." : "生成 AI 解读"}
                </Button>
                {selectedJob?.result_path && (
                  <>
                    <Separator />
                    <pre className="text-xs text-muted-foreground">{selectedJob.result_path}</pre>
                  </>
                )}
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}