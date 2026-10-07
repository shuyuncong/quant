"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import { Loader2, Play } from "lucide-react";

export type CandidateVariant = "macd" | "macd-observed" | "yearline" | "macd-divergence";

export interface MacdCandidateRow {
  symbol: string;
  name: string;
  score: number;
  strategy_score?: number;
  confirmed_at?: string;
  dif?: number;
  dea?: number;
  zero_distance?: number;
  golden_cross_zone?: "above" | "near" | "below";
  golden_cross_zone_label?: string;
  confirmation_items?: string[];
  chan_signals?: unknown[];
  execution_mode?: string;
  regime?: string | null;
  observe_reason?: string;
}

export interface YearlineCandidateRow {
  symbol: string;
  name: string;
  score: number;
  signal_type?: string;
  signal_date?: string;
  entry_reference?: string;
  close?: number;
  ma60?: number;
  ma120?: number;
  ma250?: number;
  ma250_slope_pct?: number;
  atr14_pct?: number;
  volume_ratio?: number;
  low_vs_ma250_pct?: number;
  close_vs_ma250_pct?: number;
  stop_suggestion_pct?: number;
  research_only?: boolean;
}

export interface DivergenceCandidateRow {
  symbol: string;
  name: string;
  score: number;
  signal_type?: string;
  signal_date?: string;
  entry_reference?: string;
  close?: number;
  zero_axis_zone?: "above" | "near";
  zero_axis_zone_label?: string;
  dif?: number;
  dea?: number;
  hist?: number;
  ma_long?: number;
  ma_long_slope_pct?: number;
  close_vs_ma_long_pct?: number;
  volume_ratio?: number;
  volume_ratio_threshold?: number;
  divergence_area_ratio?: number;
  divergence_price_new_low_pct?: number;
  divergence_prior_low?: number;
  divergence_latest_low?: number;
  conditions?: string[];
  research_only?: boolean;
}

interface CandidateTableProps {
  variant: CandidateVariant;
  rows: Array<
    MacdCandidateRow | YearlineCandidateRow | DivergenceCandidateRow
  >;
  emptyText?: string;
  onAnalyze?: (symbol: string, name?: string) => void;
  analyzingSymbol?: string | null;
}

function fmt(value: number | undefined, digits = 2): string {
  return value == null ? "-" : value.toFixed(digits);
}

