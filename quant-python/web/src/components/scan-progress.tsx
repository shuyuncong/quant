"use client";

import { useEffect, useState } from "react";
import { Button } from "./ui/button";
import { STRATEGIES } from "@/lib/analysis-types";

interface ScanProgressEntry {
  coverage?: number;
  candidate_count?: number;
  completed_round?: boolean;
  batch_size?: number;
  universe_size?: number;
  errors?: unknown[];
}

interface ScanJob {
  id: number;
  status: string;
  error: string | null;
  created_at: string;
  payload: {
    scan_version?: number;
    progress?: Partial<Record<string, ScanProgressEntry>>;
  };
}

const STATUS_LABELS: Record<string, string> = {
  pending: "等待执行",
  running: "执行中",
  success: "完成",
  failed: "未完成",
};

function coveragePercent(value: number | undefined): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  return Math.max(0, Math.min(100, numeric * 100));
}

function ProgressBar({
  label,
  percent,
  size = "sm",
}: {
  label: string;
  percent: number;
  size?: "sm" | "md";
}) {
  const rounded = Math.round(percent);
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={rounded}
      className={`w-full overflow-hidden rounded-full bg-muted ${size === "md" ? "h-2" : "h-1.5"}`}
    >
      <div
        className="h-full bg-primary transition-all duration-300"
        style={{ width: `${percent}%` }}
      />
    </div>
  );
}

export function ScanProgress({ onComplete }: { onComplete?: () => void }) {
  const [job, setJob] = useState<ScanJob | null>(null);
  const [error, setError] = useState("");
  const [retrying, setRetrying] = useState(false);

  useEffect(() => {
    let active = true;
    let previous = "";
    let timer: number | undefined;
    const poll = async () => {
      try {
        const response = await fetch("/api/jobs");
        if (!response.ok) throw new Error("读取筛选进度失败");
        const data = (await response.json()) as { jobs?: ScanJob[] };
        if (!active) return;
        const latest =
          (data.jobs ?? []).find((item) => item.payload.scan_version === 2) ?? null;
        setJob(latest);
        setError("");
        const signature = latest ? `${latest.id}:${latest.status}` : "";
        if (previous && previous !== signature && latest?.status === "success") {
          onComplete?.();
        }
        previous = signature;
      } catch (error) {
        if (active) setError(String(error));
      } finally {
        if (active) timer = window.setTimeout(() => void poll(), 5000);
      }
    };
    void poll();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [onComplete]);

  async function retry() {
    if (!job) return;
    setRetrying(true);
    try {
      const response = await fetch(`/api/jobs/${job.id}/retry`, { method: "POST" });
      const data = (await response.json()) as { error?: string };
      if (!response.ok) throw new Error(data.error);
      setJob({ ...job, status: "pending", error: null });
    } catch (error) {
      setError(String(error));
    } finally {
      setRetrying(false);
    }
  }

  if (!job && !error) return null;

  const progress = job?.payload.progress ?? {};
  const completedCount = STRATEGIES.filter(
    (strategy) => progress[strategy.id]?.completed_round === true
  ).length;
  const activeStrategy = STRATEGIES.find(
    (strategy) => progress[strategy.id]?.completed_round !== true
  );
  const activeCoverage = activeStrategy
    ? coveragePercent(progress[activeStrategy.id]?.coverage)
    : 0;
  const overallPercent =
    job?.status === "success"
      ? 100
      : Math.min(
          100,
          ((completedCount + (activeStrategy ? activeCoverage / 100 : 0)) /
            STRATEGIES.length) *
            100
        );

  return (
    <div
      className="mb-4 space-y-3 rounded border bg-muted/20 p-3 text-xs"
      aria-live="polite"
    >
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      {job && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span>
              最近筛选 #{job.id} · {job.created_at} ·{" "}
              {STATUS_LABELS[job.status] ?? job.status}
            </span>
            {job.status === "failed" && (
              <Button size="sm" variant="outline" disabled={retrying} onClick={() => void retry()}>
                继续未完成筛选
              </Button>
            )}
          </div>

          <div className="space-y-1">
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium">整体进度</span>
              <span>{overallPercent.toFixed(1)}%</span>
            </div>
            <ProgressBar label="整体筛选进度" percent={overallPercent} size="md" />
          </div>

          <div className="grid gap-2 sm:grid-cols-3">
            {STRATEGIES.map((strategy) => {
              const entry = progress[strategy.id];
              const percent = coveragePercent(entry?.coverage);
              const done = entry?.completed_round === true;
              const active = strategy.id === activeStrategy?.id;
              return (
                <div
                  key={strategy.id}
                  className={`rounded border p-2 ${
                    active ? "border-primary/40 bg-background" : "border-transparent"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span>{strategy.name}</span>
                    <span>{done ? "已完成" : `${percent.toFixed(1)}%`}</span>
                  </div>
                  <div className="mt-1">
                    <ProgressBar
                      label={`${strategy.name}进度`}
                      percent={done ? 100 : percent}
                    />
                  </div>
                  <div className="mt-1 text-muted-foreground">
                    候选 {entry?.candidate_count ?? "—"} 只
                    {active ? " · 进行中" : ""}
                  </div>
                </div>
              );
            })}
          </div>

          {job.status !== "success" && (
            <p className="text-muted-foreground">
              候选在整轮完成后才入池；进度按每批 500 只更新。
            </p>
          )}
          {job.error && <p className="text-destructive">{job.error}</p>}
        </>
      )}
    </div>
  );
}
