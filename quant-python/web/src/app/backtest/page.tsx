"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

interface CurvePoint { date: string; equity: number; cash: number; positions: number; return_pct: number }
interface BacktestResult {
  strategy_id: string;
  name: string;
  metrics: Record<string, number | null>;
  equity_curve: CurvePoint[];
  trades: Array<{ symbol: string; entry_day: string; exit_day: string; quantity: number; pnl_cash: number; exit_reason: string }>;
  rejected: Array<{ symbol: string; date: string; reason: string }>;
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
interface BacktestJob { id: number; status: string; created_at: string; error: string | null; options?: Record<string, unknown> | null }
interface Progress { stage: string; processed: number; total: number; excluded?: number }

const COLORS = ["#2563eb", "#9333ea", "#d97706"];
// Line style carries the series identity as well, so colour is not the only cue.
const DASHES = ["", "8 4", "2 4"];
const METRIC_COLUMNS = [
  { key: "annualized_return_pct", name: "年化收益率", percent: true },
  { key: "max_drawdown_pct", name: "最大回撤", percent: true },
  { key: "sharpe_ratio", name: "夏普比率", percent: false },
  { key: "payoff_ratio", name: "盈亏比", percent: false },
  { key: "win_rate_pct", name: "胜率", percent: true },
  { key: "closed_trades", name: "已平仓笔数", percent: false },
] as const;
const DAILY_PAGE_SIZE = 50;
const STATUS_LABEL: Record<string, string> = { success: "已完成", failed: "失败", running: "运行中", pending: "等待中" };

function formatMetric(value: number | null | undefined, percent: boolean, integer = false): string {
  if (value === null || value === undefined) return "N/A";
  return `${value.toFixed(integer ? 0 : 2)}${percent ? "%" : ""}`;
}

/** Cumulative return curves share one trading-day axis, so the crosshair always matches a date. */
function EquityChart({ results }: { results: BacktestResult[] }) {
  const dates = results[0]?.equity_curve.map((point) => point.date) ?? [];
  const byDate = results.map((result) => new Map(result.equity_curve.map((point) => [point.date, point.return_pct])));
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
    <div className="rounded border p-3">
      <div className="mb-3 flex flex-wrap gap-4 text-sm">
        {results.map((result, index) => (
          <span key={result.strategy_id} className="flex items-center gap-2">
            <svg width="20" height="8" aria-hidden="true"><line x1="0" y1="4" x2="20" y2="4" stroke={COLORS[index]} strokeWidth="2" strokeDasharray={DASHES[index]} /></svg>
            {result.name}
          </span>
        ))}
      </div>
      {!dates.length ? (
        <p className="p-6 text-center text-sm text-muted-foreground">暂无收益曲线</p>
      ) : (
        <svg role="img" aria-label="三个策略的累计收益曲线，具体数值见逐日收益表" viewBox="0 0 900 300" className="w-full">
          <title>三策略累计收益率（%）</title>
          {[0, .25, .5, .75, 1].map((fraction) => (
            <g key={fraction}>
              <line x1="65" y1={20 + fraction * 230} x2="875" y2={20 + fraction * 230} stroke="currentColor" opacity=".12" />
              <text x="58" y={24 + fraction * 230} textAnchor="end" fontSize="11" fill="currentColor">{(high - fraction * span).toFixed(1)}%</text>
            </g>
          ))}
          {results.map((result, index) => {
            const points = dates.map((date, dateIndex) => {
              const value = byDate[index].get(date);
              return value === undefined ? null : `${x(dateIndex)},${y(value)}`;
            }).filter((value): value is string => value !== null);
            return (
              <g key={result.strategy_id}>
                {points.length === 1 && <circle cx={points[0].split(",")[0]} cy={points[0].split(",")[1]} r="3" fill={COLORS[index]} />}
                <polyline fill="none" stroke={COLORS[index]} strokeWidth="2" strokeDasharray={DASHES[index]} points={points.join(" ")} />
              </g>
            );
          })}
          <text x="65" y="278" fontSize="11" fill="currentColor">{dates[0]}</text>
          <text x="875" y="278" textAnchor="end" fontSize="11" fill="currentColor">{dates.at(-1)}</text>
        </svg>
      )}
    </div>
  );
}

