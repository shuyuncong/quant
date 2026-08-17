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
import { Separator } from "@/components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { RefreshCw, ScrollText, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";

type LogLevel = "info" | "warning" | "error";

interface LogRow {
  id: number;
  job_id: number | null;
  level: LogLevel;
  module: string;
  message: string;
  detail: string | null;
  created_at: string;
}

const LEVEL_LABEL: Record<LogLevel, string> = {
  info: "信息",
  warning: "警告",
  error: "错误",
};

const LEVEL_BADGE: Record<LogLevel, "default" | "secondary" | "destructive" | "outline"> = {
  info: "outline",
  warning: "secondary",
  error: "destructive",
};

const LEVEL_CLASS: Record<LogLevel, string> = {
  info: "text-muted-foreground",
  warning: "text-amber-600",
  error: "text-red-600",
};

const MODULE_LABEL: Record<string, string> = {
  job: "任务",
  "auto-interpret": "自动解读",
  interpret: "手动解读",
};

export default function LogsPage() {
  const [logs, setLogs] = useState<LogRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [level, setLevel] = useState<LogLevel | "all">("all");
  const [selected, setSelected] = useState<LogRow | null>(null);

  const loadLogs = useCallback(async () => {
    try {
      const query = level === "all" ? "" : `?level=${level}`;
      const response = await fetch(`/api/logs${query}`);
      if (!response.ok) throw new Error("加载操作日志失败");
      const data = (await response.json()) as { logs: LogRow[] };
      setLogs(data.logs);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载操作日志失败");
    } finally {
      setLoading(false);
    }
  }, [level]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadLogs();
    const timer = setInterval(() => {
      void loadLogs();
    }, 5000);
    return () => clearInterval(timer);
  }, [loadLogs]);

  const clearLogs = useCallback(async () => {
    if (!window.confirm("确定清空全部操作日志吗？")) return;
    try {
      const response = await fetch("/api/logs", { method: "DELETE" });
      if (!response.ok) {
        const data = (await response.json().catch(() => ({}))) as { error?: string };
        throw new Error(data.error || "清空失败");
      }
      toast.success("操作日志已清空");
      void loadLogs();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "清空失败");
    }
  }, [loadLogs]);

  const filters: Array<{ value: LogLevel | "all"; label: string }> = [
    { value: "all", label: "全部" },
    { value: "error", label: "错误" },
    { value: "warning", label: "警告" },
    { value: "info", label: "信息" },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">操作日志</h1>
          <p className="text-sm text-muted-foreground">
            记录任务执行、AI 解读等操作；报错时可直接查看原因，无需翻服务端日志。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void loadLogs()} disabled={loading}>
            <RefreshCw className={cn("size-4", loading && "animate-spin")} />
            刷新
          </Button>
          <Button variant="outline" size="sm" onClick={() => void clearLogs()}>
            <Trash2 className="size-4" />
            清空
          </Button>
        </div>
      </div>

      <div className="flex items-center gap-2">
        {filters.map((item) => (
          <Button
            key={item.value}
            variant={level === item.value ? "default" : "outline"}
            size="sm"
            onClick={() => setLevel(item.value)}
          >
            {item.label}
          </Button>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ScrollText className="size-4" /> 日志列表
          </CardTitle>
          <CardDescription>最新在前，每 5 秒自动刷新。</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-16">ID</TableHead>
                <TableHead className="w-44">时间（北京时间）</TableHead>
                <TableHead className="w-20">级别</TableHead>
                <TableHead className="w-24">模块</TableHead>
                <TableHead>消息</TableHead>
                <TableHead className="w-20">任务</TableHead>
                <TableHead className="w-20">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {logs.map((log) => (
                <TableRow key={log.id} className={cn(log.level === "error" && "bg-red-50/60")}>
                  <TableCell className="font-mono text-xs">#{log.id}</TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">{log.created_at}</TableCell>
                  <TableCell>
                    <Badge variant={LEVEL_BADGE[log.level]}>{LEVEL_LABEL[log.level]}</Badge>
                  </TableCell>
                  <TableCell className="text-xs">{MODULE_LABEL[log.module] ?? log.module}</TableCell>
                  <TableCell className={cn("max-w-96 truncate text-xs", LEVEL_CLASS[log.level])}>
                    {log.message}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {log.job_id == null ? "-" : `#${log.job_id}`}
                  </TableCell>
                  <TableCell>
                    <Button variant="outline" size="sm" onClick={() => setSelected(log)}>
                      查看
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
              {logs.length === 0 && (
                <TableRow>
                  <TableCell colSpan={7} className="text-center text-muted-foreground">
                    {loading ? "加载中..." : "暂无操作日志"}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={selected !== null} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>日志详情 #{selected?.id}</DialogTitle>
            <DialogDescription>
              {selected?.created_at} 路 {MODULE_LABEL[selected?.module ?? ""] ?? selected?.module} 路{" "}
              {selected?.job_id != null ? `任务 #${selected.job_id}` : "无关联任务"}
            </DialogDescription>
          </DialogHeader>
          <div className="flex items-center gap-2">
            <Badge variant={selected ? LEVEL_BADGE[selected.level] : "outline"}>
              {selected ? LEVEL_LABEL[selected.level] : ""}
            </Badge>
          </div>
          <div className="whitespace-pre-wrap rounded-lg border p-3 text-sm">{selected?.message}</div>
          {selected?.detail && (
            <>
              <Separator />
              <div className="text-xs font-medium text-muted-foreground">详情</div>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-muted/50 p-3 text-xs">
                {selected.detail}
              </pre>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
