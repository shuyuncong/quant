"use client";

import { Badge } from "@/components/ui/badge";
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

export type CandidateVariant = "macd" | "yearline";

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

interface CandidateTableProps {
  variant: CandidateVariant;
  rows: Array<MacdCandidateRow | YearlineCandidateRow>;
  emptyText?: string;
}

function fmt(value: number | undefined, digits = 2): string {
  return value == null ? "-" : value.toFixed(digits);
}

function signedPct(value: number | undefined): string {
  if (value == null) return "-";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

/** 指标股票池候选表格: 按 variant 渲染 MACD 或年线字段, 两个池共用一个组件。 */
export function CandidateTable({
  variant,
  rows,
  emptyText = "暂无候选",
}: CandidateTableProps) {
  if (variant === "macd") {
    const macdRows = rows as MacdCandidateRow[];
    return (
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>代码</TableHead>
            <TableHead>名称</TableHead>
            <TableHead>位置</TableHead>
            <TableHead>策略分</TableHead>
            <TableHead>确认条件</TableHead>
            <TableHead>确认时间</TableHead>
            <TableHead>零轴距离</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {macdRows.map((item) => (
            <TableRow key={item.symbol}>
              <TableCell className="font-mono text-xs">{item.symbol}</TableCell>
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
              <TableCell>{item.strategy_score ?? item.score}</TableCell>
              <TableCell className="max-w-64 text-xs">
                {item.confirmation_items?.length
                  ? item.confirmation_items.join("、")
                  : "暂无额外确认"}
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {item.confirmed_at || "-"}
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {item.zero_distance != null ? item.zero_distance.toFixed(5) : "-"}
              </TableCell>
            </TableRow>
          ))}
          {macdRows.length === 0 && (
            <TableRow>
              <TableCell colSpan={7} className="text-center text-muted-foreground">
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
        </TableRow>
      </TableHeader>
      <TableBody>
        {yearlineRows.map((item) => (
          <TableRow key={item.symbol}>
            <TableCell className="font-mono text-xs">{item.symbol}</TableCell>
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
          </TableRow>
        ))}
        {yearlineRows.length === 0 && (
          <TableRow>
            <TableCell colSpan={11} className="text-center text-muted-foreground">
              {emptyText}
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  );
}