/** Daily cumulative returns, paged so a 500-day market-wide run stays readable. */
function DailyReturnsTable({ results }: { results: BacktestResult[] }) {
  const [page, setPage] = useState(1);
  const dates = results[0]?.equity_curve.map((point) => point.date) ?? [];
  const byDate = results.map((result) => new Map(result.equity_curve.map((point) => [point.date, point.return_pct])));
  const pageCount = Math.max(1, Math.ceil(dates.length / DAILY_PAGE_SIZE));
  const current = Math.min(page, pageCount);
  const rows = dates.slice((current - 1) * DAILY_PAGE_SIZE, current * DAILY_PAGE_SIZE);
  if (!dates.length) return null;
  return (
    <details className="rounded border p-3">
      <summary className="cursor-pointer text-sm">逐日累计收益率（{dates.length} 个交易日）</summary>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-xs">
          <thead><tr><th className="p-2 text-left">日期</th>{results.map((result) => <th key={result.strategy_id} className="p-2 text-right">{result.name}</th>)}</tr></thead>
          <tbody>
            {rows.map((date) => (
              <tr key={date} className="border-t">
                <td className="p-2">{date}</td>
                {results.map((result, resultIndex) => {
                  const value = byDate[resultIndex].get(date);
                  return <td key={result.strategy_id} className="p-2 text-right tabular-nums">{value === undefined ? "N/A" : `${value.toFixed(2)}%`}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
        <span>共 {dates.length} 行 · 每页 {DAILY_PAGE_SIZE} 行</span>
        <div className="flex items-center gap-3">
          <Button size="sm" variant="outline" disabled={current === 1} onClick={() => setPage(current - 1)}>上一页</Button>
          <span>{current} / {pageCount}</span>
          <Button size="sm" variant="outline" disabled={current * DAILY_PAGE_SIZE >= dates.length} onClick={() => setPage(current + 1)}>下一页</Button>
        </div>
      </div>
    </details>
  );
}

export default function BacktestPage() {
  const [mode, setMode] = useState<"stock" | "market">("stock");
  const [symbol, setSymbol] = useState("");
  const [start, setStart] = useState("2023-05-01");
  const [end, setEnd] = useState(() => new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" }));
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
  // A ref guard closes the gap between the click and React re-rendering the disabled button.
  const startGuard = useRef(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/backtests");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error ?? "加载回测失败");
      setAvailable(data.available);
      setJobs(data.jobs as BacktestJob[]);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void load(), 0);
    const interval = setInterval(() => void load(), 5000);
    return () => { clearTimeout(timer); clearInterval(interval); };
  }, [load]);

  // Selecting a task resets every piece of the previous task's view at once.
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
        // Finished tasks stop polling; the list still refreshes every 5 seconds.
        if (["success", "failed"].includes(data.job.status) && timer) {
          clearInterval(timer);
          timer = undefined;
        }
      } catch (err) {
        if (!abort.signal.aborted && current === version.current) setError(err instanceof Error ? err.message : String(err));
      }
    };
    void loadReport();
    timer = setInterval(() => void loadReport(), 5000);
    return () => { abort.abort(); if (timer) clearInterval(timer); };
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

  const selectedJob = jobs.find((job) => job.id === selected);
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">策略回测</h1>
        <p className="mt-1 text-sm text-muted-foreground">同一历史区间、相同资金约束，比较三套策略的表现。全市场口径为“当前在市全 A 股历史回放”，不含完整历史退市样本及历史 ST 状态，存在幸存者偏差。</p>
      </div>
      {available === false && <p className="rounded border bg-muted p-4 text-sm">线上回测未启用（BACKTEST_ENABLED=1）；历史任务仍可查看。</p>}
      <Tabs value={mode} onValueChange={(value) => setMode(value as "stock" | "market")}>
        <TabsList variant="line"><TabsTrigger value="stock">个股回测</TabsTrigger><TabsTrigger value="market">全市场回测</TabsTrigger></TabsList>
        <TabsContent value="stock"><div className="max-w-xs"><Label htmlFor="backtest-symbol">股票代码</Label><Input id="backtest-symbol" placeholder="600036 或 600036.SH" value={symbol} onChange={(event) => setSymbol(event.target.value)} /></div></TabsContent>
        <TabsContent value="market"><p className="text-sm text-muted-foreground">使用当前在市沪深北全部 A 股的历史行情；报告会列出纳入/排除数量与每只失败股票的原因。</p></TabsContent>
      </Tabs>
      <form className="flex flex-wrap items-end gap-3 rounded border p-4" onSubmit={(event) => { event.preventDefault(); void run(); }}>
        {[
          { id: "start", label: "开始日期", value: start, set: setStart, type: "date" },
          { id: "end", label: "结束日期", value: end, set: setEnd, type: "date" },
          { id: "cash", label: "初始资金（元）", value: cash, set: setCash, type: "number" },
          { id: "positions", label: "最大持仓数", value: positions, set: setPositions, type: "number" },
          { id: "size", label: "单笔仓位上限（%）", value: size, set: setSize, type: "number" },
        ].map((field) => (
          <div key={field.id}>
            <Label htmlFor={`bt-${field.id}`}>{field.label}</Label>
            <Input className="w-40" id={`bt-${field.id}`} type={field.type} required value={field.value} onChange={(event) => field.set(event.target.value)} />
          </div>
        ))}
        <Button type="submit" disabled={available !== true || starting || (mode === "stock" && !symbol.trim())}>{starting ? "提交中…" : "运行三策略回测"}</Button>
      </form>

      <div className="flex flex-wrap items-center gap-3">
        <Label htmlFor="backtest-history">历史任务</Label>
        <select id="backtest-history" className="h-9 min-w-72 rounded border bg-background px-2 text-sm" value={selected ?? ""} onChange={(event) => select(Number(event.target.value) || null)}>
          <option value="">选择回测任务</option>
          {jobs.map((job) => <option key={job.id} value={job.id}>#{job.id} · {job.created_at} · {STATUS_LABEL[job.status] ?? job.status}</option>)}
        </select>
        {selectedJob?.status === "failed" && available === true && <Button variant="outline" disabled={starting} onClick={() => void retry()}>从冻结输入重试</Button>}
        {progress && <span className="text-sm text-muted-foreground">{progress.stage} · {progress.processed}/{progress.total}</span>}
      </div>
      {error && <p role="alert" className="rounded border border-destructive/30 p-3 text-sm text-destructive">{error}</p>}

      {report && (
        <>
          {report.universe ? (
            <p className="text-xs text-muted-foreground">
              样本名单日期 {report.universe.as_of} · 总样本 {report.universe.total} 只（纳入 {report.universe.included}、排除 {report.universe.excluded}）· 实际区间 {report.effective_start} 至 {report.effective_end}（{report.calendar_days} 个交易日）
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">历史报告未记录样本元数据 · 覆盖 {report.calendar_days} 个交易日</p>
          )}
          <div className="overflow-x-auto rounded border">
            <table className="w-full min-w-[720px] text-sm">
              <thead className="bg-muted/40"><tr><th className="p-3 text-left">策略</th>{METRIC_COLUMNS.map((column) => <th key={column.key} className="p-3 text-right">{column.name}</th>)}</tr></thead>
              <tbody>
                {report.results.map((result) => (
                  <tr key={result.strategy_id} className="border-t">
                    <th className="p-3 text-left font-medium">{result.name}</th>
                    {METRIC_COLUMNS.map((column) => (
                      <td key={column.key} className="p-3 text-right tabular-nums">{formatMetric(result.metrics[column.key], column.percent, column.key === "closed_trades")}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <EquityChart results={report.results} />
          <p className="text-xs text-muted-foreground">N/A 表示无对应样本：未买入时年化与回撤为 0、夏普/盈亏比/胜率为 N/A；已买入但未平仓时仅盈亏比/胜率为 N/A。盈亏比采用已平仓交易净金额，夏普无风险利率为 0，区间末未平仓持仓按市值计入权益。</p>
          <div className="space-y-1">
            {report.results.flatMap((result) => result.warnings ?? []).map((warning, index) => <p key={index} className="text-xs text-amber-600">{warning}</p>)}
          </div>
          <ul className="space-y-1 text-xs text-muted-foreground">{report.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
          <DailyReturnsTable results={report.results} />
          <details className="rounded border p-3">
            <summary className="cursor-pointer text-sm">成交明细与排除原因</summary>
            <div className="mt-3 max-h-96 overflow-auto">
              {report.results.map((result) => (
                <div key={result.strategy_id}>
                  <h3 className="my-3 text-sm font-medium">{result.name}</h3>
                  {!result.trades.length && <p className="text-xs text-muted-foreground">该策略在区间内没有完成任何交易。</p>}
                  <table className="w-full text-xs">
                    <thead><tr>{["股票", "买入日", "卖出日", "数量", "净盈亏", "退出原因"].map((name) => <th className="p-2 text-left" key={name}>{name}</th>)}</tr></thead>
                    <tbody>
                      {result.trades.map((trade, index) => (
                        <tr key={index} className="border-t">
                          <td className="p-2">{trade.symbol}</td><td>{trade.entry_day}</td><td>{trade.exit_day}</td>
                          <td>{trade.quantity}</td><td>{trade.pnl_cash.toFixed(2)}</td><td>{trade.exit_reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {result.rejected.map((row, index) => <p key={index} className="text-xs text-muted-foreground">{row.date} {row.symbol}：{row.reason}</p>)}
                </div>
              ))}
              {report.excluded.map((row, index) => <p key={index} className="text-xs">{row.symbol}：{row.reason}</p>)}
            </div>
          </details>
        </>
      )}
    </div>
  );
}
