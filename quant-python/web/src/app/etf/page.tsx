"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, ArrowUpRight, ChartNoAxesCombined, RefreshCw, ShieldAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

interface EtfTarget {
  symbol: string;
  name: string;
  weight: number;
}

interface EtfMetrics {
  total_return_pct: number;
  cagr_pct: number;
  max_drawdown_pct: number;
  sharpe: number;
  daily_win_rate_pct: number | null;
  daily_payoff: number | null;
  trade_win_rate_pct: number | null;
  trade_payoff: number | null;
  avg_exposure: number;
  trades: number;
}

interface EtfSignal {
  gate: boolean;
  gate_reason: string;
  core: string;
  core_name: string;
  picks: string[];
  target_weights: Record<string, number>;
  cash_weight: number;
  vol_scale: number;
  reason: string;
}

interface EtfPaperPosition {
  symbol: string;
  name: string;
  shares: number;
  avg_cost: number;
  last_price: number;
  market_value: number;
}

interface EtfPaperNav {
  date: string;
  cash: number;
  market_value: number;
  equity: number;
  pnl_pct: number | null;
}

interface EtfPaperLedger {
  initialized: boolean;
  capital: number;
  cash: number;
  market_value: number;
  equity: number;
  pnl_pct: number | null;
  positions: EtfPaperPosition[];
  nav_history?: EtfPaperNav[];
  as_of: string | null;
  updated_at: string | null;
}

interface EtfHistoryEntry {
  as_of: string;
  generated_at?: string;
  gate: boolean;
  core: string | null;
  picks: string[];
  target_weights: Record<string, number>;
  cash_weight: number;
  vol_scale: number;
  reason: string;
}

interface EtfOverview {
  strategy: string;
  strategy_id: string;
  as_of: string;
  mode: string;
  source: string;
  stale: boolean;
  refresh_note: string;
  config: Record<string, number | string>;
  metrics: EtfMetrics;
  yearly: Record<string, number>;
  signal: EtfSignal;
  target: EtfTarget[];
  curve: Array<{ date: string; equity: number }>;
  history?: EtfHistoryEntry[];
  paper?: EtfPaperLedger;
}

const METRIC_CARDS = [
  { key: "cagr_pct", label: "年化收益", suffix: "%", digits: 2 },
  { key: "max_drawdown_pct", label: "最大回撤", suffix: "%", digits: 2 },
  { key: "sharpe", label: "Sharpe", suffix: "", digits: 3 },
  { key: "total_return_pct", label: "全期收益", suffix: "%", digits: 2 },
] as const;

function signed(value: number, digits = 2): string {
  const rounded = Number(value.toFixed(digits));
  return `${rounded > 0 ? "+" : ""}${rounded.toFixed(digits)}`;
}

function numberText(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
}