function signedPct(value: number | undefined): string {
  if (value == null) return "-";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

/** 指标股票池候选表格: 按 variant 渲染 MACD / 观察 / 年线 / 零轴底背离字段, 各池共用一个组件。 */
export function CandidateTable({
  variant,
  rows,
  emptyText = "暂无候选",
  onAnalyze,
  analyzingSymbol,
}: CandidateTableProps) {
  if (variant === "macd" || variant === "macd-observed") {
    const macdRows = rows as MacdCandidateRow[];
    const observed = variant === "macd-observed";
    return (
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>代码</TableHead>
            <TableHead>名称</TableHead>
            <TableHead>位置</TableHead>
            {observed ? <TableHead>状态</TableHead> : <TableHead>策略分</TableHead>}
            {!observed && <TableHead>确认条件</TableHead>}
            <TableHead>确认时间</TableHead>
            <TableHead>零轴距离</TableHead>
            {onAnalyze && <TableHead className="w-20 text-right">操作</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {macdRows.map((item) => (
            <TableRow key={item.symbol} className="hover:bg-muted/40">
              <TableCell className="font-mono text-xs font-semibold">{item.symbol}</TableCell>
              <TableCell>{item.name || "-"}</TableCell>
              <TableCell>
                <Badge
                  variant={
                    item.golden_cross_zone === "above"
                      ? "default"
                      : item.golden_cross_zone === "below"
                        ? "destructive"
                        : "secondary"
                  }
                >
                  {item.golden_cross_zone_label || "未识别"}
                </Badge>
              </TableCell>
              {observed ? (
                <TableCell>
                  <Badge variant="outline">
                    观察{item.regime ? ` · ${item.regime}` : ""}
                  </Badge>
                </TableCell>
              ) : (
                <TableCell>{item.strategy_score ?? item.score}</TableCell>
              )}
              {!observed && (
                <TableCell className="max-w-64 text-xs">
                  {item.confirmation_items?.length
                    ? item.confirmation_items.join("、")
                    : "暂无额外确认"}
                </TableCell>
              )}
              <TableCell className="text-xs text-muted-foreground">
                {item.confirmed_at || "-"}
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {item.zero_distance != null ? item.zero_distance.toFixed(5) : "-"}
              </TableCell>
              {onAnalyze && (
                <TableCell className="text-right">
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 px-2 text-xs gap-1 hover:border-primary hover:text-primary"
                    disabled={analyzingSymbol === item.symbol}
                    onClick={() => onAnalyze(item.symbol, item.name)}
                    title="发起五标签多策略分析"
                  >
                    {analyzingSymbol === item.symbol ? (
                      <Loader2 className="size-3 animate-spin" />
                    ) : (
                      <Play className="size-3" />
                    )}
                    分析
                  </Button>
                </TableCell>
              )}
            </TableRow>
          ))}
          {macdRows.length === 0 && (
            <TableRow>
              <TableCell colSpan={(observed ? 6 : 7) + (onAnalyze ? 1 : 0)} className="text-center text-muted-foreground">
                {emptyText}
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    );
  }

  if (variant === "macd-divergence") {
    const divergenceRows = rows as DivergenceCandidateRow[];
    return (
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>代码</TableHead>
            <TableHead>名称</TableHead>
            <TableHead>信号日期</TableHead>
            <TableHead>金叉位置</TableHead>
            <TableHead>背离面积比</TableHead>
            <TableHead>量比</TableHead>
            <TableHead>年线偏离</TableHead>
            <TableHead>命中条件</TableHead>
            {onAnalyze && <TableHead className="w-20 text-right">操作</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {divergenceRows.map((item) => (
            <TableRow key={item.symbol} className="hover:bg-muted/40">
              <TableCell className="font-mono text-xs font-semibold">{item.symbol}</TableCell>
              <TableCell>{item.name || "-"}</TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {item.signal_date || "-"}
              </TableCell>
              <TableCell>
                <Badge
                  variant={item.zero_axis_zone === "above" ? "default" : "secondary"}
                >
                  {item.zero_axis_zone_label || "0轴金叉"}
                </Badge>
              </TableCell>
              <TableCell className="text-xs">
                <TooltipProvider delay={300}>
                  <Tooltip>
                    <TooltipTrigger>
                      <span className="inline-flex items-center gap-1">
                        {fmt(item.divergence_area_ratio, 2)}
                        <Badge variant="outline">研究</Badge>
                      </span>
                    </TooltipTrigger>
                    <TooltipContent side="bottom" className="max-w-72">
                      <p className="font-medium">底背离：MACD 负柱面积收缩</p>
                      <p className="mt-0.5 text-background/70">
                        最近两段已完成负柱区间比较：后一段创新低
                        {item.divergence_price_new_low_pct != null
                          ? `（${item.divergence_price_new_low_pct.toFixed(2)}%）`
                          : ""}
                        ，面积比 = 后段 / 前段（越小背离越强）。
                      </p>
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              </TableCell>
              <TableCell className="text-xs">
                {fmt(item.volume_ratio)}x
                {item.volume_ratio_threshold != null && (
                  <span className="text-muted-foreground">
                    {" "}
                    ≥{fmt(item.volume_ratio_threshold, 1)}
                  </span>
                )}
              </TableCell>
              <TableCell className="text-xs">{signedPct(item.close_vs_ma_long_pct)}</TableCell>
              <TableCell className="max-w-64 text-xs">
                {item.conditions?.length ? item.conditions.join("、") : "-"}
              </TableCell>
              {onAnalyze && (
                <TableCell className="text-right">
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 px-2 text-xs gap-1 hover:border-primary hover:text-primary"
                    disabled={analyzingSymbol === item.symbol}
                    onClick={() => onAnalyze(item.symbol, item.name)}
                    title="发起五标签多策略分析"
                  >
                    {analyzingSymbol === item.symbol ? (
                      <Loader2 className="size-3 animate-spin" />
                    ) : (
                      <Play className="size-3" />
                    )}
                    分析
                  </Button>
                </TableCell>
              )}
            </TableRow>
          ))}
          {divergenceRows.length === 0 && (
            <TableRow>
              <TableCell colSpan={8 + (onAnalyze ? 1 : 0)} className="text-center text-muted-foreground">
                {emptyText}
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    );
  }

  const yearlineRows = rows as YearlineCandidateRow[];
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>代码</TableHead>
          <TableHead>名称</TableHead>
          <TableHead>信号日期</TableHead>
          <TableHead>MA60</TableHead>
          <TableHead>MA120</TableHead>
          <TableHead>MA250</TableHead>
          <TableHead>年线斜率</TableHead>
          <TableHead>ATR14%</TableHead>
          <TableHead>量比</TableHead>
          <TableHead>信号类型</TableHead>
          <TableHead>研究止损</TableHead>
          {onAnalyze && <TableHead className="w-20 text-right">操作</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {yearlineRows.map((item) => (
          <TableRow key={item.symbol} className="hover:bg-muted/40">
            <TableCell className="font-mono text-xs font-semibold">{item.symbol}</TableCell>
            <TableCell>{item.name || "-"}</TableCell>
            <TableCell className="text-xs text-muted-foreground">
              {item.signal_date || "-"}
            </TableCell>
            <TableCell className="text-xs">{fmt(item.ma60)}</TableCell>
            <TableCell className="text-xs">{fmt(item.ma120)}</TableCell>
            <TableCell className="text-xs">{fmt(item.ma250)}</TableCell>
            <TableCell className="text-xs">{signedPct(item.ma250_slope_pct)}</TableCell>
            <TableCell className="text-xs">{fmt(item.atr14_pct)}%</TableCell>
            <TableCell className="text-xs">{fmt(item.volume_ratio)}x</TableCell>
            <TableCell>
              <Badge variant="secondary">
                {item.signal_type === "yearline_pullback" ? "年线回踩" : item.signal_type || "-"}
              </Badge>
            </TableCell>
            <TableCell>
              <TooltipProvider delay={300}>
                <Tooltip>
                  <TooltipTrigger>
                    <span className="inline-flex items-center gap-1 text-xs">
                      {fmt(item.stop_suggestion_pct)}%
                      {item.research_only && <Badge variant="outline">研究</Badge>}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent side="bottom" className="max-w-72">
                    <p className="font-medium">动态止损建议（research_only）</p>
                    <p className="mt-0.5 text-background/70">
                      建议值 clip(2 × ATR14 / 信号收盘, 5%, 8%)，仅供研究展示，不进入下单链路；
                      生产固定 8% 止损保持不变。
                    </p>
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            </TableCell>
            {onAnalyze && (
              <TableCell className="text-right">
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 px-2 text-xs gap-1 hover:border-primary hover:text-primary"
                  disabled={analyzingSymbol === item.symbol}
                  onClick={() => onAnalyze(item.symbol, item.name)}
                  title="发起五标签多策略分析"
                >
                  {analyzingSymbol === item.symbol ? (
                    <Loader2 className="size-3 animate-spin" />
                  ) : (
                    <Play className="size-3" />
                  )}
                  分析
                </Button>
              </TableCell>
            )}
          </TableRow>
        ))}
        {yearlineRows.length === 0 && (
          <TableRow>
            <TableCell colSpan={11 + (onAnalyze ? 1 : 0)} className="text-center text-muted-foreground">
              {emptyText}
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  );
}
