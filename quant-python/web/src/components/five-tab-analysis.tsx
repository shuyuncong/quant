"use client";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { MarkdownContent } from "@/components/markdown-content";
import { STRATEGIES, type AnalysisDocument, type AnalysisStage, type StrategyCondition } from "@/lib/analysis-types";
const statusLabel: Record<string, string> = { pending: "等待生成", running: "正在生成", success: "已完成", failed: "生成失败", ok: "已确认", insufficient_data: "数据不足", error: "分析失败", disabled: "已停用" };
function Stage({ value }: { value: AnalysisStage }) {
  return <div className="space-y-3">
    <div className="flex flex-wrap gap-3 text-xs text-muted-foreground"><span>{statusLabel[value.status]}</span>{value.model && <span>模型：{value.model}</span>}{value.completed_at && <span>{value.completed_at}</span>}</div>
    {value.error && <p role="alert" className="rounded border border-destructive/30 p-3 text-sm text-destructive">{value.error}</p>}
    {value.content ? <MarkdownContent content={value.content} /> : <p className="py-6 text-sm text-muted-foreground">{value.status === "failed" ? "此阶段未完成，三策略计算结果仍可查看。" : "结果生成后会自动展示。"}</p>}
  </div>;
}
function Conditions({ rows }: { rows: StrategyCondition[] }) {
  return <div className="overflow-x-auto"><table className="w-full min-w-[520px] text-sm"><thead className="bg-muted/50"><tr>{["条件", "实际值", "要求", "判断"].map(value => <th key={value} className="p-2 text-left font-medium">{value}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index} className="border-b"><td className="p-2">{row.name}</td><td className="p-2 tabular-nums">{row.actual == null ? "未知" : typeof row.actual === "boolean" ? row.actual ? "是" : "否" : String(row.actual)}</td><td className="p-2">{row.expected}</td><td className="p-2"><Badge variant={row.met ? "default" : "outline"}>{row.met == null ? "未知" : row.met ? "满足" : "未满足"}</Badge></td></tr>)}</tbody></table></div>;
}
export function FiveTabAnalysis({ document, onRetry }: { document: AnalysisDocument; onRetry?: () => void }) {
  const failed = document.technical.status === "failed" || document.synthesis.status === "failed";
  return <Tabs defaultValue="synthesis" className="min-w-0 gap-3">
    <div className="flex flex-wrap items-center justify-between gap-2 border-b pb-2"><div className="text-xs text-muted-foreground">分析时点 {document.as_of} · 版本 {document.revision}</div>{failed && onRetry && <Button size="sm" variant="outline" onClick={onRetry}>重试失败阶段</Button>}</div>
    <div className="overflow-x-auto"><TabsList variant="line" className="min-w-max"><TabsTrigger value="synthesis">综合结论</TabsTrigger><TabsTrigger value="technical">AI 技术分析</TabsTrigger>{STRATEGIES.map(strategy => <TabsTrigger key={strategy.id} value={strategy.id}>{strategy.name}</TabsTrigger>)}</TabsList></div>
    <TabsContent value="synthesis"><Stage value={document.synthesis} /><div className="mt-4 flex flex-wrap gap-2 border-t pt-3">{document.strategies.map(strategy => <Badge key={strategy.strategy_id} variant="outline">{strategy.name}：{strategy.status !== "ok" ? statusLabel[strategy.status] : strategy.sell ? "退出条件触发" : strategy.buy ? "买点触发" : "买点未触发"}</Badge>)}</div></TabsContent>
    <TabsContent value="technical"><Stage value={document.technical} /></TabsContent>
    {STRATEGIES.map(strategy => { const result = document.strategies.find(row => row.strategy_id === strategy.id); return <TabsContent key={strategy.id} value={strategy.id} className="space-y-4">
      {!result ? <p className="text-sm text-muted-foreground">等待策略计算</p> : <>
        <div className="flex flex-wrap items-center gap-3"><Badge variant="outline">{statusLabel[result.status]}</Badge><strong className="text-sm">{result.sell ? "退出条件已触发" : result.buy ? "买点已触发" : "买点尚未触发"}</strong><span className="text-xs text-muted-foreground">日线截至 {result.as_of ?? "未知"} · 参考价 {result.reference_price?.toFixed(3) ?? "—"}</span></div>
        <div><h3 className="mb-2 text-sm font-medium">买入条件</h3><Conditions rows={result.buy_conditions} /></div>
        <div><h3 className="mb-2 text-sm font-medium">卖出条件 · {result.exit_rule === "fixed" ? "固定止损 / 止盈 / 超时" : "趋势退出"}</h3><Conditions rows={result.sell_conditions} /></div>
        <ul className="space-y-1 text-xs text-muted-foreground">{result.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
        <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">策略版本与参数 · {result.version}</summary><pre className="mt-2 max-h-48 overflow-auto rounded bg-muted p-3">{JSON.stringify(result.parameters, null, 2)}</pre></details>
      </>}
    </TabsContent>; })}
  </Tabs>;
}
