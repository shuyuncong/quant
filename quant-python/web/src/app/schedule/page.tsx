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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { CalendarClock, Save } from "lucide-react";

interface ScheduleData {
  rows: Array<{
    id: number;
    kind: "daily_scan" | "monitor_cycle";
    time: string;
    interval_seconds: number;
    fixed_times: string[];
    trading_days_only: boolean;
    enabled: boolean;
  }>;
  calendar: { is_trading_day: boolean; is_trading_session: boolean; now: string };
  next_runs: Record<string, string | null>;
}

export default function SchedulePage() {
  const [data, setData] = useState<ScheduleData | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/schedule");
      if (!response.ok) throw new Error("加载定时配置失败");
      setData((await response.json()) as ScheduleData);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载定时配置失败");
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
    const timer = setInterval(() => void load(), 30000);
    return () => clearInterval(timer);
  }, [load]);

  const patchRow = (kind: string, patch: Partial<ScheduleData["rows"][number]>) => {
    setData((prev) =>
      prev ? { ...prev, rows: prev.rows.map((row) => (row.kind === kind ? { ...row, ...patch } : row)) } : prev
    );
  };

  const toggleFixedTime = (time: string) => {
    const current = data?.rows.find((row) => row.kind === "monitor_cycle")?.fixed_times ?? [];
    const next = current.includes(time) ? current.filter((item) => item !== time) : [...current, time].sort();
    patchRow("monitor_cycle", { fixed_times: next });
  };

  const save = async () => {
    if (!data) return;
    setSaving(true);
    try {
      const response = await fetch("/api/schedule", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows: data.rows }),
      });
      const result = (await response.json().catch(() => ({}))) as { error?: string };
      if (!response.ok) throw new Error(result.error || "保存失败");
      toast.success("定时配置已保存并生效");
      void load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const daily = data?.rows.find((row) => row.kind === "daily_scan");
  const monitor = data?.rows.find((row) => row.kind === "monitor_cycle");

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">定时任务</h1>
          <p className="text-sm text-muted-foreground">
            由 Web 进程内调度器触发（每 15 秒检查一次），替代 CLI 的 monitor 常驻循环；请勿与 CLI monitor 同时开启。
          </p>
        </div>
        <Button onClick={() => void save()} disabled={saving || !data}>
          <Save className="size-4" /> {saving ? "保存中..." : "保存并生效"}
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>每日扫描</CardTitle>
          <CardDescription>在指定时刻执行一次日线扫描并推送信号（同日只跑一次）。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex items-center gap-6">
            <div className="flex items-center gap-2">
              <Switch
                checked={daily?.enabled ?? false}
                onCheckedChange={(value) => patchRow("daily_scan", { enabled: value })}
              />
              <Label>启用</Label>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>每日扫描时刻（HH:MM，Asia/Shanghai）</Label>
              <Input type="time" className="w-32" value={daily?.time ?? "15:20"} onChange={(event) => patchRow("daily_scan", { time: event.target.value })} />
            </div>
            <div className="flex items-center gap-2">
              <Switch
                checked={daily?.trading_days_only ?? true}
                onCheckedChange={(value) => patchRow("daily_scan", { trading_days_only: value })}
              />
              <Label>仅交易日</Label>
            </div>
          </div>

        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>盘中监控</CardTitle>
          <CardDescription>交易时段（9:30-11:30、13:00-15:00）运行监控循环并推送；执行方式为「间隔」或「固定时点」二选一。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex items-center gap-6">
            <div className="flex items-center gap-2">
              <Switch
                checked={monitor?.enabled ?? false}
                onCheckedChange={(value) => patchRow("monitor_cycle", { enabled: value })}
              />
              <Label>启用</Label>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>监控间隔（秒，最小 10）</Label>
              <Input type="number" className="w-32" value={monitor?.interval_seconds ?? 60} onChange={(event) => patchRow("monitor_cycle", { interval_seconds: Number(event.target.value) })} />
            </div>
            <div className="flex items-center gap-2">
              <Switch
                checked={monitor?.trading_days_only ?? true}
                onCheckedChange={(value) => patchRow("monitor_cycle", { trading_days_only: value })}
              />
              <Label>仅交易日</Label>
            </div>
          </div>
          <div className="flex flex-col gap-2 border-t pt-3">
            <Label>固定时点（可选）</Label>
            <div className="flex items-center gap-2">
              {["10:30", "13:30", "14:30"].map((time) => (
                <Button
                  key={time}
                  type="button"
                  size="sm"
                  variant={(monitor?.fixed_times ?? []).includes(time) ? "default" : "outline"}
                  onClick={() => toggleFixedTime(time)}
                >
                  {time}
                </Button>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">
              勾选后盘中检测改为按这些固定时点各执行一次（不再按间隔执行）；全部取消勾选则恢复为间隔执行。
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>运行状态</CardTitle>
          <CardDescription>基于桥接 calendar 判断，交易日历以数据源为准，失败时回退周一至周五。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          <div className="flex flex-wrap items-center gap-4">
            <span className="text-muted-foreground">当前：</span>
            <Badge variant={data?.calendar.is_trading_day ? "default" : "outline"}>
              {data?.calendar.is_trading_day ? "交易日" : "非交易日"}
            </Badge>
            <Badge variant={data?.calendar.is_trading_session ? "default" : "secondary"}>
              {data?.calendar.is_trading_session ? "交易时段内" : "非交易时段"}
            </Badge>
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-muted-foreground">下次运行（估算）：</span>
            <div className="flex gap-6">
              <span>每日扫描：<code className="font-mono text-xs">{data?.next_runs.daily_scan ?? "已停用"}</code></span>
              <span>盘中监控：<code className="font-mono text-xs">{data?.next_runs.monitor_cycle ?? "已停用"}</code></span>
            </div>
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <CalendarClock className="size-4" />
            最近一次任务与结果请到“结果”页查看；服务重启后调度器自动恢复（同日每日扫描不会重复触发）。
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
