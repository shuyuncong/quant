"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  AlertCircle,
  BarChart3,
  Calendar,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  History,
  LineChart,
  Play,
  RotateCcw,
  Sparkles,
  TrendingDown,
  TrendingUp,
} from "lucide-react";

interface CurvePoint {
  date: string;
  equity: number;
  cash: number;
  positions: number;
  return_pct: number;
}

interface BacktestResult {
  strategy_id: string;
  name: string;
  metrics: Record<string, number | null>;
  equity_curve: CurvePoint[];
  trades: Array<{
    symbol: string;
    entry_day: string;
    exit_day: string;
    quantity: number;
    pnl_cash: number;
    exit_reason: string;
  }>;
  rejected: Array<{ symbol: string; date: string; reason: string }>;
  open_positions?: number;
  warnings?: string[];
}

interface BacktestReport {
  results: BacktestResult[];
  warnings: string[];
  calendar_days: number;
  excluded: Array<{ symbol: string; reason: string }>;
  universe?: { scope: string; as_of: string; total: number; included: number; excluded: number };
  effective_start?: string;
  effective_end?: string;
  config_snapshot?: Record<string, unknown>;
}

interface BacktestJob {
  id: number;
  status: string;
  created_at: string;
  error: string | null;
  options?: Record<string, unknown> | null;
}

interface Progress {
  stage: string;
  processed: number;
  total: number;
  excluded?: number;
}

const COLORS = ["#2563eb", "#9333ea", "#d97706"];
const DASHES = ["", "8 4", "2 4"];
// 「收益」「回撤」「胜率」三个标签在指标表与对比摘要里各出现一次，抽成常量避免两处措辞漂移。
const TOTAL_RETURN_LABEL = "本样本区间收益";
const ANNUALIZED_LABEL = "年化收益率（折年估算）";
const DRAWDOWN_LABEL = "最大回撤（跌幅幅度）";

const METRIC_COLUMNS = [
  { key: "total_return_pct", name: TOTAL_RETURN_LABEL, percent: true },
  { key: "annualized_return_pct", name: ANNUALIZED_LABEL, percent: true },
  { key: "max_drawdown_pct", name: DRAWDOWN_LABEL, percent: true },
  { key: "sharpe_ratio", name: "夏普比率", percent: false },
  { key: "payoff_ratio", name: "盈亏比", percent: false },
  { key: "win_rate_pct", name: "胜率", percent: true },
  { key: "closed_trades", name: "已平仓笔数", percent: false },
] as const;
const DAILY_PAGE_SIZE = 50;
const STATUS_LABEL: Record<string, string> = {
  success: "已完成",
  failed: "失败",
  running: "运行中",
  pending: "排队中",
};

function getTodayString(): string {
  return new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" });
}

function getPastDateString(years: number): string {
  const date = new Date();
  date.setFullYear(date.getFullYear() - years);
  return date.toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" });
}

function getYtdDateString(): string {
  const date = new Date();
  return `${date.getFullYear()}-01-01`;
}

type Comparable = { value: number; result: BacktestResult };

const MISSING_REASON: Record<string, string> = {
  total_return_pct: "样本权益序列缺失，无法计算区间总收益",
  annualized_return_pct: "样本区间过短或期末权益非正，无法折年估算",
  max_drawdown_pct: "样本权益序列缺失，无法计算最大回撤",
  sharpe_ratio: "样本净值波动为 0 或有效点数不足，无法计算夏普比率",
  payoff_ratio: "缺少同时存在的盈利与亏损平仓交易，无法计算盈亏比",
  win_rate_pct: "本样本没有已平仓交易，无法计算胜率",
};

/** 缺失/非有限值一律不可比：NaN、Infinity 也算「不可计算」，绝不能被排序当成 0。 */
function finiteMetric(result: BacktestResult, key: string): number | null {
  const value = result.metrics[key];
  if (value === null || value === undefined) return null;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** 收益类文本统一带符号：正数才加 +，负数只留一个 −，舍入到 0 时不出现 -0.00。 */
function signedNumberText(value: number, digits = 2): string {
  const rounded = Number(value.toFixed(digits));
  const normalized = rounded === 0 ? 0 : rounded;
  return `${normalized > 0 ? "+" : ""}${normalized.toFixed(digits)}`;
}

function MetricValue({
  metricKey,
  value,
  percent,
  integer = false,
}: {
  metricKey: string;
  value: number | null | undefined;
  percent: boolean;
  integer?: boolean;
}) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return (
      <span
        className="text-muted-foreground"
        title={MISSING_REASON[metricKey] ?? "该指标在当前样本下不可计算"}
      >
        不可计算
      </span>
    );
  }
  const suffix = percent ? "%" : "";

  if (metricKey === "total_return_pct" || metricKey === "annualized_return_pct") {
    const rounded = Number(value.toFixed(2));
    const text = `${signedNumberText(value)}${suffix}`;
    if (rounded > 0) return <span className="font-semibold text-rose-600 dark:text-rose-400">{text}</span>;
    if (rounded < 0) return <span className="font-semibold text-emerald-600 dark:text-emerald-400">{text}</span>;
    return <span className="text-muted-foreground">{text}</span>;
  }
  // 最大回撤是「跌幅幅度」：非负、不带负号；即使拿到负值输入也只展示幅值，避免 -0.00%。
  if (metricKey === "max_drawdown_pct") {
    return (
      <span
        className="font-medium text-emerald-600 dark:text-emerald-400"
        title="回撤幅度（%，非负，越小越好）"
      >
        {Math.abs(value).toFixed(2)}
        {suffix}
      </span>
    );
  }
  // 胜率与盈亏比只呈现数值，不因 >50% 或 >1.5 就自动判定为「优秀」。
  return <span>{`${value.toFixed(integer ? 0 : 2)}${suffix}`}</span>;
}