function EquityChart({ rows }: { rows: Array<{ date: string; equity: number }> }) {
  const chart = useMemo(() => {
    if (rows.length < 2) return null;
    const base = rows[0].equity;
    const points = rows.map((row) => ({ date: row.date, value: (row.equity / base) * 100 }));
    const values = points.map((point) => point.value);
    const minimum = Math.min(...values);
    const maximum = Math.max(...values);
    const lower = Math.min(95, minimum - 5);
    const upper = maximum + 8;
    const width = 1000;
    const height = 300;
    const pad = { left: 52, right: 18, top: 18, bottom: 34 };
    const innerWidth = width - pad.left - pad.right;
    const innerHeight = height - pad.top - pad.bottom;
    const x = (index: number) => pad.left + (index / (points.length - 1)) * innerWidth;
    const y = (value: number) => pad.top + ((upper - value) / (upper - lower)) * innerHeight;
    const line = points.map((point, index) => `${x(index).toFixed(2)},${y(point.value).toFixed(2)}`).join(" ");
    const area = `${pad.left},${(pad.top + innerHeight).toFixed(2)} ${line} ${(pad.left + innerWidth).toFixed(2)},${(pad.top + innerHeight).toFixed(2)}`;
    const ticks = Array.from({ length: 5 }, (_, index) => lower + ((upper - lower) * index) / 4);
    const dates = Array.from({ length: 5 }, (_, index) => points[Math.round(((points.length - 1) * index) / 4)]);
    return { points, line, area, ticks, dates, x, y, width, height, pad, innerWidth, innerHeight, lower, upper };
  }, [rows]);

  if (!chart) return null;

  return (
    <svg viewBox={`0 0 ${chart.width} ${chart.height}`} className="w-full" role="img" aria-label="ETF 累计净值曲线">
      <defs>
        <linearGradient id="etf-equity-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#0b6bcb" stopOpacity="0.18" />
          <stop offset="1" stopColor="#0b6bcb" stopOpacity="0" />
        </linearGradient>
      </defs>
      {chart.ticks.map((tick) => (
        <g key={tick}>
          <line
            x1={chart.pad.left}
            y1={chart.y(tick)}
            x2={chart.width - chart.pad.right}
            y2={chart.y(tick)}
            stroke="#e5e7eb"
            strokeWidth="1"
          />
          <text x={chart.pad.left - 8} y={chart.y(tick) + 4} fill="#667085" fontSize="11" textAnchor="end">
            {tick.toFixed(0)}
          </text>
        </g>
      ))}
      {chart.dates.map((point, index) => {
        const x = chart.x(Math.round(((chart.points.length - 1) * index) / 4));
        return (
          <g key={`${point.date}-${index}`}>
            <line x1={x} y1={chart.pad.top} x2={x} y2={chart.height - chart.pad.bottom} stroke="#f2f4f7" />
            <text
              x={x}
              y={chart.height - 12}
              fill="#667085"
              fontSize="11"
              textAnchor={index === 0 ? "start" : index === 4 ? "end" : "middle"}
            >
              {point.date.slice(0, 7)}
            </text>
          </g>
        );
      })}
      <polygon points={chart.area} fill="url(#etf-equity-fill)" />
      <polyline points={chart.line} fill="none" stroke="#0b6bcb" strokeWidth="2.6" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={chart.x(chart.points.length - 1)} cy={chart.y(chart.points[chart.points.length - 1].value)} r="4" fill="#0b6bcb" />
      <text
        x={chart.x(chart.points.length - 1) - 6}
        y={chart.y(chart.points[chart.points.length - 1].value) - 10}
        fill="#0b6bcb"
        fontSize="12"
        fontWeight="600"
        textAnchor="end"
      >
        {chart.points[chart.points.length - 1].value.toFixed(1)}
      </text>
    </svg>
  );
}

