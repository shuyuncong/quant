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
import { Input } from "@/components/ui/input";
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
import { Play, RefreshCw, Sparkles } from "lucide-react";

interface JobRow {
  id: number;
  kind: string;
  status: string;
  payload: Record<string, unknown>;
  result_path: string | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

interface ResultItem {
  file: string;
  mtime: number;
  summary: Record<string, unknown>;
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

const STATUS_VARIANT: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  success: "default",
  running: "secondary",
  pending: "outline",
  failed: "destructive",
};

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
  const [results, setResults] = useState<ResultItem[]>([]);
  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [symbolsInput, setSymbolsInput] = useState("");
  const [notify, setNotify] = useState(true);
  const [loading, setLoading] = useState(true);
  const [detailFile, setDetailFile] = useState<string | null>(null);
  const [detail, setDetail] = useState<{
    report: Record<string, unknown>;
    notes: Array<{ id: number; content: string; model: string; created_at: string }>;
  } | null>(null);
  const [interpreting, setInterpreting] = useState(false);

  const loadResults = useCallback(async () => {
    try {
      const response = await fetch("/api/results");
      if (!response.ok) throw new Error("加载结果失败");
      const data = (await response.json()) as { results: ResultItem[] };
      setResults(data.results);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载结果失败");
    } finally {
      setLoading(false);
    }
  }, []);

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
    void loadResults();
    void loadJobs();
    const timer = setInterval(() => {
      void loadJobs();
    }, 5000);
    return () => clearInterval(timer);
  }, [loadResults, loadJobs]);

  const openDetail = useCallback(async (file: string) => {
    setDetailFile(file);
    setDetail(null);
    try {
      const response = await fetch(`/api/results/${encodeURIComponent(file)}`);
      if (!response.ok) throw new Error("加载详情失败");
      const data = (await response.json()) as typeof detail;
      setDetail(data);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载详情失败");
    }
  }, []);

  const runJob = useCallback(
    async (kind: string, extra: Record<string, unknown> = {}) => {
      try {
        const data = (await postJson("/api/run", { kind, notify, ...extra })) as { jobId?: number };
        toast.success(`任务已启动 #${data.jobId ?? ""}`);
        void loadJobs();
        setTimeout(() => void loadResults(), 1500);
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "启动失败");
      }
    },
    [notify, loadJobs, loadResults]
  );

  const interpret = useCallback(async () => {
    if (!detailFile) return;
    setInterpreting(true);
    try {
      const data = (await postJson(`/api/results/${encodeURIComponent(detailFile)}/interpret`, {})) as {
        content?: string;
        model?: string;
      };
      toast.success("AI 解读已生成");
      setDetail((prev) => ({
        report: prev?.report ?? {},
        notes: [
          {
            id: Date.now(),
            content: data.content ?? "",
            model: data.model ?? "",
            created_at: new Date().toISOString(),
          },
          ...(prev?.notes ?? []),
        ],
      }));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "AI 解读失败");
    } finally {
      setInterpreting(false);
    }
  }, [detailFile]);

  const running = jobs.some((job) => job.status === "running" || job.status === "pending");

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>手动运行</CardTitle>
          <CardDescription>立即触发一次分析/扫描/监控，结果写入 output 目录并显示在下方列表。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex min-w-72 flex-1 flex-col gap-1.5">
              <Label htmlFor="symbols">个股代码（analyze，逗号/空格分隔，留空使用自选股）</Label>
              <Input
                id="symbols"
                placeholder="600036.SH 000001.SZ"
                value={symbolsInput}
                onChange={(event) => setSymbolsInput(event.target.value)}
              />
            </div>
            <div className="flex items-center gap-2 pb-2">
              <Switch id="notify" checked={notify} onCheckedChange={setNotify} />
              <Label htmlFor="notify">启用推送</Label>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
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
            <Button variant="secondary" onClick={() => runJob("scan")} disabled={running}>
              日线扫描
            </Button>
            <Button variant="secondary" onClick={() => runJob("monitor-once")} disabled={running}>
              监控一次
            </Button>
            <Button variant="secondary" onClick={() => runJob("dispatch-outbox")} disabled={running}>
              补投队列
            </Button>
            <Button variant="outline" onClick={() => runJob("test-notify")} disabled={running}>
              测试通知
            </Button>
            <Button variant="ghost" onClick={() => void loadResults()} disabled={loading}>
              <RefreshCw className="size-4" /> 刷新结果
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>最近任务</CardTitle>
          <CardDescription>Web 与定时触发的任务都会记录在这里（每 5 秒自动刷新）。</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>类型</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>创建时间</TableHead>
                <TableHead>结果/错误</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobs.slice(0, 20).map((job) => (
                <TableRow key={job.id}>
                  <TableCell className="font-mono text-xs">#{job.id}</TableCell>
                  <TableCell>{KIND_LABEL[job.kind] ?? job.kind}</TableCell>
                  <TableCell>
                    <Badge variant={STATUS_VARIANT[job.status] ?? "outline"}>{job.status}</Badge>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{job.created_at}</TableCell>
                  <TableCell className="max-w-64 truncate text-xs text-muted-foreground">
                    {job.status === "failed" ? job.error : job.result_path ?? "-"}
                  </TableCell>
                </TableRow>
              ))}
              {jobs.length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="text-center text-muted-foreground">
                    暂无任务
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>分析结果</CardTitle>
          <CardDescription>来自 signal_system/output 目录，按修改时间倒序。</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>文件</TableHead>
                <TableHead>类型</TableHead>
                <TableHead>时间</TableHead>
                <TableHead>摘要</TableHead>
                <TableHead className="w-24">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {results.map((item) => (
                <TableRow key={item.file}>
                  <TableCell className="font-mono text-xs">{item.file}</TableCell>
                  <TableCell>
                    <Badge variant="outline">{String(item.summary.mode ?? "?")}</Badge>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {String(item.summary.scanned_at ?? "")}
                  </TableCell>
                  <TableCell className="max-w-80 truncate text-xs text-muted-foreground">
                    {JSON.stringify(item.summary).slice(0, 160)}
                  </TableCell>
                  <TableCell>
                    <Button variant="outline" size="sm" onClick={() => void openDetail(item.file)}>
                      查看
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
              {results.length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="text-center text-muted-foreground">
                    {loading ? "加载中..." : "暂无结果，先运行一次分析"}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={detailFile !== null} onOpenChange={(open) => !open && setDetailFile(null)}>
        <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>结果详情：{detailFile}</DialogTitle>
            <DialogDescription>AI 解读为补充字段，不影响原始分析结果。</DialogDescription>
          </DialogHeader>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => void interpret()} disabled={interpreting}>
              <Sparkles className="size-4" />
              {interpreting ? "生成中..." : "生成 AI 解读"}
            </Button>
          </div>
          {detail?.notes.map((note) => (
            <div key={note.id} className="rounded-lg border p-3">
              <div className="mb-1 text-xs text-muted-foreground">
                模型：{note.model} · {note.created_at}
              </div>
              <div className="whitespace-pre-wrap text-sm">{note.content}</div>
            </div>
          ))}
          <Separator />
          <pre className="max-h-96 overflow-auto rounded-lg bg-muted/50 p-3 text-xs">
            {JSON.stringify(detail?.report ?? null, null, 2)}
          </pre>
        </DialogContent>
      </Dialog>
    </div>
  );
}