/** 期末未平仓数量：优先引擎声明值，否则使用最后一个交易日，不用历史峰值冒充。 */
function openPositionCount(result: BacktestResult): number {
  if (typeof result.open_positions === "number" && Number.isFinite(result.open_positions)) {
    return Math.max(0, result.open_positions);
  }
  const last = result.equity_curve.at(-1)?.positions;
  return typeof last === "number" && Number.isFinite(last) ? Math.max(0, last) : 0;
}

type SampleState = "never_traded" | "open_positions" | "closed_trades";

/** 样本状态三分法：未交易 / 已买入未平仓 / 含平仓样本，供排名取池与表格标注共用。 */
function sampleState(result: BacktestResult): SampleState {
  if (result.trades.length > 0 || closedTradeSamples(result) > 0) return "closed_trades";
  return openPositionCount(result) > 0 ? "open_positions" : "never_traded";
}

const SAMPLE_STATE_LABEL: Record<SampleState, string> = {
  never_traded: "未交易",
  open_positions: "已买入未平仓",
  closed_trades: "含平仓样本",
};

/** 已平仓样本数：优先引擎口径，缺失时退回平仓交易明细条数。 */
function closedTradeSamples(result: BacktestResult): number {
  return finiteMetric(result, "closed_trades") ?? result.trades.length;
}

/** 在候选账户里取指标最大/最小的有效值；全部缺失或非有限时返回 null（无可比样本）。 */
function bestByMetric(results: BacktestResult[], key: string, mode: "max" | "min"): Comparable | null {
  let best: Comparable | null = null;
  for (const result of results) {
    const raw = finiteMetric(result, key);
    if (raw === null) continue;
    const value = key === "max_drawdown_pct" ? Math.abs(raw) : raw;
    if (!best || (mode === "max" ? value > best.value : value < best.value)) best = { value, result };
  }
  return best;
}

