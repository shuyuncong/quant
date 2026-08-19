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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { RefreshCw, ScrollText, Send, Trash2 } from "lucide-react";
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

interface PushRecord {
  event_id: string;
  channel: string;
  status: string;
  attempts: number;
  next_attempt_at: string | null;
  last_error: string | null;
  delivered_at: string | null;
  claimed_at: string | null;
  symbol: string;
  timeframe: string;
  signal_type: string;
  side: string;
  confirmed_at: string;
  created_at: string;
  name: string;
  summary: string;
}

interface PushSummary {
  pending: number;
  delivered: number;
  failed: number;
  total_events: number;
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
  warning: "text-amber-600 dark:text-amber-400",
  error: "text-red-600 dark:text-red-400",
};

const MODULE_LABEL: Record<string, string> = {
  job: "任务",
  "auto-interpret": "自动解读",
  interpret: "手动解读",
};

const CHANNEL_LABEL: Record<string, string> = {
  wechat: "企业微信",
  email: "邮件",
  webhook: "Webhook",
  bark: "Bark",
};

const CHANNEL_BADGE: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  wechat: "default",
  email: "secondary",
  webhook: "outline",
  bark: "secondary",
};

const STATUS_LABEL: Record<string, string> = {
  pending: "待推送",
  sending: "发送中",
  delivered: "已投递",
  failed: "失败",
};

const STATUS_BADGE: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  pending: "outline",
  sending: "secondary",
  delivered: "default",
  failed: "destructive",
};

const SIDE_LABEL: Record<string, string> = {
  buy: "买入",
  sell: "卖出",
  reduce: "减仓",
  watch: "关注",
  info: "信息",
};

function signalTypeLabel(raw: string): string {
  if (raw.startsWith("ai_analysis")) return "AI 解读";
  if (raw.includes("macd_golden_cross")) return "MACD 金叉";
  if (raw.startsWith("buy")) return "买入信号";
  if (raw.startsWith("sell")) return "卖出信号";
  return raw;
}

function timeOrDash(value: string | null | undefined): string {
  return value && value.trim() !== "" ? value : "-";
}

