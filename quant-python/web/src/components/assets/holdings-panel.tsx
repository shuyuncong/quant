"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Coins, Pencil, PieChart, Plus, RefreshCw, ShieldCheck, Trash2, Wallet, X } from "lucide-react";
import { HoldingTradeDialog, HoldingTradeHistory } from "@/components/holding-trade-dialog";

interface HoldingRow {
  symbol: string;
  name: string;
  shares: number;
  cost_price: number;
  total_amount: number;
  created_at: string;
  updated_at: string;
}

const EMPTY_FORM = {
  symbol: "",
  name: "",
  shares: "",
  costPrice: "",
  totalAmount: "",
};

function fmtShares(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function fmtMoney(value: number): string {
  return value.toFixed(2);
}

export function HoldingsPanel() {
  const [trade, setTrade] = useState<{ holding: HoldingRow; action: "buy" | "sell" | "close" } | null>(null);
  const [tradeRevision, setTradeRevision] = useState(0);
  const [holdings, setHoldings] = useState<HoldingRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editing, setEditing] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedCapital, setSavedCapital] = useState(0);
  const [capitalInput, setCapitalInput] = useState("");
  const [savingCapital, setSavingCapital] = useState(false);
  // 首屏加载完成前一律显示「—」，绝不把「还没读到」渲染成 0 元 / 未设置。
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/holdings");
      if (!response.ok) throw new Error("加载持仓失败");
      const data = (await response.json()) as {
        holdings: HoldingRow[];
        total_capital?: number;
      };
      setHoldings(data.holdings.filter(row => row.shares > 0));
      setSavedCapital(Number(data.total_capital ?? 0));
      setLoaded(true);
      // 轮询不覆盖正在编辑的输入，仅在尚未填写时同步已保存值
      setCapitalInput((prev) =>
        prev === "" && Number(data.total_capital ?? 0) > 0 ? String(data.total_capital) : prev
      );
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载持仓失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
    const timer = setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [load]);

  const computedTotal = useCallback((): number => {
    const shares = Number(form.shares);
    const cost = Number(form.costPrice);
    if (shares > 0 && cost > 0) return Math.round(shares * cost * 100) / 100;
    return 0;
  }, [form.shares, form.costPrice]);

  const setField = (key: keyof typeof EMPTY_FORM, value: string) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const startEdit = (row: HoldingRow) => {
    setEditing(row.symbol);
    setForm({
      symbol: row.symbol,
      name: row.name,
      shares: fmtShares(row.shares),
      costPrice: String(row.cost_price),
      totalAmount: row.total_amount > 0 ? String(row.total_amount) : "",
    });
  };

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setEditing(null);
  };

  const save = async () => {
    if (!form.symbol.trim()) {
      toast.error("请输入股票代码");
      return;
    }
    const shares = Number(form.shares);
    const cost = Number(form.costPrice);
    if (!Number.isFinite(shares) || shares < 0 || !Number.isFinite(cost) || cost < 0) {
      toast.error("请填写合法的持仓份额与持仓价");
      return;
    }
    setSaving(true);
    try {
      const payload = {
        name: form.name.trim(),
        shares,
        cost_price: cost,
        total_amount: form.totalAmount.trim() ? Number(form.totalAmount) : 0,
      };
      const url = editing
        ? `/api/holdings/${encodeURIComponent(editing)}`
        : "/api/holdings";
      const response = await fetch(url, {
        method: editing ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          editing ? payload : { symbol: form.symbol.trim(), ...payload }
        ),
      });
      const data = (await response.json().catch(() => ({}))) as { error?: string };
      if (!response.ok) throw new Error(data.error || "保存失败");
      toast.success(editing ? "已更新持仓" : "已添加持仓");
      resetForm();
      void load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (symbol: string) => {
    if (!window.confirm(`确认将 ${symbol} 的持仓校正为 0？此操作不代表卖出，实际清仓请使用“清仓”登记成交。`)) return;
    try {
      const response = await fetch(`/api/holdings/${encodeURIComponent(symbol)}`, { method: "DELETE" });
      if (!response.ok) throw new Error("校正失败");
      if (editing === symbol) resetForm();
      toast.success("持仓已校正为 0，成交历史保留");
      void load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "校正失败");
    }
  };

  const totalAmount = holdings.reduce((sum, row) => sum + (row.total_amount || 0), 0);
  const capitalPct =
    savedCapital > 0 ? ((totalAmount / savedCapital) * 100).toFixed(1) : "";
  // 可用现金是「设定资金 − 账面占用」，与券商实际可用资金无关，这里只做预算口径估算。
  const remainingAllocation = Math.max(0, savedCapital - totalAmount);
  const capitalKnown = loaded && savedCapital > 0;
  const unavailableMessage = loading ? "加载中…" : "未能读取，请刷新重试";
  const money = (value: number) => `¥${fmtMoney(value)}`;
  const autoTotal = computedTotal();

  const saveCapital = async () => {
    const value = Number(capitalInput);
    if (capitalInput.trim() !== "" && (!Number.isFinite(value) || value < 0)) {
      toast.error("请输入合法的设定总资金");
      return;
    }
    setSavingCapital(true);
    try {
      const response = await fetch("/api/holdings/capital", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ total_capital: capitalInput.trim() === "" ? 0 : value }),
      });
      const data = (await response.json().catch(() => ({}))) as {
        error?: string;
        total_capital?: number;
      };
      if (!response.ok) throw new Error(data.error || "保存失败");
      const saved = Number(data.total_capital ?? 0);
      setSavedCapital(saved);
      setCapitalInput(saved > 0 ? String(saved) : "");
      toast.success(saved > 0 ? "已保存设定总资金（用于估算剩余配置额度）" : "已清除设定总资金");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSavingCapital(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="text-lg font-semibold">我的持仓</h2>
        <p className="text-sm text-muted-foreground">
          手动维护持仓信息与设定总资金（均为成本口径账面记录，本页不抓取行情）；分析任务（个股/扫描/监控）与 AI 解读会带上相关持仓与账面仓位占比，供分析参考。
        </p>
      </div>

      {/* 顶部资产概览驾驶舱（全部为成本口径的账面记录，不含实时行情） */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-xl border bg-card p-4 shadow-xs">
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>设定总资金（手工基准）</span>
            <Wallet className="size-4 opacity-70" />
          </div>
          <div className="mt-2 text-xl font-bold tracking-tight sm:text-2xl">
            {!loaded ? "—" : capitalKnown ? money(savedCapital) : "未设置"}
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            {!loaded
              ? unavailableMessage
              : capitalKnown
              ? "用于估算剩余配置额度，非券商绑定资金"
              : "未设置：无法估算剩余配置额度与账面仓位占比"}
          </p>
        </div>

        <div className="rounded-xl border bg-card p-4 shadow-xs">
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>持仓账面金额（成本口径）</span>
            <Coins className="size-4 text-rose-500 opacity-80" />
          </div>
          <div className="mt-2 text-xl font-bold tracking-tight text-rose-600 dark:text-rose-400 sm:text-2xl">
            {!loaded ? "—" : money(totalAmount)}
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            {!loaded ? unavailableMessage : `共 ${holdings.length} 只在持标的 · 按登记成本记账，非实时市值`}
          </p>
        </div>

        <div className="rounded-xl border bg-card p-4 shadow-xs">
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>预估剩余可用额度</span>
            <ShieldCheck className="size-4 opacity-70" />
          </div>
          <div className="mt-2 text-xl font-bold tracking-tight sm:text-2xl">
            {capitalKnown ? money(remainingAllocation) : "—"}
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            {!loaded
              ? unavailableMessage
              : capitalKnown
              ? savedCapital < totalAmount
                ? "⚠️ 账面金额已超过设定总资金"
                : "＝设定总资金 − 持仓账面金额，非券商可用资金"
              : "需先设定总资金才能估算"}
          </p>
        </div>

        <div className="rounded-xl border bg-card p-4 shadow-xs">
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>账面仓位占比（成本口径）</span>
            <PieChart className="size-4 opacity-70" />
          </div>
          <div className="mt-2 flex items-baseline gap-2">
            <span className="text-xl font-bold tracking-tight sm:text-2xl">
              {capitalKnown && capitalPct ? `${capitalPct}%` : "—"}
            </span>
            {capitalKnown && capitalPct && (
              <Badge
                variant="outline"
                className={`text-[10px] ${
                  Number(capitalPct) > 75
                    ? "border-amber-500/30 bg-amber-500/10 text-amber-600"
                    : Number(capitalPct) > 30
                    ? "border-primary/20 bg-primary/5 text-primary"
                    : "text-muted-foreground"
                }`}
              >
                {Number(capitalPct) > 75 ? "重仓" : Number(capitalPct) > 30 ? "适中" : "轻仓"}
              </Badge>
            )}
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            {!loaded ? "加载中…" : "AI 解读将参考集中度建议（按成本口径估算）"}
          </p>
        </div>
      </div>

      <p className="text-[11px] leading-relaxed text-muted-foreground">
        口径说明：持仓是手工维护的账面记录，「持仓账面金额」按成本口径汇总（份额 × 持仓价，或手动填写的金额），不是实时市值，本页不抓取行情；
        「预估剩余可用额度」＝设定总资金 − 持仓账面金额，只是配置预算的估算值，不代表券商可用资金或可买入金额。
      </p>

      <Card>
        <CardHeader className="px-4 py-3">
          <div className="flex items-center justify-between">
            <CardTitle className="flex items-center gap-2 text-sm font-medium">
              <Wallet className="size-4" /> 资金配置与仓位基准
            </CardTitle>
            <span className="text-xs text-muted-foreground">
              {!loaded ? "加载中…" : capitalKnown ? `当前配置: ${money(savedCapital)}` : "未配置总资金"}
            </span>
          </div>
        </CardHeader>
        <CardContent className="px-4 pt-0 pb-3">
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2">
              <Label htmlFor="h-capital" className="shrink-0 text-xs">设定总资金（元）</Label>
              <Input
                id="h-capital"
                className="h-8 w-44 font-mono text-xs"
                placeholder="例如 100000"
                inputMode="decimal"
                value={capitalInput}
                onChange={(event) => setCapitalInput(event.target.value)}
              />
            </div>
            <Button size="sm" className="h-8 text-xs" onClick={() => void saveCapital()} disabled={savingCapital}>
              {savingCapital ? "保存中..." : "更新总资金"}
            </Button>
          </div>
          <p className="mt-2 text-[11px] text-muted-foreground">
            这里是手工基准资金，只用来估算剩余配置额度与账面占比；它不是券商账户余额，系统也不会同步真实资金。
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Wallet className="size-4" />
            {editing ? `编辑持仓：${editing}` : "添加持仓"}
          </CardTitle>
          <CardDescription>
            总金额按成本口径手动填写，留空则自动按 持仓份额 × 持仓价 计算；它不是实时市值，本页不抓取行情。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="h-symbol">股票代码</Label>
              <Input
                id="h-symbol"
                placeholder="600036.SH"
                value={form.symbol}
                disabled={editing !== null}
                onChange={(event) => setField("symbol", event.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="h-name">股票名称</Label>
              <Input
                id="h-name"
                placeholder="招商银行"
                value={form.name}
                onChange={(event) => setField("name", event.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="h-shares">持仓份额</Label>
              <Input
                id="h-shares"
                placeholder="1000"
                value={form.shares}
                onChange={(event) => setField("shares", event.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="h-cost">持仓价（元）</Label>
              <Input
                id="h-cost"
                placeholder="30.00"
                value={form.costPrice}
                onChange={(event) => setField("costPrice", event.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="h-total">总金额（元，成本口径）</Label>
              <Input
                id="h-total"
                placeholder={autoTotal > 0 ? fmtMoney(autoTotal) : "留空自动算"}
                value={form.totalAmount}
                onChange={(event) => setField("totalAmount", event.target.value)}
              />
            </div>
          </div>
          <div className="mt-4 flex items-center gap-2">
            <Button onClick={() => void save()} disabled={saving}>
              <Plus className="size-4" />
              {editing ? "保存修改" : "添加持仓"}
            </Button>
            {editing && (
              <Button variant="outline" onClick={resetForm}>
                <X className="size-4" />
                取消
              </Button>
            )}
            <Button variant="outline" onClick={() => void load()} disabled={loading}>
              <RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />
              刷新
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Wallet className="size-4" /> 持仓列表
          </CardTitle>
          <CardDescription>
            {!loaded ? (
              unavailableMessage
            ) : (
              <>
                共 {holdings.length} 只，持仓账面金额合计 {fmtMoney(totalAmount)} 元（成本口径）
                {capitalPct && savedCapital > 0 ? `，占设定总资金 ${capitalPct}%` : ""}
                。
              </>
            )}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>代码</TableHead>
                <TableHead>名称</TableHead>
                <TableHead className="text-right">持仓份额</TableHead>
                <TableHead className="text-right">持仓价（元）</TableHead>
                <TableHead className="text-right">账面金额（元，成本口径）</TableHead>
                <TableHead className="min-w-72">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {holdings.map((row) => (
                <TableRow key={row.symbol}>
                  <TableCell className="font-mono text-xs">{row.symbol}</TableCell>
                  <TableCell>{row.name || "-"}</TableCell>
                  <TableCell className="text-right font-mono text-xs">{fmtShares(row.shares)}</TableCell>
                  <TableCell className="text-right font-mono text-xs">{fmtMoney(row.cost_price)}</TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {fmtMoney(row.total_amount)}
                    {row.total_amount <= 0 && (
                      <Badge variant="outline" className="ml-1" title="未登记账面金额，成本口径下按 0 计">
                        未登记金额
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1">
                      <Button variant="outline" size="sm" onClick={() => setTrade({ holding: row, action: "buy" })}>加仓</Button>
                      <Button variant="outline" size="sm" onClick={() => setTrade({ holding: row, action: "sell" })}>减仓</Button>
                      <Button variant="outline" size="sm" onClick={() => setTrade({ holding: row, action: "close" })}>清仓</Button>
                      <Button variant="outline" size="sm" onClick={() => startEdit(row)}>
                        <Pencil className="size-3.5" />
                        编辑
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="text-destructive hover:text-destructive/80"
                        onClick={() => void remove(row.symbol)}
                      >
                        <Trash2 className="size-3.5" />
                        删除
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
              {holdings.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground">
                    {!loaded ? unavailableMessage : "暂无持仓，先在上方添加"}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <HoldingTradeHistory revision={tradeRevision} />
      {trade && <HoldingTradeDialog key={`${trade.holding.symbol}:${trade.action}`} holding={trade.holding} action={trade.action} onClose={() => setTrade(null)} onSaved={() => { setTradeRevision(value => value+1); void load(); }} />}
    </div>
  );
}