export default function EtfPage() {
  const [overview, setOverview] = useState<EtfOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [paperLoading, setPaperLoading] = useState(false);
  const [paperError, setPaperError] = useState<string | null>(null);

  const load = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true);
    try {
      const response = await fetch("/api/etf/overview", { cache: "no-store" });
      const payload = (await response.json()) as { overview?: EtfOverview; error?: string };
      if (!response.ok || !payload.overview) {
        throw new Error(payload.error || "ETF 概览读取失败");
      }
      setOverview(payload.overview);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "ETF 概览读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setRefreshError(null);
    try {
      const response = await fetch("/api/etf/refresh", { method: "POST" });
      const payload = (await response.json()) as { overview?: EtfOverview; error?: string };
      if (!response.ok || !payload.overview) {
        throw new Error(payload.error || "ETF 信号刷新失败");
      }
      setOverview(payload.overview);
      setError(null);
    } catch (cause) {
      setRefreshError(cause instanceof Error ? cause.message : "ETF 信号刷新失败");
    } finally {
      setRefreshing(false);
    }
  }, []);

  const paperAction = useCallback(async (action: "init" | "rebalance" | "mark") => {
    setPaperLoading(true);
    setPaperError(null);
    try {
      const response = await fetch("/api/etf/paper", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, capital: 250_000 }),
      });
      const payload = (await response.json()) as { overview?: EtfOverview; error?: string };
      if (!response.ok || !payload.overview) {
        throw new Error(payload.error || "纸面账本操作失败");
      }
      setOverview(payload.overview);
    } catch (cause) {
      setPaperError(cause instanceof Error ? cause.message : "纸面账本操作失败");
    } finally {
      setPaperLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    fetch("/api/etf/overview", { cache: "no-store" })
      .then(async (response) => {
        const payload = (await response.json()) as { overview?: EtfOverview; error?: string };
        if (!response.ok || !payload.overview) {
          throw new Error(payload.error || "ETF 概览读取失败");
        }
        return payload.overview;
      })
      .then((next) => {
        if (!active) return;
        setOverview(next);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!active) return;
        setError(cause instanceof Error ? cause.message : "ETF 概览读取失败");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  if (loading && !overview) {
    return (
      <div className="flex max-w-5xl flex-col gap-4">
        <h1 className="text-xl font-semibold">ETF 轮动</h1>
        <Card>
          <CardContent className="py-10 text-sm text-muted-foreground">正在读取 ETF 研究快照…</CardContent>
        </Card>
      </div>
    );
  }

  if (error || !overview) {
    return (
      <div className="flex max-w-5xl flex-col gap-4">
        <h1 className="text-xl font-semibold">ETF 轮动</h1>
        <Card>
          <CardContent className="flex flex-col gap-3 py-8">
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertCircle className="size-4" /> {error || "ETF 概览读取失败"}
            </div>
            <Button variant="outline" size="sm" className="w-fit gap-2" onClick={() => void load(true)}>
              <RefreshCw className="size-4" /> 重试
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  const { metrics, signal } = overview;
  const paper = overview.paper;
  const targetRows = overview.target.filter((row) => row.weight > 0);

  return (
    <div className="flex max-w-5xl flex-col gap-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">ETF 轮动</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {overview.strategy} · 数据截至 {overview.as_of}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">{overview.mode === "research_snapshot" ? "研究快照" : overview.mode === "local_refresh" ? "本地刷新" : overview.mode}</Badge>
          <Badge variant="secondary">{signal.gate ? "闸门开启" : "闸门关闭"}</Badge>
          <Badge variant="outline">只读</Badge>
          <Button size="sm" variant="outline" className="gap-2" onClick={() => void refresh()} disabled={refreshing}>
            <RefreshCw className={`size-4 ${refreshing ? "animate-spin" : ""}`} />
            {refreshing ? "刷新中" : "刷新信号"}
          </Button>
        </div>
      </div>

      {refreshError && (
        <div className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300">
          <AlertCircle className="size-4" /> {refreshError}
        </div>
      )}

      <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200">
        {overview.refresh_note} 当前页面展示研究/本地刷新快照，不触发下单，也不代表实盘收益。
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {METRIC_CARDS.map((item) => (
          <Card key={item.key}>
            <CardContent className="p-4">
              <div className="text-xs text-muted-foreground">{item.label}</div>
              <div className="mt-1 text-2xl font-semibold tabular-nums">
                {item.key === "max_drawdown_pct"
                  ? signed(metrics[item.key], item.digits)
                  : item.key === "total_return_pct"
                    ? signed(metrics[item.key], item.digits)
                    : numberText(metrics[item.key], item.digits)}
                {item.suffix}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-[1.25fr_1fr]">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ChartNoAxesCombined className="size-4" /> 当前目标
            </CardTitle>
            <CardDescription>{signal.reason}</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="grid gap-2 sm:grid-cols-2">
              <div className="rounded-lg border p-3">
                <div className="text-xs text-muted-foreground">核心</div>
                <div className="mt-1 text-sm font-medium">
                  {signal.core} {signal.core_name}
                </div>
              </div>
              <div className="rounded-lg border p-3">
                <div className="text-xs text-muted-foreground">卫星候选</div>
                <div className="mt-1 text-sm font-medium">{signal.picks.length ? signal.picks.join(" / ") : "无"}</div>
              </div>
            </div>
            <div className="overflow-hidden rounded-lg border">
              <table className="w-full text-xs">
                <thead className="bg-muted/50 text-muted-foreground">
                  <tr>
                    <th className="px-2 py-1.5 text-left font-medium">标的</th>
                    <th className="px-2 py-1.5 text-left font-medium">名称</th>
                    <th className="px-2 py-1.5 text-right font-medium">目标权重</th>
                  </tr>
                </thead>
                <tbody>
                  {targetRows.map((row) => (
                    <tr key={row.symbol} className="border-t">
                      <td className="px-2 py-1.5 font-mono">{row.symbol}</td>
                      <td className="px-2 py-1.5">{row.name}</td>
                      <td className="px-2 py-1.5 text-right tabular-nums">{(row.weight * 100).toFixed(0)}%</td>
                    </tr>
                  ))}
                  {signal.cash_weight > 0 && (
                    <tr className="border-t">
                      <td className="px-2 py-1.5 font-mono">CASH</td>
                      <td className="px-2 py-1.5">现金</td>
                      <td className="px-2 py-1.5 text-right tabular-nums">{(signal.cash_weight * 100).toFixed(0)}%</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
              <div>波动缩放：{signal.vol_scale.toFixed(1)}</div>
              <div>平均暴露：{(metrics.avg_exposure * 100).toFixed(0)}%</div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>胜率与盈亏比</CardTitle>
            <CardDescription>组合日收益与交易回合两个口径。</CardDescription>
          </CardHeader>
          <CardContent className="grid grid-cols-2 gap-3">
            {[
              ["日胜率", `${numberText(metrics.daily_win_rate_pct, 1)}%`],
              ["日盈亏比", numberText(metrics.daily_payoff, 3)],
              ["交易胜率", `${numberText(metrics.trade_win_rate_pct, 1)}%`],
              ["交易盈亏比", numberText(metrics.trade_payoff, 3)],
            ].map(([label, value]) => (
              <div key={label} className="rounded-lg border p-3">
                <div className="text-xs text-muted-foreground">{label}</div>
                <div className="mt-1 text-lg font-semibold tabular-nums">{value}</div>
              </div>
            ))}
            <div className="col-span-2 text-xs leading-5 text-muted-foreground">
              交易级指标是周度目标 0→正、正→0 的近似回合，不等于逐笔成交账本。
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>纸面账本</CardTitle>
          <CardDescription>按当前目标权重和快照价格模拟持仓、现金与净值；不触发真实下单。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {paperError && (
            <div className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300">
              <AlertCircle className="size-4" /> {paperError}
            </div>
          )}
          {!paper?.initialized ? (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3">
              <div className="text-xs leading-5 text-muted-foreground">
                尚未初始化。纸面账本只使用当前快照价格，不会产生真实委托。
              </div>
              <Button size="sm" className="gap-2" onClick={() => void paperAction("init")} disabled={paperLoading}>
                <RefreshCw className={`size-4 ${paperLoading ? "animate-spin" : ""}`} />
                {paperLoading ? "初始化中" : "初始化 25 万纸面账本"}
              </Button>
            </div>
          ) : (
            <>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                {[
                  ["初始资金", `¥${paper.capital.toLocaleString("zh-CN")}`],
                  ["现金", `¥${paper.cash.toLocaleString("zh-CN")}`],
                  ["持仓市值", `¥${paper.market_value.toLocaleString("zh-CN")}`],
                  ["总权益", `¥${paper.equity.toLocaleString("zh-CN")}`],
                ].map(([label, value]) => (
                  <div key={label} className="rounded-lg border p-3">
                    <div className="text-xs text-muted-foreground">{label}</div>
                    <div className="mt-1 text-sm font-semibold tabular-nums">{value}</div>
                  </div>
                ))}
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                <span>
                  纸面收益：{paper.pnl_pct === null ? "—" : `${signed(paper.pnl_pct, 2)}%`} · 更新于 {paper.updated_at || "—"}
                </span>
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" variant="outline" className="gap-2" onClick={() => void paperAction("mark")} disabled={paperLoading}>
                    <RefreshCw className={`size-4 ${paperLoading ? "animate-spin" : ""}`} />
                    {paperLoading ? "更新中" : "更新纸面净值"}
                  </Button>
                  <Button size="sm" variant="outline" className="gap-2" onClick={() => void paperAction("rebalance")} disabled={paperLoading}>
                    <RefreshCw className={`size-4 ${paperLoading ? "animate-spin" : ""}`} />
                    {paperLoading ? "再平衡中" : "按当前目标再平衡"}
                  </Button>
                </div>
              </div>
              {paper.positions.length > 0 ? (
                <div className="overflow-hidden rounded-lg border">
                  <table className="w-full text-xs">
                    <thead className="bg-muted/50 text-muted-foreground">
                      <tr>
                        <th className="px-2 py-1.5 text-left font-medium">标的</th>
                        <th className="px-2 py-1.5 text-left font-medium">名称</th>
                        <th className="px-2 py-1.5 text-right font-medium">份额</th>
                        <th className="px-2 py-1.5 text-right font-medium">成本</th>
                        <th className="px-2 py-1.5 text-right font-medium">现价</th>
                        <th className="px-2 py-1.5 text-right font-medium">市值</th>
                      </tr>
                    </thead>
                    <tbody>
                      {paper.positions.map((position) => (
                        <tr key={position.symbol} className="border-t">
                          <td className="px-2 py-1.5 font-mono">{position.symbol}</td>
                          <td className="px-2 py-1.5">{position.name}</td>
                          <td className="px-2 py-1.5 text-right tabular-nums">{position.shares}</td>
                          <td className="px-2 py-1.5 text-right tabular-nums">{position.avg_cost.toFixed(3)}</td>
                          <td className="px-2 py-1.5 text-right tabular-nums">{position.last_price.toFixed(3)}</td>
                          <td className="px-2 py-1.5 text-right tabular-nums">¥{position.market_value.toLocaleString("zh-CN")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">当前没有纸面持仓。</p>
              )}
              {paper.nav_history?.length ? (
                <div className="overflow-hidden rounded-lg border">
                  <table className="w-full text-xs">
                    <thead className="bg-muted/50 text-muted-foreground">
                      <tr>
                        <th className="px-2 py-1.5 text-left font-medium">净值日期</th>
                        <th className="px-2 py-1.5 text-right font-medium">现金</th>
                        <th className="px-2 py-1.5 text-right font-medium">持仓市值</th>
                        <th className="px-2 py-1.5 text-right font-medium">总权益</th>
                        <th className="px-2 py-1.5 text-right font-medium">收益</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...paper.nav_history].reverse().map((row) => (
                        <tr key={row.date} className="border-t">
                          <td className="px-2 py-1.5">{row.date}</td>
                          <td className="px-2 py-1.5 text-right tabular-nums">¥{row.cash.toLocaleString("zh-CN")}</td>
                          <td className="px-2 py-1.5 text-right tabular-nums">¥{row.market_value.toLocaleString("zh-CN")}</td>
                          <td className="px-2 py-1.5 text-right tabular-nums">¥{row.equity.toLocaleString("zh-CN")}</td>
                          <td className={`px-2 py-1.5 text-right tabular-nums ${(row.pnl_pct ?? 0) >= 0 ? "text-emerald-600" : "text-red-600"}`}>
                            {row.pnl_pct === null ? "—" : `${signed(row.pnl_pct, 2)}%`}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">还没有净值记录。点击“更新纸面净值”写入第一条。</p>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>2020–2026 累计净值</CardTitle>
          <CardDescription>起点 = 100；研究回测，含 3bp 佣金 + 5bp 滑点。</CardDescription>
        </CardHeader>
        <CardContent>
          <EquityChart rows={overview.curve} />
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>年度收益</CardTitle>
            <CardDescription>按年末净值环比计算；2026 截至 10-30。</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="overflow-hidden rounded-lg border">
              <table className="w-full text-xs">
                <thead className="bg-muted/50 text-muted-foreground">
                  <tr>
                    <th className="px-2 py-1.5 text-left font-medium">年份</th>
                    <th className="px-2 py-1.5 text-right font-medium">收益</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(overview.yearly)
                    .sort(([a], [b]) => Number(a) - Number(b))
                    .map(([year, value]) => (
                      <tr key={year} className="border-t">
                        <td className="px-2 py-1.5">{year}</td>
                        <td className={`px-2 py-1.5 text-right tabular-nums ${value >= 0 ? "text-emerald-600" : "text-red-600"}`}>
                          {signed(value, 2)}%
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ShieldAlert className="size-4" /> 规则与风险
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-xs leading-5 text-muted-foreground">
            <p>核心 65%：黄金/十年国债中选 12−1 动量更强的正动量资产。</p>
            <p>卫星 35%：趋势模板 + 12−1 动量 / 60 日波动，top1；候选不足时预算减半。</p>
            <p>无卫星回填 35%：黄金 40% / 十年国债 60%，目标波动率 10%。</p>
            <p>该版本名义满仓 100%，没有现金缓冲；2024–2026 已多次查看，不是严格盲测 OOS。</p>
            <a href="/workflow" className="mt-1 inline-flex items-center gap-1 font-medium text-primary hover:underline">
              查看完整策略说明 <ArrowUpRight className="size-3.5" />
            </a>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>最近信号</CardTitle>
          <CardDescription>本地刷新按信号日覆盖同一天记录，最多保留 30 条。</CardDescription>
        </CardHeader>
        <CardContent>
          {overview.history?.length ? (
            <div className="overflow-hidden rounded-lg border">
              <table className="w-full text-xs">
                <thead className="bg-muted/50 text-muted-foreground">
                  <tr>
                    <th className="px-2 py-1.5 text-left font-medium">信号日</th>
                    <th className="px-2 py-1.5 text-left font-medium">闸门</th>
                    <th className="px-2 py-1.5 text-left font-medium">核心</th>
                    <th className="px-2 py-1.5 text-left font-medium">卫星</th>
                    <th className="px-2 py-1.5 text-left font-medium">目标</th>
                  </tr>
                </thead>
                <tbody>
                  {[...overview.history].reverse().map((entry) => (
                    <tr key={entry.as_of} className="border-t">
                      <td className="px-2 py-1.5">{entry.as_of}</td>
                      <td className="px-2 py-1.5">{entry.gate ? "开" : "关"}</td>
                      <td className="px-2 py-1.5">{entry.core || "—"}</td>
                      <td className="px-2 py-1.5">{entry.picks.length ? entry.picks.join(" / ") : "—"}</td>
                      <td className="px-2 py-1.5">
                        {Object.entries(entry.target_weights)
                          .map(([symbol, weight]) => `${symbol} ${(weight * 100).toFixed(0)}%`)
                          .join(" / ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">还没有本地刷新记录。点击右上角“刷新信号”生成第一条。</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