export default function LogsPage() {
  const [tab, setTab] = useState<"logs" | "push">("logs");

  // 操作日志
  const [logs, setLogs] = useState<LogRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [level, setLevel] = useState<LogLevel | "all">("all");
  const [selected, setSelected] = useState<LogRow | null>(null);

  // 推送日志
  const [records, setRecords] = useState<PushRecord[]>([]);
  const [summary, setSummary] = useState<PushSummary>({
    pending: 0,
    delivered: 0,
    failed: 0,
    total_events: 0,
  });
  const [pushLoading, setPushLoading] = useState(true);
  const [pushSelected, setPushSelected] = useState<PushRecord | null>(null);

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

  const loadPush = useCallback(async () => {
    try {
      const response = await fetch("/api/push-logs?limit=200");
      const data = (await response.json().catch(() => ({}))) as {
        error?: string;
        records?: PushRecord[];
        summary?: PushSummary;
      };
      if (!response.ok) throw new Error(data.error || "加载推送日志失败");
      setRecords(Array.isArray(data.records) ? data.records : []);
      setSummary(data.summary ?? { pending: 0, delivered: 0, failed: 0, total_events: 0 });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载推送日志失败");
    } finally {
      setPushLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadLogs();
    const timer = setInterval(() => {
      void loadLogs();
    }, 5000);
    return () => clearInterval(timer);
  }, [loadLogs]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadPush();
    const timer = setInterval(() => {
      void loadPush();
    }, 5000);
    return () => clearInterval(timer);
  }, [loadPush]);

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

  const summaryCards = [
    { label: "待推送", value: summary.pending, className: "text-amber-600 dark:text-amber-400" },
    { label: "已投递", value: summary.delivered, className: "text-green-600 dark:text-green-400" },
    { label: "失败", value: summary.failed, className: "text-red-600 dark:text-red-400" },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">日志中心</h1>
        <p className="text-sm text-muted-foreground">
          操作日志记录任务与解读行为；推送日志展示信号事件经 outbox 向各通道投递的完整记录。
        </p>
      </div>

      <Tabs value={tab} onValueChange={(value) => setTab(value === "push" ? "push" : "logs")}>
        <TabsList variant="line">
          <TabsTrigger value="logs">操作日志</TabsTrigger>
          <TabsTrigger value="push">推送日志</TabsTrigger>
        </TabsList>

        <TabsContent value="logs">
          <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-base font-semibold">操作日志</h2>
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
                      <TableRow key={log.id} className={cn(log.level === "error" && "bg-red-50/60 dark:bg-red-500/10")}>
                        <TableCell className="font-mono text-xs">#{log.id}</TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          {log.created_at}
                        </TableCell>
                        <TableCell>
                          <Badge variant={LEVEL_BADGE[log.level]}>{LEVEL_LABEL[log.level]}</Badge>
                        </TableCell>
                        <TableCell className="text-xs">
                          {MODULE_LABEL[log.module] ?? log.module}
                        </TableCell>
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
          </div>
        </TabsContent>

        <TabsContent value="push">
          <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-base font-semibold">推送日志</h2>
                <p className="text-sm text-muted-foreground">
                  信号事件进入 outbox 后按通道投递；失败会自动重试，第 5 次失败后终止并保留错误信息。
                </p>
              </div>
              <Button variant="outline" size="sm" onClick={() => void loadPush()} disabled={pushLoading}>
                <RefreshCw className={cn("size-4", pushLoading && "animate-spin")} />
                刷新
              </Button>
            </div>

            <div className="grid grid-cols-3 gap-3">
              {summaryCards.map((item) => (
                <Card key={item.label}>
                  <CardContent className="flex items-baseline justify-between pt-4">
                    <span className="text-sm text-muted-foreground">{item.label}</span>
                    <span className={cn("text-2xl font-semibold", item.className)}>{item.value}</span>
                  </CardContent>
                </Card>
              ))}
            </div>

            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Send className="size-4" /> 投递记录
                </CardTitle>
                <CardDescription>最新在前（按事件时间倒序），共 {records.length} 条，每 5 秒自动刷新。</CardDescription>
              </CardHeader>
              <CardContent>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>时间</TableHead>
                      <TableHead>事件</TableHead>
                      <TableHead>类型</TableHead>
                      <TableHead>通道</TableHead>
                      <TableHead>摘要</TableHead>
                      <TableHead className="w-24">状态</TableHead>
                      <TableHead className="w-16 text-right">次数</TableHead>
                      <TableHead className="w-20">操作</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {records.map((record) => (
                      <TableRow
                        key={`${record.event_id}-${record.channel}`}
                        className={cn(record.status === "failed" && "bg-red-50/60 dark:bg-red-500/10")}
                      >
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          {record.created_at}
                        </TableCell>
                        <TableCell className="text-xs">
                          <span className="font-mono">{record.symbol}</span>
                          <span className="text-muted-foreground"> {record.name || ""}</span>
                        </TableCell>
                        <TableCell className="text-xs">
                          {signalTypeLabel(record.signal_type)}
                          {SIDE_LABEL[record.side] ? (
                            <span className="ml-1 text-muted-foreground">
                              {SIDE_LABEL[record.side]}
                            </span>
                          ) : null}
                        </TableCell>
                        <TableCell>
                          <Badge variant={CHANNEL_BADGE[record.channel] ?? "outline"}>
                            {CHANNEL_LABEL[record.channel] ?? record.channel}
                          </Badge>
                        </TableCell>
                        <TableCell className="max-w-80 truncate text-xs text-muted-foreground">
                          {record.summary || "-"}
                        </TableCell>
                        <TableCell>
                          <Badge variant={STATUS_BADGE[record.status] ?? "outline"}>
                            {STATUS_LABEL[record.status] ?? record.status}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-right font-mono text-xs">{record.attempts}</TableCell>
                        <TableCell>
                          <Button variant="outline" size="sm" onClick={() => setPushSelected(record)}>
                            查看
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                    {records.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={8} className="text-center text-muted-foreground">
                          {pushLoading ? "加载中..." : "暂无推送记录"}
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
          </div>
        </TabsContent>
      </Tabs>

      <Dialog open={selected !== null} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>日志详情 #{selected?.id}</DialogTitle>
            <DialogDescription>
              {selected?.created_at} {selected ? `· ${MODULE_LABEL[selected.module] ?? selected.module}` : ""}{" "}
              {selected?.job_id != null ? `· 任务 #${selected.job_id}` : ""}
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

      <Dialog open={pushSelected !== null} onOpenChange={(open) => !open && setPushSelected(null)}>
        <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>推送详情</DialogTitle>
            <DialogDescription>
              {pushSelected ? `${pushSelected.symbol} ${pushSelected.name || ""}` : ""} ·{" "}
              {pushSelected ? signalTypeLabel(pushSelected.signal_type) : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={pushSelected ? STATUS_BADGE[pushSelected.status] ?? "outline" : "outline"}>
              {pushSelected ? STATUS_LABEL[pushSelected.status] ?? pushSelected.status : ""}
            </Badge>
            <Badge variant={pushSelected ? CHANNEL_BADGE[pushSelected.channel] ?? "outline" : "outline"}>
              {pushSelected ? CHANNEL_LABEL[pushSelected.channel] ?? pushSelected.channel : ""}
            </Badge>
            {pushSelected?.last_error && <Badge variant="destructive">失败原因</Badge>}
          </div>
          <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-sm">
            <div className="text-muted-foreground">事件 ID</div>
            <div className="font-mono text-xs">{timeOrDash(pushSelected?.event_id)}</div>
            <div className="text-muted-foreground">信号类型</div>
            <div>{pushSelected ? signalTypeLabel(pushSelected.signal_type) : ""}{pushSelected?.side ? `（${SIDE_LABEL[pushSelected.side] ?? pushSelected.side}）` : ""}</div>
            <div className="text-muted-foreground">事件时间</div>
            <div className="font-mono text-xs">{timeOrDash(pushSelected?.created_at)}</div>
            <div className="text-muted-foreground">确认时间</div>
            <div className="font-mono text-xs">{timeOrDash(pushSelected?.confirmed_at)}</div>
            <div className="text-muted-foreground">投递时间</div>
            <div className="font-mono text-xs">{timeOrDash(pushSelected?.delivered_at)}</div>
            <div className="text-muted-foreground">尝试次数</div>
            <div className="font-mono text-xs">{pushSelected?.attempts ?? 0}</div>
            <div className="text-muted-foreground">下次重试</div>
            <div className="font-mono text-xs">{timeOrDash(pushSelected?.next_attempt_at)}</div>
          </div>
          {pushSelected?.summary && (
            <>
              <Separator />
              <div className="text-xs font-medium text-muted-foreground">内容摘要</div>
              <div className="whitespace-pre-wrap rounded-lg border p-3 text-sm">{pushSelected.summary}</div>
            </>
          )}
          {pushSelected?.last_error && (
            <>
              <Separator />
              <div className="text-xs font-medium text-red-600 dark:text-red-400">失败原因</div>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-red-50/60 p-3 text-xs dark:bg-red-500/10">
                {pushSelected.last_error}
              </pre>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
