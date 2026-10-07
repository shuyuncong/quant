"use client";

import {
  Activity,
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  HelpCircle,
  RefreshCw,
  TrendingDown,
  TrendingUp,
  XCircle,
} from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { MarkdownContent } from "@/components/markdown-content";
import {
  STRATEGIES,
  type AnalysisDocument,
  type AnalysisStage,
  type StrategyCondition,
} from "@/lib/analysis-types";

const statusLabel: Record<string, string> = {
  pending: "等待生成",
  running: "正在生成",
  success: "已完成",
  failed: "生成失败",
  ok: "已就绪",
  insufficient_data: "数据不足",
  error: "分析失败",
  disabled: "已停用",
};

function Stage({ value }: { value: AnalysisStage }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        <span className="font-medium text-foreground">{statusLabel[value.status] ?? value.status}</span>
        {value.model && (
          <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">
            模型: {value.model}
          </span>
        )}
        {value.completed_at && <span>{value.completed_at}</span>}
      </div>
      {value.error && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
        >
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <span>{value.error}</span>
        </div>
      )}
      {value.content ? (
        <div className="prose prose-sm dark:prose-invert max-w-none rounded-lg border bg-card p-4">
          <MarkdownContent content={value.content} />
        </div>
      ) : (
        <p className="py-6 text-center text-sm text-muted-foreground">
          {value.status === "failed"
            ? "此阶段未生成，三策略独立规则计算结果仍可查看。"
            : "分析结果生成后将在此处实时展示。"}
        </p>
      )}
    </div>
  );
}