/** Cumulative return curves share one trading-day axis, so the crosshair always matches a date. */
function EquityChart({ results }: { results: BacktestResult[] }) {
  const dates = results[0]?.equity_curve.map((point) => point.date) ?? [];
  const byDate = results.map(
    (result) => new Map(result.equity_curve.map((point) => [point.date, point.return_pct]))
  );
  let low = 0;
  let high = 1;
  for (const points of byDate) {
    for (const value of points.values()) {
      if (value < low) low = value;
      if (value > high) high = value;
    }
  }
  const span = high - low || 1;
  const x = (index: number) => 65 + (dates.length > 1 ? index / (dates.length - 1) : 0.5) * 810;
  const y = (value: number) => 250 - ((value - low) / span) * 230;

  return (
    <Card className="shadow-none">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <LineChart className="size-4 text-primary" />
            <CardTitle className="text-sm font-semibold">三策略累计收益率曲线</CardTitle>
          </div>
          <div className="flex flex-wrap gap-3 text-xs">
            {results.map((result, index) => (
              <span key={result.strategy_id} className="flex items-center gap-1.5 font-medium">
                <svg width="18" height="8" aria-hidden="true">
                  <line
                    x1="0"
                    y1="4"
                    x2="18"
                    y2="4"
                    stroke={COLORS[index]}
                    strokeWidth="2.5"
                    strokeDasharray={DASHES[index]}
                  />
                </svg>
                {result.name}
              </span>
            ))}
          </div>
        </div>
      </CardHeader>
      <CardContent className="pt-2">
        {!dates.length ? (
          <p className="p-8 text-center text-sm text-muted-foreground">暂无收益曲线数据</p>
        ) : (
          <div className="w-full overflow-hidden">
            <svg
              role="img"
              aria-label="三个策略的累计收益曲线，具体数值见逐日收益表"
              viewBox="0 0 900 300"
              className="w-full text-foreground"
            >
              <title>三策略累计收益率（%）</title>
              {[0, 0.25, 0.5, 0.75, 1].map((fraction) => (
                <g key={fraction}>
                  <line
                    x1="65"
                    y1={20 + fraction * 230}
                    x2="875"
                    y2={20 + fraction * 230}
                    stroke="currentColor"
                    opacity=".1"
                  />
                  <text
                    x="58"
                    y={24 + fraction * 230}
                    textAnchor="end"
                    fontSize="11"
                    fill="currentColor"
                    className="font-mono text-[10px] text-muted-foreground"
                  >
                    {(high - fraction * span).toFixed(1)}%
                  </text>
                </g>
              ))}
              {results.map((result, index) => {
                const points = dates
                  .map((date, dateIndex) => {
                    const value = byDate[index].get(date);
                    return value === undefined ? null : `${x(dateIndex)},${y(value)}`;
                  })
                  .filter((value): value is string => value !== null);
                return (
                  <g key={result.strategy_id}>
                    {points.length === 1 && (
                      <circle
                        cx={points[0].split(",")[0]}
                        cy={points[0].split(",")[1]}
                        r="3"
                        fill={COLORS[index]}
                      />
                    )}
                    <polyline
                      fill="none"
                      stroke={COLORS[index]}
                      strokeWidth="2.2"
                      strokeDasharray={DASHES[index]}
                      points={points.join(" ")}
                    />
                  </g>
                );
              })}
              <text x="65" y="280" fontSize="11" fill="currentColor" className="font-mono text-[10px] text-muted-foreground">
                {dates[0]}
              </text>
              <text x="875" y="280" textAnchor="end" fontSize="11" fill="currentColor" className="font-mono text-[10px] text-muted-foreground">
                {dates.at(-1)}
              </text>
            </svg>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** Daily cumulative returns, paged so a 500-day market-wide run stays readable. */
function DailyReturnsTable({ results }: { results: BacktestResult[] }) {
  const [page, setPage] = useState(1);
  const dates = results[0]?.equity_curve.map((point) => point.date) ?? [];
  const byDate = results.map(
    (result) => new Map(result.equity_curve.map((point) => [point.date, point.return_pct]))
  );
  const pageCount = Math.max(1, Math.ceil(dates.length / DAILY_PAGE_SIZE));
  const current = Math.min(page, pageCount);
  const rows = dates.slice((current - 1) * DAILY_PAGE_SIZE, current * DAILY_PAGE_SIZE);

  if (!dates.length) return null;

  return (
    <details className="group rounded-xl border bg-card p-4 transition-all">
      <summary className="flex cursor-pointer items-center justify-between text-sm font-medium">
        <span className="flex items-center gap-2">
          <Calendar className="size-4 text-muted-foreground" />
          逐日累计收益率明细（共 {dates.length} 个交易日）
        </span>
        <span className="text-xs text-muted-foreground group-open:rotate-180 transition-transform">
          ▼
        </span>
      </summary>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b bg-muted/30">
              <th className="p-2.5 text-left font-medium text-muted-foreground">日期</th>
              {results.map((result) => (
                <th key={result.strategy_id} className="p-2.5 text-right font-medium">
                  {result.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y">
            {rows.map((date) => (
              <tr key={date} className="transition-colors hover:bg-muted/20">
                <td className="p-2 font-mono text-muted-foreground">{date}</td>
                {results.map((result, resultIndex) => {
                  const value = byDate[resultIndex].get(date);
                  return (
                    <td
                      key={result.strategy_id}
                      className={`p-2 text-right font-mono tabular-nums ${
                        value === undefined || !Number.isFinite(value)
                          ? "text-muted-foreground"
                          : value > 0
                          ? "font-medium text-rose-600 dark:text-rose-400"
                          : value < 0
                          ? "font-medium text-emerald-600 dark:text-emerald-400"
                          : "text-muted-foreground"
                      }`}
                    >
                      {value === undefined || !Number.isFinite(value)
                        ? "不可计算"
                        : `${signedNumberText(value)}%`}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
        <span>共 {dates.length} 个交易日 · 每页 {DAILY_PAGE_SIZE} 条</span>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            className="h-7 px-2"
            disabled={current === 1}
            onClick={() => setPage(current - 1)}
          >
            <ChevronLeft className="size-3.5" />
          </Button>
          <span className="font-mono">{current} / {pageCount}</span>
          <Button
            size="sm"
            variant="outline"
            className="h-7 px-2"
            disabled={current * DAILY_PAGE_SIZE >= dates.length}
            onClick={() => setPage(current + 1)}
          >
            <ChevronRight className="size-3.5" />
          </Button>
        </div>
      </div>
    </details>
  );
}

export default function BacktestPage() {
  const [mode, setMode] = useState<"stock" | "market">("stock");
  const [symbol, setSymbol] = useState("");
  const [start, setStart] = useState("2023-05-01");
  const [end, setEnd] = useState(() => getTodayString());
  const [cash, setCash] = useState("100000");
  const [positions, setPositions] = useState("4");
  const [size, setSize] = useState("25");
  const [available, setAvailable] = useState<boolean | null>(null);
  const [jobs, setJobs] = useState<BacktestJob[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [report, setReport] = useState<BacktestReport | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const startGuard = useRef(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/backtests");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error ?? "加载回测失败");
      setAvailable(data.available);
      setJobs((prev) => {
        const next = data.jobs as BacktestJob[];
        if (
          prev.length === next.length &&
          prev.every((p, i) => p.id === next[i]?.id && p.status === next[i]?.status)
        ) {
          return prev;
        }
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void load(), 0);
    const interval = setInterval(() => {
      if (typeof document === "undefined" || document.visibilityState !== "hidden") {
        void load();
      }
    }, 5000);
    return () => {
      clearTimeout(timer);
      clearInterval(interval);
    };
  }, [load]);

  const select = useCallback((id: number | null) => {
    setSelected(id);
    setReport(null);
    setProgress(null);
    setError("");
  }, []);

  useEffect(() => {
    if (!selected) return;
    const abort = new AbortController();
    const version = { current: 0 };
    let timer: ReturnType<typeof setInterval> | undefined;
    const loadReport = async () => {
      const current = ++version.current;
      try {
        const response = await fetch(`/api/backtests/${selected}`, { signal: abort.signal });
        const data = await response.json();
        if (abort.signal.aborted || current !== version.current) return;
        if (!response.ok) throw new Error(data.error ?? "读取回测失败");
        setReport(data.report);
        setProgress(data.progress);
        setError(data.job.error ?? "");
        if (["success", "failed"].includes(data.job.status) && timer) {
          clearInterval(timer);
          timer = undefined;
        }
      } catch (err) {
        if (!abort.signal.aborted && current === version.current)
          setError(err instanceof Error ? err.message : String(err));
      }
    };
    void loadReport();
    timer = setInterval(() => void loadReport(), 5000);
    return () => {
      abort.abort();
      if (timer) clearInterval(timer);
    };
  }, [selected]);

  const run = async () => {
    if (startGuard.current) return;
    startGuard.current = true;
    setStarting(true);
    try {
      const response = await fetch("/api/backtests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode,
          symbols: mode === "stock" ? [symbol.trim()] : [],
          start,
          end,
          initial_cash: Number(cash),
          max_positions: Number(positions),
          position_size_pct: Number(size) / 100,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error ?? "提交回测失败");
      select(Number(data.jobId));
      toast.success("回测已进入任务队列");
      void load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      startGuard.current = false;
      setStarting(false);
    }
  };

  const retry = async () => {
    if (!selected || startGuard.current) return;
    startGuard.current = true;
    try {
      const response = await fetch(`/api/backtests/${selected}`, { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error ?? "重试失败");
      toast.success("已从冻结输入重新排队");
      setError("");
      void load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      startGuard.current = false;
    }
  };

  const setPresetRange = (type: "1y" | "2y" | "3y" | "ytd") => {
    const today = getTodayString();
    setEnd(today);
    if (type === "1y") setStart(getPastDateString(1));
    else if (type === "2y") setStart(getPastDateString(2));
    else if (type === "3y") setStart(getPastDateString(3));
    else if (type === "ytd") setStart(getYtdDateString());
  };

  const selectedJob = jobs.find((job) => job.id === selected);

  // 高光对比只在「真正持有过仓位」的账户之间排名：从未交易的纯现金账户
  // 年化/回撤恒为 0，混进来会假装成「收益最高 / 回撤最低」。
  const highlights = useMemo(() => {
    const results = report?.results ?? [];
    if (!results.length) return null;
    const traded = results.filter((result) => sampleState(result) !== "never_traded");
    const neverTraded = results.filter((result) => sampleState(result) === "never_traded");
    const closedSampleTotal = traded.reduce((sum, result) => sum + closedTradeSamples(result), 0);

    // 只比较真实区间收益；缺失时不可拿年化折算值冒充。
    const returnPool = traded.filter((result) => finiteMetric(result, "total_return_pct") !== null);
    const returnRanking = bestByMetric(returnPool, "total_return_pct", "max");
    const lossOnly = returnRanking !== null && returnRanking.value < 0;
    const bestReturn = returnRanking?.result ?? null;
    const bestDrawdown = bestByMetric(traded, "max_drawdown_pct", "min");
    // 胜率只认「有已平仓样本且胜率有效」的账户：全缺失即无可比样本。
    const winRatePool = traded.filter(
      (result) => closedTradeSamples(result) > 0 && finiteMetric(result, "win_rate_pct") !== null
    );
    const bestWinRate = bestByMetric(winRatePool, "win_rate_pct", "max");

    return {
      traded,
      neverTraded,
      closedSampleTotal,
      bestReturn,
      bestReturnValue: returnRanking?.value ?? null,
      returnPoolSize: returnPool.length,
      bestDrawdown,
      bestWinRate,
      lossOnly,
      drawdownPoolSize: traded.filter((result) => finiteMetric(result, "max_drawdown_pct") !== null).length,
      winRatePoolSize: winRatePool.length,
    };
  }, [report]);

  return (
    <div className="space-y-6">
      {/* 头部标题与风险提示 */}
      <div>
        <div className="flex items-center gap-2">
          <BarChart3 className="size-6 text-primary" />
          <h1 className="text-xl font-bold tracking-tight">策略回测看板</h1>
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          基于相同历史区间与一致资金约束，对多套量化策略进行收益曲线与风险指标横向对比。全市场口径回测存在幸存者偏差。
        </p>
      </div>

      {available === false && (
        <div className="flex items-center gap-2 rounded-xl border border-amber-500/20 bg-amber-500/10 p-4 text-sm text-amber-700 dark:text-amber-400">
          <AlertCircle className="size-4 shrink-0" />
          <span>线上回测服务未启用（BACKTEST_ENABLED=1）；您仍可自由查阅历史任务报告与指标数据。</span>
        </div>
      )}

      {/* 参数配置卡片 */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle className="text-base font-semibold">发起回测任务</CardTitle>
            <Tabs
              value={mode}
              onValueChange={(val) => setMode(val as "stock" | "market")}
              className="w-auto"
            >
              <TabsList className="h-8">
                <TabsTrigger value="stock" className="px-3 text-xs">个股回测</TabsTrigger>
                <TabsTrigger value="market" className="px-3 text-xs">全市场回测</TabsTrigger>
              </TabsList>
            </Tabs>
          </div>
          <CardDescription className="text-xs">
            {mode === "stock"
              ? "针对特定标的进行精准回溯与交易点诊断。"
              : "基于在市全部 A 股进行全样本回测，需耗费较长时间。"}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {mode === "stock" && (
            <div className="max-w-xs space-y-1">
              <Label htmlFor="backtest-symbol" className="text-xs font-medium">股票代码</Label>
              <Input
                id="backtest-symbol"
                placeholder="600036 或 600036.SH"
                className="font-mono text-sm"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
              />
            </div>
          )}

          {/* 快捷区间预设芯片 */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="flex items-center gap-1 text-xs text-muted-foreground">
              <Sparkles className="size-3 text-primary" />
              快捷区间：
            </span>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={() => setPresetRange("1y")}
            >
              近 1 年
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={() => setPresetRange("2y")}
            >
              近 2 年
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={() => setPresetRange("3y")}
            >
              近 3 年
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={() => setPresetRange("ytd")}
            >
              今年以来 (YTD)
            </Button>
          </div>

          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              void run();
            }}
          >
            {[
              { id: "start", label: "开始日期", value: start, set: setStart, type: "date" },
              { id: "end", label: "结束日期", value: end, set: setEnd, type: "date" },
              { id: "cash", label: "初始资金（元）", value: cash, set: setCash, type: "number" },
              { id: "positions", label: "最大持仓数", value: positions, set: setPositions, type: "number" },
              { id: "size", label: "单笔仓位上限（%）", value: size, set: setSize, type: "number" },
            ].map((field) => (
              <div key={field.id} className="space-y-1">
                <Label htmlFor={`bt-${field.id}`} className="text-xs font-medium">
                  {field.label}
                </Label>
                <Input
                  className="w-36 font-mono text-sm"
                  id={`bt-${field.id}`}
                  type={field.type}
                  required
                  value={field.value}
                  onChange={(e) => field.set(e.target.value)}
                />
              </div>
            ))}
            <Button
              type="submit"
              disabled={
                available !== true ||
                starting ||
                (mode === "stock" && !symbol.trim())
              }
              className="min-w-36"
            >
              <Play className="mr-1.5 size-4" />
              {starting ? "提交计算中…" : "运行三策略回测"}
            </Button>
          </form>
        </CardContent>
      </Card>

      {/* 历史任务与进度状态条 */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border bg-card p-3 shadow-sm">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-1.5 text-sm font-medium">
            <History className="size-4 text-muted-foreground" />
            <Label htmlFor="backtest-history" className="text-sm font-medium">历史回测任务：</Label>
          </div>
          <select
            id="backtest-history"
            className="h-9 min-w-72 rounded-lg border bg-background px-3 font-mono text-sm shadow-none transition-colors"
            value={selected ?? ""}
            onChange={(e) => select(Number(e.target.value) || null)}
          >
            <option value="">-- 选择回测任务记录 --</option>
            {jobs.map((job) => (
              <option key={job.id} value={job.id}>
                #{job.id} · {job.created_at} · {STATUS_LABEL[job.status] ?? job.status}
              </option>
            ))}
          </select>

          {selectedJob && (
            <Badge
              variant={
                selectedJob.status === "success"
                  ? "default"
                  : selectedJob.status === "failed"
                  ? "destructive"
                  : "secondary"
              }
              className="gap-1 px-2.5 py-0.5 text-xs font-medium"
            >
              {selectedJob.status === "running" && (
                <span className="relative flex size-2">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
                  <span className="relative inline-flex size-2 rounded-full bg-emerald-500" />
                </span>
              )}
              {STATUS_LABEL[selectedJob.status] ?? selectedJob.status}
            </Badge>
          )}

          {selectedJob?.status === "failed" && available === true && (
            <Button
              variant="outline"
              size="sm"
              className="h-8 gap-1.5 text-xs"
              disabled={starting}
              onClick={() => void retry()}
            >
              <RotateCcw className="size-3.5" />
              从冻结输入重试
            </Button>
          )}
        </div>

        {progress && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <div className="h-1.5 w-24 overflow-hidden rounded-full bg-muted">
              <div
                className="h-full bg-primary transition-all duration-300"
                style={{
                  width: `${Math.min(
                    100,
                    progress.total > 0 ? (progress.processed / progress.total) * 100 : 0
                  )}%`,
                }}
              />
            </div>
            <span>
              {progress.stage} · {progress.processed}/{progress.total}
            </span>
          </div>
        )}
      </div>

      {error && (
        <div
          role="alert"
          className="flex items-center gap-2 rounded-xl border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
        >
          <AlertCircle className="size-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* 回测报告区域 */}
      {report && (
        <div className="space-y-6">
          {/* 样本与区间元数据摘要 */}
          <div className="rounded-xl border bg-muted/30 px-4 py-2.5 text-xs text-muted-foreground">
            {report.universe ? (
              <p>
                样本名单基准：{report.universe.as_of} · 全市场样本：{report.universe.total} 只（纳入 {report.universe.included}、排除 {report.universe.excluded}）· 实际交易区间：{report.effective_start} 至 {report.effective_end}（共 {report.calendar_days} 个交易日）
              </p>
            ) : (
              <p>历史记录未内嵌全市场样本元数据 · 覆盖 {report.calendar_days} 个交易日</p>
            )}
          </div>

          {/* 核心指标三对比卡片：只比「真正持有过仓位」的账户，并标注已平仓样本量 */}
          {highlights && (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <Card className="bg-gradient-to-br from-muted/40 to-transparent">
                <CardHeader className="p-4 pb-1">
                  <div className="flex items-center justify-between gap-2">
                    <CardTitle className="text-xs font-semibold">
                      {highlights.bestReturn
                        ? highlights.lossOnly
                          ? "本样本亏损最少"
                          : "本样本区间收益最高"
                        : "区间收益对比"}
                    </CardTitle>
                    {highlights.bestReturn &&
                      (highlights.lossOnly
                        ? <TrendingDown className="size-4 shrink-0 text-muted-foreground" />
                        : <TrendingUp className="size-4 shrink-0 text-rose-500/70" />)}
                  </div>
                </CardHeader>
                <CardContent className="p-4 pt-1">
                  {highlights.bestReturn ? (
                    <>
                      <div
                        className={`font-mono text-2xl font-bold tracking-tight ${
                          highlights.lossOnly
                            ? "text-emerald-600 dark:text-emerald-400"
                            : "text-rose-600 dark:text-rose-400"
                        }`}
                      >
                        {highlights.bestReturnValue === null
                          ? "不可计算"
                          : `${signedNumberText(highlights.bestReturnValue)}%`}
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground truncate">
                        <span className="font-medium text-foreground">{highlights.bestReturn.name}</span>
                        {` · 已平仓 ${closedTradeSamples(highlights.bestReturn)} 笔 · `}
                        {TOTAL_RETURN_LABEL}
                      </p>
                      <p className="mt-1 text-[11px] text-muted-foreground">
                        收益口径 · {highlights.returnPoolSize} 个账户有可比较区间收益
                      </p>
                    </>
                  ) : (
                    <>
                      <div className="font-mono text-2xl font-bold tracking-tight text-muted-foreground">不可计算</div>
                      <p className="mt-1 text-[11px] text-muted-foreground">
                        无人持有过仓位，或全部账户的收益指标缺失，无收益可比样本。
                      </p>
                    </>
                  )}
                </CardContent>
              </Card>

              <Card className="bg-gradient-to-br from-emerald-500/5 to-transparent border-emerald-500/20">
                <CardHeader className="p-4 pb-1">
                  <div className="flex items-center justify-between gap-2">
                    <CardTitle className="text-xs font-semibold text-emerald-600 dark:text-emerald-400">
                      本样本最低回撤
                    </CardTitle>
                    {highlights.bestDrawdown && <TrendingDown className="size-4 shrink-0 text-emerald-500/70" />}
                  </div>
                </CardHeader>
                <CardContent className="p-4 pt-1">
                  {highlights.bestDrawdown ? (
                    <>
                      <div className="font-mono text-2xl font-bold tracking-tight text-emerald-600 dark:text-emerald-400">
                        {Math.abs(highlights.bestDrawdown.value).toFixed(2)}%
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground truncate">
                        <span className="font-medium text-foreground">{highlights.bestDrawdown.result.name}</span>
                        {` · 已平仓 ${closedTradeSamples(highlights.bestDrawdown.result)} 笔 · `}
                        {DRAWDOWN_LABEL}
                      </p>
                      <p className="mt-1 text-[11px] text-muted-foreground">
                        回撤越小越好，不等于风控更优或波动更低 · 参与对比 {highlights.drawdownPoolSize} 个账户 · 已平仓样本合计 {highlights.closedSampleTotal} 笔
                      </p>
                    </>
                  ) : (
                    <>
                      <div className="font-mono text-2xl font-bold tracking-tight text-muted-foreground">不可计算</div>
                      <p className="mt-1 text-[11px] text-muted-foreground">
                        无人持有过仓位，或全部账户的回撤指标缺失，无回撤可比样本。
                      </p>
                    </>
                  )}
                </CardContent>
              </Card>

              <Card className="bg-gradient-to-br from-blue-500/5 to-transparent border-blue-500/20">
                <CardHeader className="p-4 pb-1">
                  <div className="flex items-center justify-between gap-2">
                    <CardTitle className="text-xs font-semibold text-blue-600 dark:text-blue-400">
                      本样本胜率对比
                    </CardTitle>
                    {highlights.bestWinRate && <CheckCircle2 className="size-4 shrink-0 text-blue-500/70" />}
                  </div>
                </CardHeader>
                <CardContent className="p-4 pt-1">
                  {highlights.bestWinRate ? (
                    <>
                      <div className="font-mono text-2xl font-bold tracking-tight text-blue-600 dark:text-blue-400">
                        {`${highlights.bestWinRate.value.toFixed(2)}%`}
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground truncate">
                        <span className="font-medium text-foreground">{highlights.bestWinRate.result.name}</span>
                        {` · 已平仓 ${closedTradeSamples(highlights.bestWinRate.result)} 笔 · 最高胜率（仅统计已平仓交易）`}
                      </p>
                      <p className="mt-1 text-[11px] text-muted-foreground">
                        参与对比 {highlights.winRatePoolSize} 个有平仓样本的账户 · 各自已平仓笔数见下表 · 胜率高不代表收益高
                      </p>
                    </>
                  ) : (
                    <>
                      <div className="font-mono text-2xl font-bold tracking-tight text-muted-foreground">{highlights.closedSampleTotal > 0 ? "不可计算" : "无平仓样本"}</div>
                      <p className="mt-1 text-[11px] text-muted-foreground">
                        没有账户产生可计算的已平仓交易胜率，无法比较。
                      </p>
                    </>
                  )}
                </CardContent>
              </Card>

              <p className="text-[11px] leading-relaxed text-muted-foreground sm:col-span-3">
                排名口径：仅对实际交易且对应指标有效的账户比较；共 {highlights.traded.length} 个账户有交易记录，已平仓样本合计 {highlights.closedSampleTotal} 笔。
                {highlights.neverTraded.length > 0 &&
                  `未交易的纯现金账户 ${highlights.neverTraded.length} 个（${highlights.neverTraded
                    .map((result) => result.name)
                    .join("、")}）权益恒等于初始现金，不参与排名。`}
                {highlights.lossOnly && "可计算区间收益的交易账户均为亏损，收益卡片只表示这些账户中亏损最少的一项。"}
              </p>
            </div>
          )}

          {/* 策略横向对比表格 */}
          <div className="overflow-x-auto rounded-xl border bg-card">
            <table className="w-full min-w-[720px] text-sm">
              <thead className="bg-muted/40">
                <tr>
                  <th className="p-3 text-left font-semibold">策略名称</th>
                  {METRIC_COLUMNS.map((column) => (
                    <th key={column.key} className="p-3 text-right font-semibold">
                      {column.name}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {report.results.map((result) => {
                  const state = sampleState(result);
                  return (
                    <tr key={result.strategy_id} className="transition-colors hover:bg-muted/10">
                      <th className="p-3 text-left font-medium">
                        <span className="flex flex-wrap items-center gap-1.5">
                          {result.name}
                          <Badge
                            variant="outline"
                            className={`text-[10px] font-normal ${
                              state === "never_traded"
                                ? "border-muted-foreground/30 text-muted-foreground"
                                : "border-primary/20 text-primary"
                            }`}
                          >
                            {SAMPLE_STATE_LABEL[state]}
                          </Badge>
                        </span>
                        {state !== "closed_trades" && (
                          <span className="mt-0.5 block text-[11px] font-normal text-muted-foreground">
                            {state === "never_traded"
                              ? "未买入任何标的，权益为纯现金"
                              : `已买入未平仓 ${openPositionCount(result)} 只，胜率/盈亏比缺少可统计样本`}
                          </span>
                        )}
                      </th>
                      {METRIC_COLUMNS.map((column) => (
                        <td key={column.key} className="p-3 text-right font-mono tabular-nums">
                          <MetricValue
                            metricKey={column.key}
                            value={result.metrics[column.key]}
                            percent={column.percent}
                            integer={column.key === "closed_trades"}
                          />
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* 累计收益率曲线 */}
          <EquityChart results={report.results} />

          <p className="text-xs text-muted-foreground">
            指标口径说明：「不可计算」表示当前样本下没有有效数值；未提供区间总收益时保持缺失，不用年化收益替代。
            {TOTAL_RETURN_LABEL}为样本区间权益变化，{ANNUALIZED_LABEL}由同一区间折算全年，区间越短越不可信；{DRAWDOWN_LABEL}为区间内自峰值的最大跌幅，非负、越小越好。
            盈亏比采用已平仓交易净金额，夏普无风险利率取 0，区间末未平仓持仓按收盘市值计入权益；胜率与盈亏比仅统计已平仓交易，样本量见「已平仓笔数」列。
            「未交易」账户区间内未买入任何标的，权益恒为纯现金；「已买入未平仓」账户缺少已平仓样本，不应与其他账户比较胜率与盈亏比。
          </p>

          {/* 警告信息 */}
          {report.results.some((r) => r.warnings?.length) && (
            <div className="space-y-1 rounded-xl border border-amber-500/20 bg-amber-500/5 p-3">
              {report.results
                .flatMap((result) => result.warnings ?? [])
                .map((warning, index) => (
                  <p key={index} className="text-xs text-amber-600 dark:text-amber-400">
                    ⚠️ {warning}
                  </p>
                ))}
            </div>
          )}

          {/* 逐日收益与交易明细折叠面板 */}
          <DailyReturnsTable results={report.results} />

          <details className="group rounded-xl border bg-card p-4 transition-all">
            <summary className="flex cursor-pointer items-center justify-between text-sm font-medium">
              <span>成交明细与标的排除清单</span>
              <span className="text-xs text-muted-foreground group-open:rotate-180 transition-transform">
                ▼
              </span>
            </summary>
            <div className="mt-4 max-h-96 space-y-6 overflow-auto pr-2">
              {report.results.map((result) => (
                <div key={result.strategy_id} className="space-y-2">
                  <h3 className="text-sm font-semibold">{result.name}</h3>
                  {!result.trades.length ? (
                    <p className="text-xs text-muted-foreground">
                      {sampleState(result) === "never_traded"
                        ? "该策略在选定区间内未买入任何标的，因此没有平仓交易（纯现金账户）。"
                        : `该策略在选定区间内没有平仓交易：已买入未平仓 ${openPositionCount(result)} 只，胜率与盈亏比缺少可统计样本。`}
                    </p>
                  ) : (
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b bg-muted/20">
                          {["股票", "买入日", "卖出日", "数量", "净盈亏", "退出原因"].map(
                            (name) => (
                              <th className="p-2 text-left font-medium text-muted-foreground" key={name}>
                                {name}
                              </th>
                            )
                          )}
                        </tr>
                      </thead>
                      <tbody className="divide-y">
                        {result.trades.map((trade, index) => (
                          <tr key={index} className="transition-colors hover:bg-muted/20">
                            <td className="p-2 font-mono font-medium">{trade.symbol}</td>
                            <td>{trade.entry_day}</td>
                            <td>{trade.exit_day}</td>
                            <td className="font-mono tabular-nums">{trade.quantity}</td>
                            <td
                              className={`font-mono font-semibold tabular-nums ${
                                trade.pnl_cash > 0
                                  ? "text-rose-600 dark:text-rose-400"
                                  : trade.pnl_cash < 0
                                  ? "text-emerald-600 dark:text-emerald-400"
                                  : "text-muted-foreground"
                              }`}
                            >
                              {trade.pnl_cash > 0 ? `+${trade.pnl_cash.toFixed(2)}` : trade.pnl_cash.toFixed(2)}
                            </td>
                            <td>
                              <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
                                {trade.exit_reason}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  {result.rejected.map((row, index) => (
                    <p key={index} className="text-xs text-muted-foreground">
                      🚫 {row.date} {row.symbol}：{row.reason}
                    </p>
                  ))}
                </div>
              ))}
              {report.excluded.map((row, index) => (
                <p key={index} className="text-xs text-muted-foreground">
                  排除标的 {row.symbol}：{row.reason}
                </p>
              ))}
            </div>
          </details>
        </div>
      )}
    </div>
  );
}
