"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ArrowRight, TrendingDown, TrendingUp } from "lucide-react";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { calculateTrade } from "@/lib/trade-math";
import type { HoldingRow } from "@/lib/types";

export function HoldingTradeDialog({
  holding,
  action,
  onClose,
  onSaved,
}: {
  holding: HoldingRow;
  action: "buy" | "sell" | "close";
  onClose: () => void;
  onSaved: () => void;
}) {
  const [quantity, setQuantity] = useState(
    action === "close" ? String(holding.shares) : "100"
  );
  const [price, setPrice] = useState("");
  const [fees, setFees] = useState("0");
  const [time, setTime] = useState(() =>
    new Date()
      .toLocaleString("sv-SE", { timeZone: "Asia/Shanghai" })
      .replace(" ", "T")
      .slice(0, 16)
  );
  const [requestKey] = useState(() => crypto.randomUUID());
  const [saving, setSaving] = useState(false);
  const side = action === "buy" ? "buy" : "sell";

  let preview: ReturnType<typeof calculateTrade> | null = null;
  try {
    preview = calculateTrade(holding, {
      side,
      quantity: Number(quantity),
      price,
      fees,
    });
  } catch {
    /* inline preview waits for valid values */
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!preview || saving) return;
    setSaving(true);
    try {
      const response = await fetch("/api/holdings/trades", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: holding.symbol,
          side,
          quantity: Number(quantity),
          price,
          fees,
          traded_at: time,
          request_key: requestKey,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error ?? "登记失败");
      toast.success("成交已登记，持仓已更新");
      onSaved();
      onClose();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "登记失败");
    } finally {
      setSaving(false);
    }
  };

  const actionLabel = action === "buy" ? "加仓" : action === "sell" ? "减仓" : "清仓";
  const pnl = preview && side === "sell" ? Number(preview.realized_pnl) : null;

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !saving) onClose();
      }}
    >
      <DialogContent className="flex max-h-[90vh] sm:max-w-lg flex-col gap-0 p-0 overflow-hidden">
        <DialogHeader className="shrink-0 border-b p-4 pr-12">
          <div className="flex items-center gap-2">
            <DialogTitle>
              {actionLabel} · {holding.name || holding.symbol}
            </DialogTitle>
            <Badge
              className={
                action === "buy"
                  ? "border-rose-500/30 bg-rose-500/10 text-rose-600"
                  : "border-emerald-500/30 bg-emerald-500/10 text-emerald-600"
              }
            >
              {actionLabel}
            </Badge>
          </div>
          <DialogDescription className="text-xs">
            登记真实成交。当前持有{" "}
            <span className="font-mono font-medium text-foreground">{holding.shares}</span> 股，
            成本价{" "}
            <span className="font-mono font-medium text-foreground">
              {holding.cost_price.toFixed(3)}
            </span>{" "}
            元。
          </DialogDescription>
        </DialogHeader>

        <form id="trade-form" onSubmit={submit} className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="trade-quantity" className="text-xs">成交数量（股）</Label>
              <Input
                id="trade-quantity"
                required
                type="number"
                min="1"
                step="1"
                disabled={saving || action === "close"}
                value={quantity}
                onChange={(event) => setQuantity(event.target.value)}
                className="font-mono text-sm"
              />
            </div>
            <div>
              <Label htmlFor="trade-price" className="text-xs">成交价格（元）</Label>
              <Input
                id="trade-price"
                required
                inputMode="decimal"
                disabled={saving}
                value={price}
                placeholder="例如 28.50"
                onChange={(event) => setPrice(event.target.value)}
                className="font-mono text-sm"
              />
            </div>
            <div>
              <Label htmlFor="trade-fees" className="text-xs">全部费用/手续费（元）</Label>
              <Input
                id="trade-fees"
                required
                inputMode="decimal"
                disabled={saving}
                value={fees}
                onChange={(event) => setFees(event.target.value)}
                className="font-mono text-sm"
              />
            </div>
            <div>
              <Label htmlFor="trade-time" className="text-xs">成交时间（北京时间）</Label>
              <Input
                id="trade-time"
                required
                type="datetime-local"
                disabled={saving}
                value={time}
                onChange={(event) => setTime(event.target.value)}
                className="text-xs"
              />
            </div>
          </div>

          {/* 实时推演前后对比卡片 */}
          <div
            className="rounded-lg border bg-muted/30 p-3.5 text-xs"
            aria-live="polite"
          >
            <div className="mb-2 font-medium text-muted-foreground">
              操作试算与持仓变化推演
            </div>
            {preview ? (
              <div className="space-y-2">
                <div className="grid grid-cols-2 gap-2 text-xs">
                  <div className="rounded border bg-background p-2">
                    <span className="text-muted-foreground block text-[11px]">成交金额</span>
                    <span className="font-mono font-semibold text-sm">
                      ¥{Number(preview.amount).toFixed(2)}
                    </span>
                  </div>
                  <div className="rounded border bg-background p-2">
                    <span className="text-muted-foreground block text-[11px]">
                      {side === "buy" ? "现金支出" : "现金回流"}
                    </span>
                    <span className="font-mono font-semibold text-sm">
                      ¥{Number(preview.cash_amount).toFixed(2)}
                    </span>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-2 text-xs">
                  <div className="rounded border bg-background p-2">
                    <span className="text-muted-foreground block text-[11px]">持仓份额变动</span>
                    <div className="flex items-center gap-1 font-mono text-xs font-medium">
                      <span>{holding.shares} 股</span>
                      <ArrowRight className="size-3 text-muted-foreground" />
                      <span className="font-bold text-foreground">{preview.shares} 股</span>
                    </div>
                  </div>

                  <div className="rounded border bg-background p-2">
                    <span className="text-muted-foreground block text-[11px]">成本价变动</span>
                    <div className="flex items-center gap-1 font-mono text-xs font-medium">
                      <span>¥{holding.cost_price.toFixed(3)}</span>
                      <ArrowRight className="size-3 text-muted-foreground" />
                      <span className="font-bold text-foreground">
                        ¥{Number(preview.cost_price).toFixed(3)}
                      </span>
                    </div>
                  </div>
                </div>

                {side === "sell" && pnl !== null && (
                  <div
                    className={`flex items-center justify-between rounded border p-2.5 ${
                      pnl > 0
                        ? "border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400"
                        : pnl < 0
                        ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                        : "border-border bg-background"
                    }`}
                  >
                    <span className="font-medium text-xs">本次已实现盈亏</span>
                    <span className="font-mono font-bold text-sm">
                      {pnl > 0 ? `+${pnl.toFixed(2)}` : pnl.toFixed(2)} 元
                    </span>
                  </div>
                )}
              </div>
            ) : (
              <p className="py-2 text-center text-muted-foreground">
                填写成交数量与单价后，此处将自动试算变动推演
              </p>
            )}
          </div>
        </form>

        <DialogFooter className="shrink-0 m-0 border-t bg-muted/50 p-4">
          <Button type="button" variant="outline" disabled={saving} onClick={onClose}>
            取消
          </Button>
          <Button form="trade-form" type="submit" disabled={saving || !preview}>
            {saving ? "登记中…" : "确认登记成交"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface TradeRow {
  id: string;
  symbol: string;
  side: string;
  quantity: string;
  price: string;
  fees: string;
  amount: string;
  realized_pnl: string;
  traded_at: string;
}

export function HoldingTradeHistory({ revision }: { revision: number }) {
  const [rows, setRows] = useState<TradeRow[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    const abort = new AbortController();
    fetch("/api/holdings/trades", { signal: abort.signal })
      .then(async (response) => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.error);
        setRows(data.trades);
        setError("");
      })
      .catch((err) => {
        if (!abort.signal.aborted) setError(String(err));
      });
    return () => abort.abort();
  }, [revision]);

  return (
    <section className="rounded-xl border bg-card p-4 shadow-xs">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold">最近成交记录（含已清仓标的）</h2>
        <span className="text-xs text-muted-foreground">共 {rows.length} 笔记录</span>
      </div>

      {error && (
        <p role="alert" className="mb-3 text-sm text-destructive">
          {error}
        </p>
      )}

      <div className="overflow-x-auto">
        <table className="w-full min-w-[680px] text-xs">
          <thead>
            <tr className="border-b bg-muted/40 text-muted-foreground">
              <th className="p-2.5 text-left font-medium">成交时间</th>
              <th className="p-2.5 text-left font-medium">标的代码</th>
              <th className="p-2.5 text-left font-medium">交易方向</th>
              <th className="p-2.5 text-right font-medium">数量（股）</th>
              <th className="p-2.5 text-right font-medium">成交价（元）</th>
              <th className="p-2.5 text-right font-medium">成交金额（元）</th>
              <th className="p-2.5 text-right font-medium">费用（元）</th>
              <th className="p-2.5 text-right font-medium">已实现盈亏</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {rows.map((row) => {
              const pnl = Number(row.realized_pnl);
              const isSell = row.side === "sell";
              return (
                <tr key={row.id} className="transition-colors hover:bg-muted/20">
                  <td className="p-2.5 text-muted-foreground">{row.traded_at}</td>
                  <td className="p-2.5 font-mono font-medium">{row.symbol}</td>
                  <td className="p-2.5">
                    {row.side === "buy" ? (
                      <Badge className="border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400">
                        买入
                      </Badge>
                    ) : (
                      <Badge className="border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
                        卖出
                      </Badge>
                    )}
                  </td>
                  <td className="p-2.5 text-right font-mono tabular-nums">{row.quantity}</td>
                  <td className="p-2.5 text-right font-mono tabular-nums">
                    {Number(row.price).toFixed(3)}
                  </td>
                  <td className="p-2.5 text-right font-mono tabular-nums">
                    {Number(row.amount).toFixed(2)}
                  </td>
                  <td className="p-2.5 text-right font-mono tabular-nums text-muted-foreground">
                    {Number(row.fees).toFixed(2)}
                  </td>
                  <td className="p-2.5 text-right font-mono font-semibold tabular-nums">
                    {isSell ? (
                      pnl > 0 ? (
                        <span className="text-rose-600 dark:text-rose-400">
                          +{pnl.toFixed(2)}
                        </span>
                      ) : pnl < 0 ? (
                        <span className="text-emerald-600 dark:text-emerald-400">
                          {pnl.toFixed(2)}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">0.00</span>
                      )
                    ) : (
                      <span className="text-muted-foreground/60">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!rows.length && (
          <p className="py-6 text-center text-sm text-muted-foreground">
            暂无登记成交记录；现有持仓视为期初快照。
          </p>
        )}
      </div>
    </section>
  );
}
