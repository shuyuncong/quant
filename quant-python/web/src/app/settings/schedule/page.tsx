"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { AnalysisScopePicker } from "@/components/analysis-scope-picker";
import { DEFAULT_ANALYSIS_SCOPE } from "@/lib/analysis-types";
import type { ScheduleRow } from "@/lib/types";
import {
  Activity,
  Calendar,
  CalendarClock,
  Clock,
  Loader2,
  Play,
  Save,
  Timer,
} from "lucide-react";

interface ScheduleData {
  rows: ScheduleRow[];
  calendar: {
    is_trading_day: boolean;
    is_trading_session: boolean;
  };
  next_runs: Record<string, string | null>;
}

const DEFINITIONS = [
  {
    kind: "daily_scan",
    title: "每日三策略筛选",
    subtitle: "收盘全市场筛选与指标候选池更新",
    description: "在指定时刻（推荐设在交易日收盘后 15:20）筛选全市场 A 股并更新日线金叉、底背离、年线指标候选池；同日内仅触发一次。",
    icon: CalendarClock,
  },
  {
    kind: "monitor_cycle",
    title: "盘中间隔巡检",
    subtitle: "交易时段高频周期性多策略分析",
    description: "仅在 A 股交易时段内每隔 N 分钟自动分析所选范围标的，计算缠论五标签买卖点与 AI 决策结论，输出至分析报告与推送通知。",
    icon: Timer,
  },
  {
    kind: "monitor_fixed",
    title: "固定时点巡检",
    subtitle: "特定关键盘口时点深度分析",
    description: "独立于时间间隔，在设定的关键时间点（如开盘 10:30、尾盘 14:30、收盘 15:20）执行分析；若调度延迟超过 5 分钟将记为错过。",
    icon: Clock,
  },
] as const;

const QUICK_FIXED_TIMES = ["10:30", "14:30", "15:20"];

