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
import { Pencil, Plus, RefreshCw, Trash2, Wallet, X } from "lucide-react";

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

export default function HoldingsPage() {
  const [holdings, setHoldings] = useState<HoldingRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editing, setEditing] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/holdings");
      if (!response.ok) throw new Error("加载持仓失败");
      const data = (await response.json()) as { holdings: HoldingRow[] };
      setHoldings(data.holdings);
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
    if (!window.confirm(`确认删除 ${symbol} 的持仓记录？`)) return;
    try {
      const response = await fetch(`/api/holdings/${encodeURIComponent(symbol)}`, { method: "DELETE" });
      if (!response.ok) throw new Error("删除失败");
      if (editing === symbol) resetForm();
      toast.success("已删除");
      void load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除失败");
    }
  };

  const totalAmount = holdings.reduce((sum, row) => sum + (row.total_amount || 0), 0);
  const autoTotal = computedTotal();

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">我的持仓</h1>
        <p className="text-sm text-muted-foreground">
          手动维护持仓信息；个股分析与 AI 解读时会带上相关持仓，供分析参考。
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Wallet className="size-4" />
            {editing ? `编辑持仓：${editing}` : "添加持仓"}
          </CardTitle>
          <CardDescription>
            总金额可手动填写，留空则自动按 持仓份额 × 持仓价 计算。
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
              <Label htmlFor="h-total">总金额（元）</Label>
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
            共 {holdings.length} 只，总金额 {fmtMoney(totalAmount)} 元。
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
                <TableHead className="text-right">总金额（元）</TableHead>
                <TableHead className="w-28">操作</TableHead>
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
                      <Badge variant="outline" className="ml-1">
                        未填
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1">
                      <Button variant="outline" size="sm" onClick={() => startEdit(row)}>
                        <Pencil className="size-3.5" />
                        编辑
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="text-red-600 hover:text-red-700"
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
                    {loading ? "加载中..." : "暂无持仓，先在上方添加"}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
