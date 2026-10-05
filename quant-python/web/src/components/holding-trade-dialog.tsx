"use client";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "./ui/dialog";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { Button } from "./ui/button";
import { calculateTrade } from "@/lib/trade-math";
import type { HoldingRow } from "@/lib/types";
export function HoldingTradeDialog({ holding, action, onClose, onSaved }: { holding: HoldingRow; action: "buy" | "sell" | "close"; onClose: () => void; onSaved: () => void }) {
  const [quantity, setQuantity] = useState(action === "close" ? String(holding.shares) : "100");
  const [price, setPrice] = useState("");
  const [fees, setFees] = useState("0");
  const [time, setTime] = useState(() => new Date().toLocaleString("sv-SE", { timeZone: "Asia/Shanghai" }).replace(" ", "T").slice(0, 16));
  const [requestKey] = useState(() => crypto.randomUUID());
  const [saving, setSaving] = useState(false);
  const side = action === "buy" ? "buy" : "sell";
  let preview: ReturnType<typeof calculateTrade> | null = null;
  try { preview = calculateTrade(holding, { side, quantity: Number(quantity), price, fees }); } catch { /* inline preview waits for valid values */ }
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!preview || saving) return;
    setSaving(true);
    try {
      const response = await fetch("/api/holdings/trades", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol: holding.symbol, side, quantity: Number(quantity), price, fees, traded_at: time, request_key: requestKey }) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error ?? "登记失败");
      toast.success("成交已登记，持仓已更新"); onSaved(); onClose();
    } catch (error) { toast.error(error instanceof Error ? error.message : "登记失败"); }
    finally { setSaving(false); }
  };
  return <Dialog open onOpenChange={open => { if (!open && !saving) onClose(); }}><DialogContent className="max-w-lg"><DialogHeader><DialogTitle>{action === "buy" ? "加仓" : action === "sell" ? "减仓" : "清仓"} · {holding.name || holding.symbol}</DialogTitle><DialogDescription>登记已发生的成交。当前 {holding.shares} 股，成本价 {holding.cost_price.toFixed(3)} 元。</DialogDescription></DialogHeader><form onSubmit={submit} className="space-y-4"><div className="grid grid-cols-2 gap-3"><div><Label htmlFor="trade-quantity">成交数量（股）</Label><Input id="trade-quantity" required type="number" min="1" step="1" disabled={saving || action === "close"} value={quantity} onChange={event => setQuantity(event.target.value)} /></div><div><Label htmlFor="trade-price">成交价格（元）</Label><Input id="trade-price" required inputMode="decimal" disabled={saving} value={price} onChange={event => setPrice(event.target.value)} /></div><div><Label htmlFor="trade-fees">全部费用（元）</Label><Input id="trade-fees" required inputMode="decimal" disabled={saving} value={fees} onChange={event => setFees(event.target.value)} /></div><div><Label htmlFor="trade-time">成交时间（北京时间）</Label><Input id="trade-time" required type="datetime-local" disabled={saving} value={time} onChange={event => setTime(event.target.value)} /></div></div>
    <div className="rounded border bg-muted/30 p-3 text-sm" aria-live="polite">{preview ? <dl className="grid grid-cols-2 gap-2"><dt>成交金额</dt><dd className="text-right tabular-nums">{Number(preview.amount).toFixed(2)} 元</dd><dt>{side === "buy" ? "现金支出" : "现金收入"}</dt><dd className="text-right tabular-nums">{Number(preview.cash_amount).toFixed(2)} 元</dd><dt>操作后持仓</dt><dd className="text-right">{preview.shares} 股</dd><dt>操作后成本价</dt><dd className="text-right">{Number(preview.cost_price).toFixed(4)} 元</dd>{side === "sell" && <><dt>本次已实现盈亏</dt><dd className="text-right">{Number(preview.realized_pnl).toFixed(2)} 元</dd></>}</dl> : "填写数量、成交价格和费用后显示计算结果"}</div><div className="flex justify-end gap-2"><Button type="button" variant="outline" disabled={saving} onClick={onClose}>取消</Button><Button type="submit" disabled={saving || !preview}>{saving ? "登记中…" : "登记成交"}</Button></div></form></DialogContent></Dialog>;
}

interface TradeRow { id: string; symbol: string; side: string; quantity: string; price: string; fees: string; amount: string; realized_pnl: string; traded_at: string }
export function HoldingTradeHistory({ revision }: { revision: number }) {
  const [rows, setRows] = useState<TradeRow[]>([]);
  const [error, setError] = useState("");
  useEffect(() => { const abort = new AbortController(); fetch("/api/holdings/trades", { signal: abort.signal }).then(async response => { const data = await response.json(); if (!response.ok) throw new Error(data.error); setRows(data.trades); setError(""); }).catch(err => { if (!abort.signal.aborted) setError(String(err)); }); return () => abort.abort(); }, [revision]);
  return <section className="rounded border p-4"><h2 className="mb-3 text-sm font-semibold">最近成交记录（含已清仓股票）</h2>{error && <p role="alert" className="text-sm text-destructive">{error}</p>}<div className="overflow-x-auto"><table className="w-full min-w-[680px] text-sm"><thead><tr>{["成交时间", "股票", "方向", "数量", "成交价", "成交金额", "费用", "已实现盈亏"].map(label => <th key={label} className="border-b p-2 text-left font-medium">{label}</th>)}</tr></thead><tbody>{rows.map(row => <tr key={row.id} className="border-b"><td className="p-2">{row.traded_at}</td><td>{row.symbol}</td><td>{row.side === "buy" ? "买入" : "卖出"}</td><td>{row.quantity}</td><td>{Number(row.price).toFixed(3)}</td><td>{Number(row.amount).toFixed(2)}</td><td>{Number(row.fees).toFixed(2)}</td><td>{row.side === "sell" ? Number(row.realized_pnl).toFixed(2) : "—"}</td></tr>)}</tbody></table>{!rows.length && <p className="py-4 text-sm text-muted-foreground">暂无成交记录；现有持仓视为期初快照，历史买卖未推算。</p>}</div></section>;
}