export default function SchedulePage() {
  const [data, setData] = useState<ScheduleData | null>(null);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState<string | null>(null);
  const [fixedText, setFixedText] = useState("");

  const load = async () => {
    const response = await fetch("/api/schedule");
    if (!response.ok) throw new Error("加载定时任务失败");
    const value = (await response.json()) as ScheduleData;
    setData(value);
    setFixedText(
      value.rows.find((row) => row.kind === "monitor_fixed")?.fixed_times.join(", ") ??
        "15:20"
    );
  };

  useEffect(() => {
    const timer = setTimeout(
      () => void load().catch((error) => toast.error(String(error))),
      0
    );
    return () => clearTimeout(timer);
  }, []);

  const patch = (kind: string, patchObj: Partial<ScheduleRow>) =>
    setData((current) =>
      current
        ? {
            ...current,
            rows: current.rows.map((row) =>
              row.kind === kind ? { ...row, ...patchObj } : row
            ),
          }
        : current
    );

  const save = async () => {
    if (!data) return;
    setSaving(true);
    try {
      const rows = data.rows.map((row) =>
        row.kind === "monitor_fixed"
          ? {
              ...row,
              fixed_times: fixedText
                .split(/[,，;；\s]+/)
                .filter(Boolean),
            }
          : row
      );
      const response = await fetch("/api/schedule", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      setData(result);
      toast.success("定时设置已保存");
    } catch (error) {
      toast.error(String(error));
    } finally {
      setSaving(false);
    }
  };

  const run = async (row: ScheduleRow) => {
    setRunning(row.kind);
    try {
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: row.kind === "daily_scan" ? "daily-scan" : "monitor-once",
          scope: row.scope,
          notify: true,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      toast.success(`任务 #${result.jobId} 已启动`);
    } catch (error) {
      toast.error(String(error));
    } finally {
      setRunning(null);
    }
  };

  const toggleFixedTime = (t: string) => {
    const current = fixedText.split(/[,，;；\s]+/).filter(Boolean);
    if (current.includes(t)) {
      setFixedText(current.filter((item) => item !== t).join(", "));
    } else {
      setFixedText([...current, t].sort().join(", "));
    }
  };

  const enabledCount = data?.rows.filter((r) => r.enabled).length ?? 0;

  return (
    <div className="max-w-5xl space-y-6">
      {/* 顶栏标题与保存操作 */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">定时任务调度</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            基于 A 股交易日历的自动化巡检与选股任务调度；所有时刻均为北京时间。
          </p>
        </div>
        <Button
          onClick={() => void save()}
          disabled={saving || !data}
          className="gap-1.5 self-start sm:self-auto"
        >
          {saving ? (
            <Loader2 className="size-4 animate-spin" />
          ) : (
            <Save className="size-4" />
          )}
          {saving ? "保存中…" : "保存所有设置"}
        </Button>
      </div>

      {/* 状态看板条 */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div className="flex items-center gap-3 rounded-xl border bg-card p-3.5 shadow-xs">
          <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Calendar className="size-4.5" />
          </div>
          <div>
            <div className="text-xs text-muted-foreground">交易日判定</div>
            <div className="mt-0.5 flex items-center gap-1.5 font-semibold text-sm">
              <span
                className={`size-2 rounded-full ${
                  data?.calendar.is_trading_day
                    ? "bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.5)]"
                    : "bg-muted-foreground"
                }`}
              />
              {data ? (data.calendar.is_trading_day ? "今日为交易日" : "今日非交易日 (休市)") : "-"}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3 rounded-xl border bg-card p-3.5 shadow-xs">
          <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Activity className="size-4.5" />
          </div>
          <div>
            <div className="text-xs text-muted-foreground">交易时段状态</div>
            <div className="mt-0.5 flex items-center gap-1.5 font-semibold text-sm">
              <span
                className={`size-2 rounded-full ${
                  data?.calendar.is_trading_session
                    ? "bg-emerald-500 animate-pulse"
                    : "bg-muted-foreground"
                }`}
              />
              {data ? (data.calendar.is_trading_session ? "交易时段中" : "非交易时段 (闭盘)") : "-"}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3 rounded-xl border bg-card p-3.5 shadow-xs">
          <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Clock className="size-4.5" />
          </div>
          <div>
            <div className="text-xs text-muted-foreground">自动化任务</div>
            <div className="mt-0.5 font-semibold text-sm">
              已启用 {enabledCount} / 3 项定时任务
            </div>
          </div>
        </div>
      </div>

      {/* 任务配置卡片列表 */}
      {!data ? (
        <div className="space-y-4">
          {[1, 2, 3].map((i) => (
            <Card key={i}>
              <CardHeader className="gap-2">
                <Skeleton className="h-5 w-40" />
                <Skeleton className="h-4 w-72" />
              </CardHeader>
              <CardContent className="space-y-3">
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-8 w-48" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <div className="space-y-4">
          {DEFINITIONS.map((definition) => {
            const row = data.rows.find((r) => r.kind === definition.kind);
            const isEnabled = row?.enabled ?? false;
            const nextRun = data.next_runs[definition.kind];
            const Icon = definition.icon;

            return (
              <Card
                key={definition.kind}
                className={`transition-all ${
                  isEnabled ? "border-primary/30 shadow-xs" : "opacity-85"
                }`}
              >
                <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex items-start gap-3">
                    <div
                      className={`mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-lg ${
                        isEnabled
                          ? "bg-primary/10 text-primary"
                          : "bg-muted text-muted-foreground"
                      }`}
                    >
                      <Icon className="size-4.5" />
                    </div>
                    <div>
                      <div className="flex items-center gap-2">
                        <CardTitle className="text-base font-semibold">
                          {definition.title}
                        </CardTitle>
                        <Badge
                          variant={isEnabled ? "default" : "outline"}
                          className="text-[11px]"
                        >
                          {isEnabled ? "已启用" : "已停用"}
                        </Badge>
                      </div>
                      <CardDescription className="mt-1 text-xs">
                        {definition.description}
                      </CardDescription>
                    </div>
                  </div>

                  <div className="flex items-center gap-3 shrink-0">
                    <div className="flex items-center gap-2">
                      <Label
                        htmlFor={`enable-${definition.kind}`}
                        className="text-xs font-medium cursor-pointer"
                      >
                        {isEnabled ? "开启运行" : "暂停运行"}
                      </Label>
                      <Switch
                        id={`enable-${definition.kind}`}
                        disabled={!row || saving}
                        checked={isEnabled}
                        onCheckedChange={(enabled) =>
                          patch(definition.kind, { enabled })
                        }
                      />
                    </div>
                  </div>
                </CardHeader>

                <CardContent className="space-y-4 pt-1">
                  {/* 配置参数工具行 */}
                  <div className="flex flex-wrap items-center gap-4 rounded-lg border bg-muted/20 p-3">
                    {definition.kind === "daily_scan" ? (
                      <div className="flex items-center gap-2">
                        <Label htmlFor="daily-time" className="text-xs whitespace-nowrap">
                          执行时刻 (北京时间)
                        </Label>
                        <Input
                          id="daily-time"
                          type="time"
                          className="h-8.5 w-32 bg-background font-mono text-xs"
                          value={row?.time ?? "15:20"}
                          onChange={(event) =>
                            patch(definition.kind, { time: event.target.value })
                          }
                        />
                      </div>
                    ) : definition.kind === "monitor_cycle" ? (
                      <div className="flex items-center gap-2">
                        <Label htmlFor="interval-minutes" className="text-xs whitespace-nowrap">
                          巡检间隔
                        </Label>
                        <div className="flex items-center gap-1.5">
                          <Input
                            id="interval-minutes"
                            className="h-8.5 w-24 bg-background font-mono text-xs"
                            type="number"
                            min="1"
                            max="1440"
                            value={(row?.interval_seconds ?? 300) / 60}
                            onChange={(event) =>
                              patch(definition.kind, {
                                interval_seconds: Number(event.target.value) * 60,
                              })
                            }
                          />
                          <span className="text-xs text-muted-foreground">分钟</span>
                        </div>
                      </div>
                    ) : (
                      <div className="flex flex-1 flex-wrap items-center gap-2">
                        <Label htmlFor="fixed-times" className="text-xs whitespace-nowrap">
                          固定时点
                        </Label>
                        <Input
                          id="fixed-times"
                          value={fixedText}
                          onChange={(event) => setFixedText(event.target.value)}
                          placeholder="10:30, 14:30, 15:20"
                          className="h-8.5 min-w-52 flex-1 bg-background font-mono text-xs"
                        />
                        <div className="flex items-center gap-1">
                          <span className="text-[11px] text-muted-foreground mr-0.5">
                            快捷:
                          </span>
                          {QUICK_FIXED_TIMES.map((t) => {
                            const isIncluded = fixedText
                              .split(/[,，;；\s]+/)
                              .filter(Boolean)
                              .includes(t);
                            return (
                              <button
                                key={t}
                                type="button"
                                onClick={() => toggleFixedTime(t)}
                                className={`rounded px-1.5 py-0.5 font-mono text-[11px] transition-colors ${
                                  isIncluded
                                    ? "bg-primary text-primary-foreground font-medium"
                                    : "bg-muted text-muted-foreground hover:bg-muted/80"
                                }`}
                              >
                                {t}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    )}

                    <label className="flex h-8.5 items-center gap-2 text-xs cursor-pointer select-none">
                      <input
                        type="checkbox"
                        className="rounded border-input text-primary focus:ring-primary size-3.5"
                        checked={row?.trading_days_only ?? true}
                        onChange={(event) =>
                          patch(definition.kind, {
                            trading_days_only: event.target.checked,
                          })
                        }
                      />
                      <span>仅交易日执行</span>
                    </label>

                    <div className="ml-auto">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={!row || running !== null}
                        onClick={() => row && void run(row)}
                        className="h-8.5 text-xs gap-1.5"
                        title="立即触发一次后台任务"
                      >
                        {running === definition.kind ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <Play className="size-3.5" />
                        )}
                        {running === definition.kind ? "启动中…" : "立即执行一次"}
                      </Button>
                    </div>
                  </div>

                  {/* 选股与分析范围选择器 */}
                  {definition.kind !== "daily_scan" && (
                    <div className="pt-1">
                      <div className="mb-1.5 text-xs font-medium text-muted-foreground">
                        巡检分析股票范围
                      </div>
                      <AnalysisScopePicker
                        value={row?.scope ?? DEFAULT_ANALYSIS_SCOPE}
                        onChange={(scope) => patch(definition.kind, { scope })}
                      />
                    </div>
                  )}

                  {/* 调度下一次预计时间 */}
                  <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3 text-xs text-muted-foreground">
                    <div className="flex items-center gap-1.5">
                      <Clock className="size-3.5 opacity-70" />
                      <span>下次预计：</span>
                      <span className="font-mono font-medium text-foreground">
                        {nextRun ?? "未启用"}
                      </span>
                    </div>
                    <span className="text-[11px] opacity-75">
                      实际执行以交易日历与任务并发队列为准
                    </span>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