function Conditions({
  rows,
  isSellRule = false,
}: {
  rows: StrategyCondition[];
  isSellRule?: boolean;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border bg-card">
      <table className="w-full min-w-[520px] text-sm">
        <thead className="border-b bg-muted/40 text-xs text-muted-foreground">
          <tr>
            <th className="p-2.5 text-left font-medium">条件项</th>
            <th className="p-2.5 text-left font-medium">实际数值</th>
            <th className="p-2.5 text-left font-medium">规则阈值</th>
            <th className="p-2.5 text-right font-medium">满足判断</th>
          </tr>
        </thead>
        <tbody className="divide-y text-xs">
          {rows.map((row, index) => {
            const isMet = row.met === true;
            return (
              <tr key={index} className="transition-colors hover:bg-muted/20">
                <td className="p-2.5 font-medium">{row.name}</td>
                <td className="p-2.5 font-mono text-muted-foreground">
                  {row.actual == null
                    ? "未知"
                    : typeof row.actual === "boolean"
                    ? row.actual
                      ? "是"
                      : "否"
                    : String(row.actual)}
                </td>
                <td className="p-2.5 text-muted-foreground">{row.expected}</td>
                <td className="p-2.5 text-right">
                  {row.met == null ? (
                    <Badge variant="outline" className="text-muted-foreground/60">
                      <HelpCircle className="mr-1 size-3" /> 未知
                    </Badge>
                  ) : isMet ? (
                    isSellRule ? (
                      <Badge className="border-emerald-500/30 bg-emerald-500/10 text-emerald-600 hover:bg-emerald-500/20 dark:text-emerald-400">
                        <CheckCircle2 className="mr-1 size-3" /> 触发退出
                      </Badge>
                    ) : (
                      <Badge className="border-rose-500/30 bg-rose-500/10 text-rose-600 hover:bg-rose-500/20 dark:text-rose-400">
                        <CheckCircle2 className="mr-1 size-3" /> 满足买点
                      </Badge>
                    )
                  ) : (
                    <Badge variant="outline" className="text-muted-foreground">
                      <XCircle className="mr-1 size-3 opacity-60" /> 未满足
                    </Badge>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function FiveTabAnalysis({
  document,
  onRetry,
}: {
  document: AnalysisDocument;
  onRetry?: () => void;
}) {
  const failed =
    document.technical.status === "failed" || document.synthesis.status === "failed";

  const buyStrategies = document.strategies.filter((s) => s.buy);
  const sellStrategies = document.strategies.filter((s) => s.sell);

  // 综合判定共识态度
  let consensusTone: "bull" | "bear" | "neutral" = "neutral";
  let consensusLabel = "观望 · 暂无共振信号";
  if (sellStrategies.length > 0) {
    consensusTone = "bear";
    consensusLabel = `预警 · ${sellStrategies.length} 个策略触发退出条件`;
  } else if (buyStrategies.length >= 2) {
    consensusTone = "bull";
    consensusLabel = `看多 · ${buyStrategies.length} 个策略共振触发买点`;
  } else if (buyStrategies.length === 1) {
    consensusTone = "bull";
    consensusLabel = `谨慎关注 · 1 个策略触发买点`;
  }

  return (
    <div className="space-y-4">
      {/* 顶部行动共识横幅 */}
      <div
        className={`flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3 ${
          consensusTone === "bull"
            ? "border-rose-500/30 bg-rose-500/5 text-rose-950 dark:text-rose-100"
            : consensusTone === "bear"
            ? "border-emerald-500/30 bg-emerald-500/5 text-emerald-950 dark:text-emerald-100"
            : "border-border bg-muted/20"
        }`}
      >
        <div className="flex items-center gap-2.5">
          {consensusTone === "bull" ? (
            <TrendingUp className="size-5 text-rose-600 dark:text-rose-400" />
          ) : consensusTone === "bear" ? (
            <TrendingDown className="size-5 text-emerald-600 dark:text-emerald-400" />
          ) : (
            <Activity className="size-5 text-muted-foreground" />
          )}
          <div>
            <div className="flex items-center gap-2">
              <span className="font-semibold text-sm">{consensusLabel}</span>
              <span className="text-xs text-muted-foreground">
                ({document.name || document.symbol})
              </span>
            </div>
            <p className="text-xs text-muted-foreground">
              分析时点 {document.as_of} · 版本 {document.revision}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {failed && onRetry && (
            <Button size="sm" variant="outline" onClick={onRetry} className="h-7 text-xs">
              <RefreshCw className="mr-1 size-3" /> 重试失败阶段
            </Button>
          )}
        </div>
      </div>

      {/* 多策略共识状态微型看板 */}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        {document.strategies.map((strategy) => {
          const isOk = strategy.status === "ok";
          return (
            <div
              key={strategy.strategy_id}
              className="flex items-center justify-between rounded-lg border bg-card px-3 py-2 text-xs"
            >
              <span className="font-medium">{strategy.name}</span>
              {!isOk ? (
                <Badge variant="outline" className="text-muted-foreground">
                  {statusLabel[strategy.status] ?? strategy.status}
                </Badge>
              ) : strategy.sell ? (
                <Badge className="border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
                  退出触发
                </Badge>
              ) : strategy.buy ? (
                <Badge className="border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400">
                  买点触发
                </Badge>
              ) : (
                <Badge variant="outline" className="text-muted-foreground">
                  未触发
                </Badge>
              )}
            </div>
          );
        })}
      </div>

      {/* 五页签导航与主体内容 */}
      <Tabs defaultValue="synthesis" className="min-w-0 gap-3">
        <div className="overflow-x-auto border-b">
          <TabsList variant="line" className="min-w-max">
            <TabsTrigger value="synthesis" className="text-xs sm:text-sm">
              综合结论
            </TabsTrigger>
            <TabsTrigger value="technical" className="text-xs sm:text-sm">
              AI 技术分析
            </TabsTrigger>
            {STRATEGIES.map((strategy) => (
              <TabsTrigger key={strategy.id} value={strategy.id} className="text-xs sm:text-sm">
                {strategy.name}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>

        <TabsContent value="synthesis" className="space-y-4 pt-2">
          <Stage value={document.synthesis} />
        </TabsContent>

        <TabsContent value="technical" className="space-y-4 pt-2">
          <Stage value={document.technical} />
        </TabsContent>

        {STRATEGIES.map((strategy) => {
          const result = document.strategies.find((row) => row.strategy_id === strategy.id);
          return (
            <TabsContent key={strategy.id} value={strategy.id} className="space-y-4 pt-2">
              {!result ? (
                <p className="py-6 text-center text-sm text-muted-foreground">等待策略计算...</p>
              ) : (
                <>
                  <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-muted/20 p-3">
                    <div className="flex items-center gap-2">
                      <Badge variant="outline">{statusLabel[result.status]}</Badge>
                      <strong className="text-sm">
                        {result.sell
                          ? "退出条件已触发"
                          : result.buy
                          ? "买点已触发"
                          : "买点尚未触发"}
                      </strong>
                    </div>
                    <span className="text-xs text-muted-foreground">
                      日线截至 {result.as_of ?? "未知"} · 参考价{" "}
                      <span className="font-mono font-medium text-foreground">
                        {result.reference_price?.toFixed(3) ?? "—"}
                      </span>
                    </span>
                  </div>

                  <div>
                    <h3 className="mb-2 text-xs font-semibold text-muted-foreground">
                      买入触发条件清单 ({result.buy_conditions.filter((c) => c.met).length}/
                      {result.buy_conditions.length} 项满足)
                    </h3>
                    <Conditions rows={result.buy_conditions} isSellRule={false} />
                  </div>

                  <div>
                    <h3 className="mb-2 text-xs font-semibold text-muted-foreground">
                      卖出/退出规则 ·{" "}
                      {result.exit_rule === "fixed" ? "固定止损 / 止盈 / 超时" : "趋势退出"} (
                      {result.sell_conditions.filter((c) => c.met).length}/
                      {result.sell_conditions.length} 项触发)
                    </h3>
                    <Conditions rows={result.sell_conditions} isSellRule={true} />
                  </div>

                  {result.warnings.length > 0 && (
                    <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-700 dark:text-amber-400">
                      <div className="mb-1 flex items-center gap-1.5 font-medium">
                        <AlertTriangle className="size-3.5" /> 风险提示与说明
                      </div>
                      <ul className="list-inside list-disc space-y-1 pl-1">
                        {result.warnings.map((warning, index) => (
                          <li key={index}>{warning}</li>
                        ))}
                      </ul>
                    </div>
                  )}

                  <details className="text-xs text-muted-foreground">
                    <summary className="cursor-pointer font-medium hover:text-foreground">
                      查看策略版本与运行参数 · {result.version}
                    </summary>
                    <pre className="mt-2 max-h-48 overflow-auto rounded-lg border bg-muted p-3 font-mono text-[11px]">
                      {JSON.stringify(result.parameters, null, 2)}
                    </pre>
                  </details>
                </>
              )}
            </TabsContent>
          );
        })}
      </Tabs>
    </div>
  );
}
