"use client";
import { ScanProgress } from "@/components/scan-progress";

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
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { useRouter } from "next/navigation";
import {
  CandidateTable,
  type DivergenceCandidateRow,
  type MacdCandidateRow,
  type YearlineCandidateRow,
} from "@/components/candidate-table";
import {
  CheckCircle2,
  FileImage,
  FileText,
  Filter,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
  XCircle,
} from "lucide-react";

interface PoolRow {
  symbol: string;
  name: string;
  source: string;
  created_at: string;
}

interface ExpiredCandidateRow {
  symbol: string;
  name: string;
  score: number;
  expired_on: string;
  reason: string;
  updated_at: string;
  pool_type?: string;
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

type PoolType = "macd_zero_axis" | "yearline_pullback" | "macd_divergence";

interface PoolData {
  candidates: Array<
    MacdCandidateRow | YearlineCandidateRow | DivergenceCandidateRow
  >;
  ttl_business_days?: number;
  capacity?: number;
  config?: { min_volume_ratio?: number } | null;
  latestScan?: LatestScan | null;
  expired: ExpiredCandidateRow[];
  expiredCount: number;
}

interface LatestScan {
  scanned_at?: string | null;
  universe_mode?: string | null;
  coverage?: number | null;
  completed_round?: boolean | null;
  candidate_count?: number | null;
  condition_funnel?: {
    evaluated_symbols?: number;
    conditions?: Record<string, { count: number; rate: number }>;
  } | null;
  volume_ratio_distribution?: {
    count?: number;
    median?: number;
    p90?: number;
    max?: number;
    above_threshold?: number;
  } | null;
  report_file?: string | null;
}

interface ObservedData {
  candidates: MacdCandidateRow[];
  observedCount: number;
  scannedAt?: string | null;
  universeMode?: string | null;
  completedRound?: boolean;
  reportFile?: string | null;
}

const POOL_LABEL: Record<PoolType, string> = {
  macd_zero_axis: "日线零轴金叉",
  yearline_pullback: "年线趋势",
  macd_divergence: "零轴+底背离",
};

export function PoolPanel() {
  const router = useRouter();
  const [pool, setPool] = useState<PoolRow[]>([]);
  const [pending, setPending] = useState<PendingItem[]>([]);
  const [newSymbol, setNewSymbol] = useState("");
  const [newName, setNewName] = useState("");
  const [dialog, setDialog] = useState<ImportDialogState>(EMPTY_DIALOG);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [scanStrategy, setScanStrategy] = useState<PoolType | "all">("all");
  const [poolType, setPoolType] = useState<PoolType>("macd_zero_axis");
  const [poolData, setPoolData] = useState<Partial<Record<PoolType, PoolData>>>({});
  const [observedData, setObservedData] = useState<ObservedData>({
    candidates: [],
    observedCount: 0,
  });
  const [expiredOpen, setExpiredOpen] = useState(false);
  const [scanning, setScanning] = useState<string | null>(null);
  const [analyzingSymbol, setAnalyzingSymbol] = useState<string | null>(null);
  const [analyzingAll, setAnalyzingAll] = useState(false);
  const [searchFilter, setSearchFilter] = useState("");

  const triggerAnalyze = async (symbol: string, name?: string) => {
    setAnalyzingSymbol(symbol);
    try {
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: "analyze",
          symbols: [symbol],
          notify: true,
        }),
      });
      const data = (await response.json().catch(() => ({}))) as {
        ok?: boolean;
        jobId?: number;
        error?: string;
      };
      if (!response.ok) throw new Error(data.error || "启动分析失败");
      toast.success(`已为 ${name || symbol} 启动分析任务 #${data.jobId}`, {
        action: {
          label: "查看结果",
          onClick: () => router.push("/results"),
        },
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动分析失败");
    } finally {
      setAnalyzingSymbol(null);
    }
  };

  const triggerAnalyzeAll = async () => {
    if (pool.length === 0) return;
    setAnalyzingAll(true);
    try {
      const symbols = pool.map((item) => item.symbol);
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: "analyze",
          symbols,
          notify: true,
        }),
      });
      const data = (await response.json().catch(() => ({}))) as {
        ok?: boolean;
        jobId?: number;
        error?: string;
      };
      if (!response.ok) throw new Error(data.error || "启动批量分析失败");
      toast.success(`已为自选池全部 ${symbols.length} 只股票启动分析 #${data.jobId}`, {
        action: {
          label: "查看进度",
          onClick: () => router.push("/results"),
        },
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动批量分析失败");
    } finally {
      setAnalyzingAll(false);
    }
  };

  const loadPool = useCallback(async (type: PoolType) => {
    const response = await fetch(`/api/candidates?pool_type=${type}`).catch(() => null);
    if (response?.ok) {
      const data = (await response.json()) as {
        candidates?: Array<
          MacdCandidateRow | YearlineCandidateRow | DivergenceCandidateRow
        >;
        ttl_business_days?: number;
        capacity?: number;
        config?: { min_volume_ratio?: number } | null;
        latest_scan?: LatestScan | null;
        expired_candidates?: ExpiredCandidateRow[];
        expired_count?: number;
      };
      setPoolData((prev) => ({
        ...prev,
        [type]: {
          candidates: data.candidates ?? [],
          ttl_business_days: data.ttl_business_days,
          capacity: data.capacity,
          config: data.config ?? null,
          latestScan: data.latest_scan ?? null,
          expired: data.expired_candidates ?? [],
          expiredCount: data.expired_count ?? 0,
        },
      }));
    }
  }, []);

  const loadObserved = useCallback(async () => {
    const response = await fetch("/api/candidates/observed").catch(() => null);
    if (!response?.ok) return;
    const data = (await response.json()) as {
      candidates?: MacdCandidateRow[];
      observed_count?: number;
      scanned_at?: string | null;
      universe_mode?: string | null;
      completed_round?: boolean;
      report_file?: string | null;
    };
    setObservedData({
      candidates: data.candidates ?? [],
      observedCount: data.observed_count ?? 0,
      scannedAt: data.scanned_at,
      universeMode: data.universe_mode,
      completedRound: data.completed_round,
      reportFile: data.report_file,
    });
  }, []);

  const load = useCallback(async () => {
    try {
      const [poolResponse, pendingResponse] = await Promise.all([
        fetch("/api/pool"),
        fetch("/api/pool/import/pending"),
      ]);
      const poolDataResponse = (await poolResponse.json()) as { pool: PoolRow[] };
      const pendingData = (await pendingResponse.json()) as { pending: PendingItem[] };
      setPool(poolDataResponse.pool);
      setPending(pendingData.pending);
      await Promise.all([
        loadPool("macd_zero_axis"),
        loadPool("yearline_pullback"),
        loadPool("macd_divergence"),
        loadObserved(),
      ]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载股票池失败");
    }
  }, [loadObserved, loadPool]);

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

  const runScan = async (mode: "watchlist" | "all_a", scanKind: PoolType | "all") => {
    const key = `${scanKind}:${mode}`;
    setScanning(key);
    try {
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: "scan",
          scan_kind: scanKind,
          universe_mode: mode,
          notify: false,
        }),
      });
      const data = (await response.json().catch(() => ({}))) as { error?: string; jobId?: number };
      if (!response.ok) throw new Error(data.error || "启动筛选失败");
      const label = scanKind === "all" ? "三策略" : POOL_LABEL[scanKind];
      toast.success(
        mode === "all_a" ? `全市场${label}筛选已启动，可在结果页查看进度` : `自选池${label}筛选已启动，可在结果页查看进度`
      );
      setTimeout(() => { for (const type of Object.keys(POOL_LABEL) as PoolType[]) void loadPool(type); }, 8000);
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

  const filteredPool = pool.filter((item) => {
    if (!searchFilter.trim()) return true;
    const q = searchFilter.trim().toLowerCase();
    return (
      item.symbol.toLowerCase().includes(q) ||
      (item.name && item.name.toLowerCase().includes(q))
    );
  });

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">股票池与选股</h2>
          <p className="text-sm text-muted-foreground">管理自选标的与指标候选池；支持一键发起五标签多策略分析与截图/文本导入。</p>
        </div>
      </div>

      <Card>
        <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <CardTitle>我的自选股票池</CardTitle>
              <Badge variant="secondary" className="font-mono text-xs">
                {pool.length} 只
              </Badge>
            </div>
            <CardDescription className="mt-1">
              手动维护自选标的；分析任务可直接调用自选池，亦可在此快速发起分析。
            </CardDescription>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="default"
              size="sm"
              disabled={pool.length === 0 || analyzingAll}
              onClick={() => void triggerAnalyzeAll()}
              className="gap-1.5"
            >
              {analyzingAll ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Sparkles className="size-3.5" />
              )}
              一键分析全部自选
            </Button>
            <Button variant="outline" size="sm" onClick={openTextImport} className="gap-1.5">
              <FileText className="size-3.5" /> 文本导入
            </Button>
            <Button variant="outline" size="sm" onClick={openImageImport} className="gap-1.5">
              <FileImage className="size-3.5" /> 图片识别
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* 紧凑型快速添加与搜索过滤工具条 */}
          <div className="flex flex-col gap-3 rounded-lg border bg-muted/20 p-3 sm:flex-row sm:items-center sm:justify-between">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void addSymbol();
              }}
              className="flex flex-1 flex-wrap items-center gap-2"
            >
              <Input
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value)}
                placeholder="代码 (如 600036)"
                className="h-8.5 w-36 bg-background text-xs font-mono"
              />
              <Input
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="名称 (可选，如 招商银行)"
                className="h-8.5 w-40 bg-background text-xs"
              />
              <Button type="submit" size="sm" variant="secondary" className="h-8.5 text-xs gap-1">
                <Plus className="size-3.5" /> 加入自选
              </Button>
            </form>

            {pool.length > 3 && (
              <div className="flex items-center gap-1.5">
                <Search className="size-3.5 text-muted-foreground" />
                <Input
                  value={searchFilter}
                  onChange={(e) => setSearchFilter(e.target.value)}
                  placeholder="过滤代码 / 名称..."
                  className="h-8.5 w-36 bg-background text-xs"
                />
              </div>
            )}
          </div>

          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>代码</TableHead>
                <TableHead>名称</TableHead>
                <TableHead>来源</TableHead>
                <TableHead>添加时间</TableHead>
                <TableHead className="w-36 text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredPool.map((item) => (
                <TableRow key={item.symbol} className="hover:bg-muted/40">
                  <TableCell className="font-mono text-xs font-semibold">{item.symbol}</TableCell>
                  <TableCell className="font-medium text-sm">{item.name || "-"}</TableCell>
                  <TableCell>
                    <span
                      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${
                        item.source === "image"
                          ? "bg-purple-500/10 text-purple-600 dark:text-purple-400"
                          : item.source === "text"
                          ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                          : "bg-muted text-muted-foreground"
                      }`}
                    >
                      {item.source === "image"
                        ? "截图识别"
                        : item.source === "text"
                        ? "文本导入"
                        : item.source === "manual"
                        ? "手动添加"
                        : item.source}
                    </span>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{item.created_at}</TableCell>
                  <TableCell className="text-right">
                    <div className="flex items-center justify-end gap-1.5">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={analyzingSymbol === item.symbol}
                        onClick={() => void triggerAnalyze(item.symbol, item.name)}
                        className="h-7 px-2.5 text-xs gap-1 hover:border-primary hover:text-primary"
                        title="发起五标签多策略分析"
                      >
                        {analyzingSymbol === item.symbol ? (
                          <Loader2 className="size-3 animate-spin" />
                        ) : (
                          <Play className="size-3" />
                        )}
                        分析
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => void removeSymbol(item.symbol)}
                        className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
                        title="从股票池删除"
                      >
                        <Trash2 className="size-3.5" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
              {filteredPool.length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                    {pool.length === 0 ? "自选股票池为空，可使用上方工具栏添加或导入" : "未找到匹配股票"}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <Tabs value={poolType} onValueChange={(value) => setPoolType(value as PoolType)}>
          <CardHeader className="flex flex-row items-start justify-between gap-3">
            <div>
              <CardTitle>指标股票池</CardTitle>
              <CardDescription>
                扫描生成的候选股，两个池共用一张候选表、按 pool_type 独立保留 TTL 与容量；
                年线池为研究展示，不进入监控与下单链路。
              </CardDescription>
            </div>
            <TabsList>
              <TabsTrigger value="macd_zero_axis">日线零轴金叉</TabsTrigger>
              <TabsTrigger value="macd_divergence">零轴+底背离</TabsTrigger>
              <TabsTrigger value="yearline_pullback">年线趋势</TabsTrigger>
            </TabsList>
          </CardHeader>
          <CardContent>
            <div className="mb-4 flex flex-wrap items-center gap-3 border-b pb-3">
              <label className="text-sm" htmlFor="scan-strategy">选择策略</label>
              <select id="scan-strategy" className="h-9 rounded border bg-background px-3 text-sm" value={scanStrategy} onChange={event => setScanStrategy(event.target.value as PoolType | "all")}>
                <option value="all">全部三策略</option>{(Object.entries(POOL_LABEL) as [PoolType,string][]).map(([id,name]) => <option key={id} value={id}>{name}</option>)}
              </select>
              <Button disabled={scanning !== null} onClick={() => void runScan("all_a", scanStrategy)}><Filter className="size-4" />{scanning ? "启动中…" : "全市场筛选"}</Button>
              <span className="text-xs text-muted-foreground">筛选完成后更新对应指标池</span>
            </div>
            <ScanProgress onComplete={load} />
            <TabsContent value="macd_zero_axis">
              <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                <p className="max-w-md text-xs text-muted-foreground">
                  全市场日线零轴金叉筛选结果，保留{" "}
                  {poolData.macd_zero_axis?.ttl_business_days ?? 5} 个交易日，最多{" "}
                  {poolData.macd_zero_axis?.capacity ?? 100} 只。后续监控循环会对其做缠论买卖点分析。
                </p>
                <div className="flex shrink-0 gap-2">
                  <Button variant="outline" size="sm" onClick={() => void loadPool("macd_zero_axis")}>
                    <RefreshCw className="size-3.5" /> 刷新
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => setExpiredOpen(true)}>
                    失效/过期（{poolData.macd_zero_axis?.expiredCount ?? 0}）
                  </Button>
                </div>
              </div>
              <CandidateTable
                variant="macd"
                rows={poolData.macd_zero_axis?.candidates ?? []}
                emptyText="暂无候选，点击「筛选自选池」或「全市场筛选」生成"
                onAnalyze={triggerAnalyze}
                analyzingSymbol={analyzingSymbol}
              />
              <div className="mt-6 rounded-lg border border-amber-200/70 bg-amber-50/60 p-4 dark:border-amber-900/60 dark:bg-amber-950/20">
                <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <h3 className="text-sm font-semibold">观察候选（0 轴上方 / 0 轴附近）</h3>
                      <Badge variant="outline">仅展示</Badge>
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                      共 {observedData.observedCount} 只；不进入正式候选池、盘中监控或下单链路。
                      {observedData.scannedAt ? ` 最近扫描：${observedData.scannedAt}` : " 暂无扫描报告"}
                    </p>
                  </div>
                  <Button variant="outline" size="sm" onClick={() => void loadObserved()}>
                    <RefreshCw className="size-3.5" /> 刷新观察候选
                  </Button>
                </div>
                <CandidateTable
                  variant="macd-observed"
                  rows={observedData.candidates}
                  emptyText="暂无 0 轴上方或附近的观察候选"
                  onAnalyze={triggerAnalyze}
                  analyzingSymbol={analyzingSymbol}
                />
              </div>
            </TabsContent>

            <TabsContent value="macd_divergence">
              <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                <p className="max-w-md text-xs text-muted-foreground">
                  四个条件同时成立才入池：0轴上方或附近的 MACD 金叉、日线底背离（最近两段已完成
                  负柱区间价格创新低且面积收缩）、放量（当日量 ≥ 前 20 日均量 ×{" "}
                  {(poolData.macd_divergence?.config?.min_volume_ratio ?? 1.5).toString()}）、
                  收盘在年线上方且年线上行。信号收盘确认，入场参考次日开盘（仅记录）；
                  独立研究池，不进入监控、投票与下单链路，也不推送通知。保留{" "}
                  {poolData.macd_divergence?.ttl_business_days ?? 5} 个交易日，最多{" "}
                  {poolData.macd_divergence?.capacity ?? 100} 只。
                </p>
                <div className="flex shrink-0 gap-2">
                  <Button variant="outline" size="sm" onClick={() => void loadPool("macd_divergence")}>
                    <RefreshCw className="size-3.5" /> 刷新
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => setExpiredOpen(true)}>
                    失效/过期（{poolData.macd_divergence?.expiredCount ?? 0}）
                  </Button>
                </div>
              </div>
              <CandidateTable
                variant="macd-divergence"
                rows={poolData.macd_divergence?.candidates ?? []}
                emptyText="暂无候选，点击「筛选自选池」或「全市场筛选」生成"
                onAnalyze={triggerAnalyze}
                analyzingSymbol={analyzingSymbol}
              />
              <div className="mt-3 rounded-lg border bg-muted/40 p-3 text-xs text-muted-foreground">
                <span className="font-medium text-foreground">最近一次筛选漏斗</span>
                {(() => {
                  const scan = poolData.macd_divergence?.latestScan;
                  const funnel = scan?.condition_funnel;
                  if (!scan || !funnel?.conditions) {
                    return " ：暂无扫描报告，先点「筛选自选池」或「全市场筛选」。";
                  }
                  const c = funnel.conditions;
                  const pct = (key: string) =>
                    `${((c[key]?.rate ?? 0) * 100).toFixed(1)}%`;
                  const vol = scan.volume_ratio_distribution;
                  return (
                    <>
                      ：评估 {funnel.evaluated_symbols ?? 0} 只，命中{" "}
                      {c.all?.count ?? 0} 只 —— 金叉 {pct("golden_cross")}（
                      {c.golden_cross?.count ?? 0}）→ 0轴达标 {pct("zero_axis_ok")} →
                      放量 {pct("volume_ok")} → 年线上方 {pct("above_yearline")} →
                      底背离 {pct("divergence_ok")}。量比中位数{" "}
                      {vol?.median != null ? vol.median.toFixed(2) : "-"}x、
                      达阈值 {vol?.above_threshold ?? 0}/{vol?.count ?? 0} 只。
                      四条件为「且」关系，命中率天然很低。
                      {scan.scanned_at ? ` 扫描于 ${scan.scanned_at}。` : ""}
                    </>
                  );
                })()}
              </div>
            </TabsContent>

            <TabsContent value="yearline_pullback">
              <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                <p className="max-w-md text-xs text-muted-foreground">
                  年线回踩候选：MA250 上行且 MA60&gt;MA120&gt;MA250，前一日在年线上方，当日最低
                  回踩年线区间、收盘不破且收阳。信号收盘确认，入场参考为下一交易日开盘（仅记录）；
                  动态止损建议仅供研究展示，不进入下单链路，生产固定 8% 止损保持不变。保留{" "}
                  {poolData.yearline_pullback?.ttl_business_days ?? 5} 个交易日，最多{" "}
                  {poolData.yearline_pullback?.capacity ?? 100} 只。
                </p>
                <div className="flex shrink-0 gap-2">
                  <Button variant="outline" size="sm" onClick={() => void loadPool("yearline_pullback")}>
                    <RefreshCw className="size-3.5" /> 刷新
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => setExpiredOpen(true)}>
                    失效/过期（{poolData.yearline_pullback?.expiredCount ?? 0}）
                  </Button>
                </div>
              </div>
              <CandidateTable
                variant="yearline"
                rows={poolData.yearline_pullback?.candidates ?? []}
                emptyText="暂无候选，点击「筛选自选池」或「全市场筛选」生成"
                onAnalyze={triggerAnalyze}
                analyzingSymbol={analyzingSymbol}
              />
            </TabsContent>
          </CardContent>
        </Tabs>
      </Card>

      <Dialog open={expiredOpen} onOpenChange={setExpiredOpen}>
        <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>失效/过期指标股票池（{POOL_LABEL[poolType]}）</DialogTitle>
            <DialogDescription>
              不再符合条件或超过保留期的候选股（共 {poolData[poolType]?.expiredCount ?? 0} 只），
              不再参与监控扫描；每日全市场扫描完成时更新。
            </DialogDescription>
          </DialogHeader>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>代码</TableHead>
                <TableHead>名称</TableHead>
                <TableHead>原因</TableHead>
                <TableHead>移除日期</TableHead>
                <TableHead>评分</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(poolData[poolType]?.expired ?? []).map((item) => (
                <TableRow key={item.symbol}>
                  <TableCell className="font-mono text-xs">{item.symbol}</TableCell>
                  <TableCell>{item.name || "-"}</TableCell>
                  <TableCell>
                    <Badge variant={item.reason === "no_longer_qualified" ? "secondary" : "outline"}>
                      {item.reason === "no_longer_qualified" ? "不再符合条件" : item.reason === "expired" ? "已过期" : item.reason}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{item.expired_on}</TableCell>
                  <TableCell className="text-xs">{item.score}</TableCell>
                </TableRow>
              ))}
              {(poolData[poolType]?.expired ?? []).length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="text-center text-muted-foreground">
                    暂无失效/过期记录
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </DialogContent>
      </Dialog>

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
