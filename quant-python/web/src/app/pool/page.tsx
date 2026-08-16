"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { CheckCircle2, FileImage, FileText, Filter, Plus, RefreshCw, Trash2, XCircle } from "lucide-react";

interface PoolRow {
  symbol: string;
  name: string;
  source: string;
  created_at: string;
}

interface CandidateRow {
  symbol: string;
  name: string;
  score: number;
  confirmed_at?: string;
  dif?: number;
  dea?: number;
  zero_distance?: number;
  chan_signals?: unknown[];
}

interface PendingItem {
  id: number;
  kind: "text" | "image";
  raw: string;
  candidates: Array<{ symbol: string; name: string }>;
  created_at: string;
}

interface ImportDialogState {
  open: boolean;
  mode: "text" | "image";
  text: string;
  parsing: boolean;
  pendingId: number | null;
  symbols: Array<{ symbol: string; name: string }>;
  unknown: string[];
  imageUrl: string;
}

const EMPTY_DIALOG: ImportDialogState = {
  open: false,
  mode: "text",
  text: "",
  parsing: false,
  pendingId: null,
  symbols: [],
  unknown: [],
  imageUrl: "",
};

export default function PoolPage() {
  const [pool, setPool] = useState<PoolRow[]>([]);
  const [pending, setPending] = useState<PendingItem[]>([]);
  const [newSymbol, setNewSymbol] = useState("");
  const [newName, setNewName] = useState("");
  const [dialog, setDialog] = useState<ImportDialogState>(EMPTY_DIALOG);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [candidates, setCandidates] = useState<CandidateRow[]>([]);
  const [candidateMeta, setCandidateMeta] = useState<{ ttl_business_days?: number; capacity?: number }>({});
  const [scanning, setScanning] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [poolResponse, pendingResponse] = await Promise.all([
        fetch("/api/pool"),
        fetch("/api/pool/import/pending"),
      ]);
      const poolData = (await poolResponse.json()) as { pool: PoolRow[] };
      const pendingData = (await pendingResponse.json()) as { pending: PendingItem[] };
      setPool(poolData.pool);
      setPending(pendingData.pending);
      const candidatesResponse = await fetch("/api/candidates").catch(() => null);
      if (candidatesResponse?.ok) {
        const candidatesData = (await candidatesResponse.json()) as {
          candidates?: CandidateRow[];
          ttl_business_days?: number;
          capacity?: number;
        };
        setCandidates(candidatesData.candidates ?? []);
        setCandidateMeta({
          ttl_business_days: candidatesData.ttl_business_days,
          capacity: candidatesData.capacity,
        });
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载股票池失败");
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const addSymbol = async () => {
    if (!newSymbol.trim()) {
      toast.error("请输入股票代码");
      return;
    }
    try {
      const response = await fetch("/api/pool", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: [{ symbol: newSymbol.trim(), name: newName.trim() }] }),
      });
      const data = (await response.json().catch(() => ({}))) as { error?: string };
      if (!response.ok) throw new Error(data.error || "添加失败");
      toast.success("已加入股票池");
      setNewSymbol("");
      setNewName("");
      void load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "添加失败");
    }
  };

  const removeSymbol = async (symbol: string) => {
    if (!window.confirm(`确认从股票池删除 ${symbol}？`)) return;
    try {
      const response = await fetch(`/api/pool/${encodeURIComponent(symbol)}`, { method: "DELETE" });
      if (!response.ok) throw new Error("删除失败");
      toast.success("已删除");
      void load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除失败");
    }
  };

  const runScan = async (mode: "watchlist" | "all_a") => {
    setScanning(mode);
    try {
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "scan", universe_mode: mode, notify: false }),
      });
      const data = (await response.json().catch(() => ({}))) as { error?: string; jobId?: number };
      if (!response.ok) throw new Error(data.error || "启动筛选失败");
      toast.success(
        mode === "all_a" ? "全市场零轴金叉筛选已启动，可在结果页查看进度" : "自选池零轴金叉筛选已启动，可在结果页查看进度"
      );
      setTimeout(() => void load(), 8000);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动筛选失败");
    } finally {
      setScanning(null);
    }
  };

  const openTextImport = () => {
    setDialog({ ...EMPTY_DIALOG, open: true, mode: "text" });
  };

  const openImageImport = () => {
    setDialog({ ...EMPTY_DIALOG, open: true, mode: "image" });
    setTimeout(() => fileInputRef.current?.click(), 50);
  };

  const parseText = async () => {
    if (!dialog.text.trim()) {
      toast.error("请输入股票列表文本");
      return;
    }
    setDialog((prev) => ({ ...prev, parsing: true }));
    try {
      const response = await fetch("/api/pool/import/text", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: dialog.text }),
      });
      const data = (await response.json().catch(() => ({}))) as {
        error?: string;
        pending_id?: number;
        symbols?: Array<{ symbol: string; name: string }>;
        unknown?: string[];
      };
      if (!response.ok) throw new Error(data.error || "解析失败");
      setDialog((prev) => ({
        ...prev,
        parsing: false,
        pendingId: data.pending_id ?? null,
        symbols: data.symbols ?? [],
        unknown: data.unknown ?? [],
      }));
    } catch (error) {
      setDialog((prev) => ({ ...prev, parsing: false }));
      toast.error(error instanceof Error ? error.message : "解析失败");
    }
  };

  const handleImageFile = async (file: File | null) => {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      toast.error("请选择图片文件");
      return;
    }
    setDialog((prev) => ({ ...prev, parsing: true }));
    const reader = new FileReader();
    reader.onload = async () => {
      const dataUrl = String(reader.result ?? "");
      try {
        const response = await fetch("/api/pool/import/image", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ dataUrl }),
        });
        const data = (await response.json().catch(() => ({}))) as {
          error?: string;
          pending_id?: number;
          candidates?: Array<{ symbol: string; name: string }>;
        };
        if (!response.ok) throw new Error(data.error || "图片识别失败");
        setDialog((prev) => ({
          ...prev,
          parsing: false,
          pendingId: data.pending_id ?? null,
          symbols: data.candidates ?? [],
          unknown: [],
          imageUrl: dataUrl.slice(0, 100000),
        }));
      } catch (error) {
        setDialog((prev) => ({ ...prev, parsing: false }));
        toast.error(error instanceof Error ? error.message : "图片识别失败");
      }
    };
    reader.onerror = () => {
      setDialog((prev) => ({ ...prev, parsing: false }));
      toast.error("读取图片失败");
    };
    reader.readAsDataURL(file);
  };

  const confirmImport = async () => {
    if (dialog.pendingId === null) return;
    try {
      const response = await fetch("/api/pool/import/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pending_id: dialog.pendingId }),
      });
      const data = (await response.json().catch(() => ({}))) as { error?: string; added?: number };
      if (!response.ok) throw new Error(data.error || "确认失败");
      toast.success(`已导入 ${data.added ?? 0} 只股票`);
      setDialog(EMPTY_DIALOG);
      void load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "确认失败");
    }
  };

  const cancelImport = async () => {
    if (dialog.pendingId !== null) {
      try {
        await fetch(`/api/pool/import/pending/${dialog.pendingId}`, { method: "DELETE" });
      } catch {
        /* ignore */
      }
    }
    setDialog(EMPTY_DIALOG);
    void load();
  };

  const confirmPending = async (item: PendingItem) => {
    try {
      const response = await fetch("/api/pool/import/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pending_id: item.id }),
      });
      const data = (await response.json().catch(() => ({}))) as { error?: string; added?: number };
      if (!response.ok) throw new Error(data.error || "确认失败");
      toast.success(`已导入 ${data.added ?? 0} 只股票`);
      void load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "确认失败");
    }
  };

  const cancelPending = async (item: PendingItem) => {
    try {
      await fetch(`/api/pool/import/pending/${item.id}`, { method: "DELETE" });
      void load();
    } catch {
      /* ignore */
    }
  };

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">股票池</h1>
          <p className="text-sm text-muted-foreground">文本导入解析后需确认；图片导入由视觉模型识别，识别结果同样先确认再入库。</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={openTextImport}><FileText className="size-4" /> 文本导入</Button>
          <Button onClick={openImageImport}><FileImage className="size-4" /> 图片导入</Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>手动添加</CardTitle>
        </CardHeader>
        <CardContent className="flex items-end gap-3">
          <div className="flex flex-1 flex-col gap-1.5">
            <Label>股票代码</Label>
            <Input value={newSymbol} onChange={(event) => setNewSymbol(event.target.value)} placeholder="600036 或 600036.SH" />
          </div>
          <div className="flex flex-1 flex-col gap-1.5">
            <Label>名称（可选）</Label>
            <Input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="招商银行" />
          </div>
          <Button onClick={() => void addSymbol()}><Plus className="size-4" /> 添加</Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>股票池（{pool.length} 只）</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>代码</TableHead>
                <TableHead>名称</TableHead>
                <TableHead>来源</TableHead>
                <TableHead>添加时间</TableHead>
                <TableHead className="w-20">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pool.map((item) => (
                <TableRow key={item.symbol}>
                  <TableCell className="font-mono text-xs">{item.symbol}</TableCell>
                  <TableCell>{item.name || "-"}</TableCell>
                  <TableCell><Badge variant="outline">{item.source}</Badge></TableCell>
                  <TableCell className="text-xs text-muted-foreground">{item.created_at}</TableCell>
                  <TableCell>
                    <Button variant="ghost" size="sm" onClick={() => void removeSymbol(item.symbol)}>
                      <Trash2 className="size-3.5" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
              {pool.length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="text-center text-muted-foreground">
                    股票池为空
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-3">
          <div>
            <CardTitle>指标股票池（日线零轴金叉）</CardTitle>
            <CardDescription>
              扫描生成的候选股：全市场或自选池日线零轴金叉筛选结果，保留{" "}
              {candidateMeta.ttl_business_days ?? 5} 个交易日，最多{" "}
              {candidateMeta.capacity ?? 100} 只。后续监控循环会对其做缠论买卖点分析。
            </CardDescription>
          </div>
          <div className="flex shrink-0 gap-2">
            <Button variant="outline" size="sm" onClick={() => void load()}>
              <RefreshCw className="size-3.5" /> 刷新
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={scanning !== null}
              onClick={() => void runScan("watchlist")}
            >
              <Filter className="size-3.5" /> 筛选自选池
            </Button>
            <Button
              size="sm"
              disabled={scanning !== null}
              onClick={() => void runScan("all_a")}
            >
              <Filter className="size-3.5" /> 全市场筛选
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>代码</TableHead>
                <TableHead>名称</TableHead>
                <TableHead>评分</TableHead>
                <TableHead>确认时间</TableHead>
                <TableHead>零轴距离</TableHead>
                <TableHead>缠论信号</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {candidates.map((item) => (
                <TableRow key={item.symbol}>
                  <TableCell className="font-mono text-xs">{item.symbol}</TableCell>
                  <TableCell>{item.name || "-"}</TableCell>
                  <TableCell>{item.score}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{item.confirmed_at || "-"}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {item.zero_distance != null ? item.zero_distance.toFixed(5) : "-"}
                  </TableCell>
                  <TableCell className="text-xs">
                    {Array.isArray(item.chan_signals) && item.chan_signals.length > 0
                      ? item.chan_signals.map((signal) => String(signal)).join(", ")
                      : "-"}
                  </TableCell>
                </TableRow>
              ))}
              {candidates.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground">
                    暂无候选，点击「筛选自选池」或「全市场筛选」生成
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {pending.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>待确认导入（{pending.length}）</CardTitle>
            <CardDescription>识别/解析结果需人工确认后才写入股票池。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {pending.map((item) => (
              <div key={item.id} className="flex items-start justify-between gap-3 rounded-lg border p-3">
                <div>
                  <div className="text-sm font-medium">
                    {item.kind === "image" ? "图片导入" : "文本导入"} · #{item.id}
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1.5">
                    {item.candidates.map((candidate) => (
                      <Badge key={candidate.symbol} variant="secondary">
                        {candidate.symbol}{candidate.name ? ` ${candidate.name}` : ""}
                      </Badge>
                    ))}
                  </div>
                </div>
                <div className="flex gap-2">
                  <Button size="sm" onClick={() => void confirmPending(item)}><CheckCircle2 className="size-3.5" /> 确认</Button>
                  <Button size="sm" variant="ghost" onClick={() => void cancelPending(item)}><XCircle className="size-3.5" /> 取消</Button>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Dialog open={dialog.open} onOpenChange={(open) => !open && void cancelImport()}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{dialog.mode === "image" ? "图片导入" : "文本导入"}</DialogTitle>
            <DialogDescription>
              {dialog.mode === "image" ? "图片由已启用的视觉模型识别，结果需确认后入库。" : "支持逗号/空格/换行分隔，可带名称，如：600036 招商银行。"}
            </DialogDescription>
          </DialogHeader>
          {dialog.mode === "text" && !dialog.pendingId && (
            <>
              <Textarea
                className="min-h-40"
                value={dialog.text}
                onChange={(event) => setDialog((prev) => ({ ...prev, text: event.target.value }))}
                placeholder={"600036 招商银行\n000001.SZ 平安银行\n600519,601318"}
              />
              <Button onClick={() => void parseText()} disabled={dialog.parsing}>
                {dialog.parsing ? "解析中..." : "解析预览"}
              </Button>
            </>
          )}
          {dialog.mode === "image" && !dialog.pendingId && (
            <div className="flex flex-col gap-3">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(event) => void handleImageFile(event.target.files?.[0] ?? null)}
              />
              {dialog.parsing ? (
                <p className="text-sm text-muted-foreground">正在调用视觉模型识别...</p>
              ) : (
                <Button onClick={() => fileInputRef.current?.click()}>选择图片</Button>
              )}
            </div>
          )}
          {dialog.pendingId && (
            <div className="flex flex-col gap-3">
              {dialog.imageUrl && (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={dialog.imageUrl} alt="待识别图片" className="max-h-48 rounded-lg border object-contain" />
              )}
              {dialog.unknown.length > 0 && (
                <div className="text-sm text-muted-foreground">
                  未识别：{dialog.unknown.join("、")}
                </div>
              )}
              <div className="text-sm font-medium">识别到 {dialog.symbols.length} 只：</div>
              <div className="flex flex-wrap gap-1.5">
                {dialog.symbols.map((item) => (
                  <Badge key={item.symbol} variant="secondary">{item.symbol}{item.name ? ` ${item.name}` : ""}</Badge>
                ))}
              </div>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => void cancelImport()}>取消</Button>
            <Button onClick={() => void confirmImport()} disabled={!dialog.pendingId}>确认导入</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
