"use client";

import { useCallback, useEffect, useState } from "react";
import { StrategyRuleSettings } from "@/components/strategy-rule-settings";
import { toast } from "sonner";
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
import { Switch } from "@/components/ui/switch";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Activity,
  AlertTriangle,
  Clock,
  Compass,
  Filter,
  Save,
  ShieldCheck,
  Sparkles,
} from "lucide-react";

interface StrategiesForm {
  min_bi_bars: string;
  divergence_ratio: string;
  fresh_signal_bars: string;
  macd_fast: string;
  macd_slow: string;
  macd_signal: string;
  zero_axis_tolerance: string;
  moderate_volume_min: string;
  moderate_volume_max: string;
  min_confirmations: string;
  llm_context_bars: string;
  buy_threshold: string;
  sell_threshold: string;
  execution_default: string;
  buy_1_mode: string;
  buy_2_mode: string;
  buy_3_mode: string;
  macd_above_mode: string;
  macd_near_mode: string;
  timeframes: string;
  watchlist: string;
  bar_limit: string;
  max_symbols_per_cycle: string;
  universe_mode: string;
  stock_pool_enabled: boolean;
  min_market_cap: string;
  max_market_cap: string;
  amount_window: string;
  min_avg_amount: string;
  turnover_window: string;
  min_avg_turnover_rate: string;
  max_avg_turnover_rate: string;
  min_listing_trade_days: string;
  exclude_st: boolean;
  exclude_delisting: boolean;
  missing_data_policy: string;
}

function num(value: unknown, fallback = ""): string {
  return typeof value === "number" ? String(value) : fallback;
}

function list(value: unknown): string {
  return Array.isArray(value) ? (value as unknown[]).map(String).join(", ") : "";
}

function NumberField({
  id,
  label,
  value,
  onChange,
  hint,
  unit,
  min,
  max,
  step = "any",
  disabled = false,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (event: React.ChangeEvent<HTMLInputElement>) => void;
  hint?: string;
  unit?: string;
  min?: number;
  max?: number;
  step?: number | "any";
  disabled?: boolean;
}) {
  return (
    <div className="flex flex-col gap-1.5 rounded-lg border bg-background/50 p-3 transition-colors hover:border-primary/30">
      <div className="flex items-center justify-between">
        <Label htmlFor={id} className="text-xs font-medium">
          {label}
        </Label>
        {unit ? (
          <span className="text-[11px] font-mono text-muted-foreground">{unit}</span>
        ) : null}
      </div>
      <Input
        id={id}
        type="number"
        step={step}
        min={min}
        max={max}
        value={value}
        onChange={onChange}
        disabled={disabled}
        className="h-8 font-mono text-sm"
      />
      {hint ? <p className="text-[11px] text-muted-foreground leading-tight">{hint}</p> : null}
    </div>
  );
}

function ExecutionModeField({
  id,
  label,
  value,
  onChange,
  hint,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (event: React.ChangeEvent<HTMLSelectElement>) => void;
  hint?: string;
}) {
  const getBadge = () => {
    switch (value) {
      case "enabled":
        return <Badge className="bg-emerald-600/10 text-emerald-600 dark:bg-emerald-500/20 dark:text-emerald-400">实时与回测启用</Badge>;
      case "observe_only":
        return <Badge variant="secondary" className="bg-amber-500/10 text-amber-600 dark:bg-amber-500/20 dark:text-amber-400">仅观察记录</Badge>;
      default:
        return <Badge variant="outline" className="text-muted-foreground">完全禁用</Badge>;
    }
  };

  return (
    <div className="flex flex-col gap-2 rounded-xl border bg-card p-4 transition-all hover:border-primary/40">
      <div className="flex items-center justify-between">
        <Label htmlFor={id} className="font-semibold text-sm">
          {label}
        </Label>
        {getBadge()}
      </div>
      <select
        id={id}
        className="h-9 w-full rounded-md border bg-background px-3 text-sm transition-colors focus:ring-1 focus:ring-primary"
        value={value}
        onChange={onChange}
      >
        <option value="enabled">启用 (交易 + 回测入场依据)</option>
        <option value="observe_only">仅观察 (记录信号，不触发入场)</option>
        <option value="disabled">禁用 (忽略此买点)</option>
      </select>
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

export default function StrategiesPage() {
  const [form, setForm] = useState<StrategiesForm>({
    min_bi_bars: "",
    divergence_ratio: "",
    fresh_signal_bars: "",
    macd_fast: "",
    macd_slow: "",
    macd_signal: "",
    zero_axis_tolerance: "",
    moderate_volume_min: "",
    moderate_volume_max: "",
    min_confirmations: "",
    llm_context_bars: "",
    buy_threshold: "",
    sell_threshold: "",
    execution_default: "enabled",
    buy_1_mode: "enabled",
    buy_2_mode: "observe_only",
    buy_3_mode: "enabled",
    macd_above_mode: "enabled",
    macd_near_mode: "enabled",
    timeframes: "",
    watchlist: "",
    bar_limit: "",
    max_symbols_per_cycle: "",
    universe_mode: "watchlist",
    stock_pool_enabled: true,
    min_market_cap: "",
    max_market_cap: "",
    amount_window: "",
    min_avg_amount: "",
    turnover_window: "",
    min_avg_turnover_rate: "",
    max_avg_turnover_rate: "",
    min_listing_trade_days: "",
    exclude_st: true,
    exclude_delisting: true,
    missing_data_policy: "reject",
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/config/strategies");
      if (!response.ok) throw new Error("加载配置失败");
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data = (await response.json()) as { config: Record<string, any> };
      const signal = data.config.signal_strategy ?? {};
      const executionPolicy = signal.execution_policy ?? {};
      const signalModes = executionPolicy.signals ?? {};
      const monitor = data.config.monitor ?? {};
      const scan = data.config.scan ?? {};
      const stockPool = data.config.stock_pool ?? {};
      setForm({
        min_bi_bars: num(signal.chan?.min_bi_bars, "4"),
        divergence_ratio: num(signal.chan?.divergence_ratio, "0.9"),
        fresh_signal_bars: num(signal.chan?.fresh_signal_bars, "1"),
        macd_fast: num(signal.macd?.fast, "12"),
        macd_slow: num(signal.macd?.slow, "26"),
        macd_signal: num(signal.macd?.signal, "9"),
        zero_axis_tolerance: num(signal.macd?.zero_axis_tolerance, "0.005"),
        moderate_volume_min: num(signal.macd?.moderate_volume_min, "1"),
        moderate_volume_max: num(signal.macd?.moderate_volume_max, "2"),
        min_confirmations: num(
          signal.macd?.min_confirmations ?? signal.chan_zero_axis?.min_confirmations,
          "0"
        ),
        llm_context_bars: num(signal.llm_context_bars, "48"),
        buy_threshold: num(signal.scoring?.buy_threshold, "60"),
        sell_threshold: num(signal.scoring?.sell_threshold, "60"),
        execution_default: String(executionPolicy.default ?? "enabled"),
        buy_1_mode: String(signalModes.buy_1 ?? "enabled"),
        buy_2_mode: String(signalModes.buy_2 ?? "observe_only"),
        buy_3_mode: String(signalModes.buy_3 ?? "enabled"),
        macd_above_mode: String(
          signalModes.macd_golden_cross_pullback_confirmed_above ?? "enabled"
        ),
        macd_near_mode: String(
          signalModes.macd_golden_cross_pullback_confirmed_near ?? "enabled"
        ),
        timeframes: list(monitor.timeframes),
        watchlist: list(monitor.watchlist),
        bar_limit: num(monitor.bar_limit, "300"),
        max_symbols_per_cycle: num(monitor.max_symbols_per_cycle, "20"),
        universe_mode: String(scan.universe_mode ?? "watchlist"),
        stock_pool_enabled: Boolean(stockPool.enabled ?? true),
        min_market_cap: num(stockPool.min_market_cap, "50"),
        max_market_cap: num(stockPool.max_market_cap, "3000"),
        amount_window: num(stockPool.amount_window, "20"),
        min_avg_amount: num(stockPool.min_avg_amount, "1"),
        turnover_window: num(stockPool.turnover_window, "20"),
        min_avg_turnover_rate: num(stockPool.min_avg_turnover_rate, "0.5"),
        max_avg_turnover_rate: num(stockPool.max_avg_turnover_rate, "8"),
        min_listing_trade_days: num(stockPool.min_listing_trade_days, "120"),
        exclude_st: Boolean(stockPool.exclude_st ?? true),
        exclude_delisting: Boolean(stockPool.exclude_delisting ?? true),
        missing_data_policy: String(stockPool.missing_data_policy ?? "reject"),
      });
      setFormError("");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载配置失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const set =
    (key: keyof StrategiesForm) =>
    (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
      setForm((prev) => ({ ...prev, [key]: event.target.value }));

  const applyPreset = (presetName: string) => {
    if (presetName === "standard") {
      setForm((prev) => ({
        ...prev,
        min_bi_bars: "4",
        divergence_ratio: "0.9",
        fresh_signal_bars: "1",
        zero_axis_tolerance: "0.005",
        min_confirmations: "0",
        buy_threshold: "60",
        sell_threshold: "60",
      }));
      toast.success("已载入【稳健标准型】策略参数预设");
    } else if (presetName === "sensitive") {
      setForm((prev) => ({
        ...prev,
        min_bi_bars: "4",
        divergence_ratio: "0.8",
        fresh_signal_bars: "1",
        zero_axis_tolerance: "0.008",
        min_confirmations: "0",
        buy_threshold: "50",
        sell_threshold: "55",
      }));
      toast.success("已载入【短线高敏型】策略参数预设");
    } else if (presetName === "strict") {
      setForm((prev) => ({
        ...prev,
        min_bi_bars: "5",
        divergence_ratio: "0.95",
        fresh_signal_bars: "1",
        zero_axis_tolerance: "0.003",
        min_confirmations: "1",
        buy_threshold: "75",
        sell_threshold: "65",
      }));
      toast.success("已载入【严谨高胜率型】策略参数预设");
    }
  };

  const save = async () => {
    const requiredStockPoolValues = [
      form.min_market_cap,
      form.max_market_cap,
      form.amount_window,
      form.min_avg_amount,
      form.turnover_window,
      form.min_avg_turnover_rate,
      form.max_avg_turnover_rate,
      form.min_listing_trade_days,
    ];
    if (form.stock_pool_enabled && requiredStockPoolValues.some((value) => value.trim() === "")) {
      setFormError("请完整填写股票池过滤参数。");
      return;
    }
    if (Number(form.min_market_cap) > Number(form.max_market_cap)) {
      setFormError("流通市值下限不能大于上限。");
      return;
    }
    if (Number(form.min_avg_turnover_rate) > Number(form.max_avg_turnover_rate)) {
      setFormError("平均换手率下限不能大于上限。");
      return;
    }
    setFormError("");
    setSaving(true);
    const values = {
      "signal_strategy.chan.min_bi_bars": Number(form.min_bi_bars),
      "signal_strategy.chan.divergence_ratio": Number(form.divergence_ratio),
      "signal_strategy.chan.fresh_signal_bars": Number(form.fresh_signal_bars),
      "signal_strategy.macd.fast": Number(form.macd_fast),
      "signal_strategy.macd.slow": Number(form.macd_slow),
      "signal_strategy.macd.signal": Number(form.macd_signal),
      "signal_strategy.macd.zero_axis_tolerance": Number(form.zero_axis_tolerance),
      "signal_strategy.macd.moderate_volume_min": Number(form.moderate_volume_min),
      "signal_strategy.macd.moderate_volume_max": Number(form.moderate_volume_max),
      "signal_strategy.macd.min_confirmations": Number(form.min_confirmations),
      "signal_strategy.llm_context_bars": Number(form.llm_context_bars),
      "signal_strategy.scoring.buy_threshold": Number(form.buy_threshold),
      "signal_strategy.scoring.sell_threshold": Number(form.sell_threshold),
      "signal_strategy.execution_policy.default": form.execution_default,
      "signal_strategy.execution_policy.signals.buy_1": form.buy_1_mode,
      "signal_strategy.execution_policy.signals.buy_2": form.buy_2_mode,
      "signal_strategy.execution_policy.signals.buy_3": form.buy_3_mode,
      "signal_strategy.execution_policy.signals.macd_golden_cross_pullback_confirmed_above":
        form.macd_above_mode,
      "signal_strategy.execution_policy.signals.macd_golden_cross_pullback_confirmed_near":
        form.macd_near_mode,
      "monitor.timeframes": form.timeframes.split(/[\s,，;；]+/).filter(Boolean),
      "monitor.watchlist": form.watchlist.split(/[\s,，;；]+/).filter(Boolean),
      "monitor.bar_limit": Number(form.bar_limit),
      "stock_pool.enabled": form.stock_pool_enabled,
      "stock_pool.min_market_cap": Number(form.min_market_cap),
      "stock_pool.max_market_cap": Number(form.max_market_cap),
      "stock_pool.amount_window": Number(form.amount_window),
      "stock_pool.min_avg_amount": Number(form.min_avg_amount),
      "stock_pool.turnover_window": Number(form.turnover_window),
      "stock_pool.min_avg_turnover_rate": Number(form.min_avg_turnover_rate),
      "stock_pool.max_avg_turnover_rate": Number(form.max_avg_turnover_rate),
      "stock_pool.min_listing_trade_days": Number(form.min_listing_trade_days),
      "stock_pool.exclude_st": form.exclude_st,
      "stock_pool.exclude_delisting": form.exclude_delisting,
      "stock_pool.missing_data_policy": form.missing_data_policy,
    };
    try {
      const response = await fetch("/api/config/strategies", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      const data = (await response.json().catch(() => ({}))) as {
        error?: string;
        errors?: string[];
      };
      if (!response.ok) {
        throw new Error((data.errors ?? []).join("；") || data.error || "保存失败");
      }
      toast.success("策略配置已保存");
      void load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex max-w-4xl flex-col gap-6">
        <Skeleton className="h-48 rounded-xl" />
        <Skeleton className="h-96 rounded-xl" />
      </div>
    );
  }

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <StrategyRuleSettings />

      <Card>
        <CardHeader className="pb-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <CardTitle className="text-xl">量化策略综合参数</CardTitle>
              <CardDescription className="mt-1">
                保存后下一次分析与扫描立即生效。优先级：环境变量 &gt; Web 设置 &gt; config.yaml。
              </CardDescription>
            </div>
            <Button onClick={() => void save()} disabled={saving} className="shrink-0">
              <Save className="mr-1.5 size-4" />
              {saving ? "保存中..." : "保存策略参数"}
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          {formError ? (
            <Alert variant="destructive">
              <AlertTriangle className="size-4" />
              <AlertTitle>配置校验未通过</AlertTitle>
              <AlertDescription>{formError}</AlertDescription>
            </Alert>
          ) : null}

          <Tabs defaultValue="indicators" className="w-full">
            <TabsList className="grid w-full grid-cols-4">
              <TabsTrigger value="indicators" className="flex items-center gap-1.5 text-xs sm:text-sm">
                <Activity className="size-3.5" />
                技术与指标
              </TabsTrigger>
              <TabsTrigger value="execution" className="flex items-center gap-1.5 text-xs sm:text-sm">
                <Compass className="size-3.5" />
                入场执行模式
              </TabsTrigger>
              <TabsTrigger value="pool" className="flex items-center gap-1.5 text-xs sm:text-sm">
                <ShieldCheck className="size-3.5" />
                股票池风控
              </TabsTrigger>
              <TabsTrigger value="monitor" className="flex items-center gap-1.5 text-xs sm:text-sm">
                <Clock className="size-3.5" />
                周期与自选
              </TabsTrigger>
            </TabsList>

            {/* TAB 1: 技术与指标参数 */}
            <TabsContent value="indicators" className="space-y-6 pt-4">
              {/* 推荐预设工具条 */}
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border bg-muted/30 p-3">
                <div className="flex items-center gap-2">
                  <Sparkles className="size-4 text-primary" />
                  <span className="text-xs font-medium">推荐参数预设：</span>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-7 text-xs"
                    onClick={() => applyPreset("standard")}
                  >
                    稳健标准型（默认）
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-7 text-xs"
                    onClick={() => applyPreset("sensitive")}
                  >
                    短线高敏型
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-7 text-xs"
                    onClick={() => applyPreset("strict")}
                  >
                    严谨高胜率型
                  </Button>
                </div>
              </div>

              {/* 缠论特征参数 */}
              <div className="space-y-3">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  缠论笔与背驰参数
                </h3>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  <NumberField
                    id="min-bi-bars"
                    label="最小笔 K 线数"
                    value={form.min_bi_bars}
                    onChange={set("min_bi_bars")}
                    hint="顶底分型之间至少间隔的K线数"
                    unit="根"
                    min={3}
                  />
                  <NumberField
                    id="divergence-ratio"
                    label="背驰判定比例"
                    value={form.divergence_ratio}
                    onChange={set("divergence_ratio")}
                    hint="后段动力指标比前段力度的阈值（如 0.9 为 90%）"
                    step={0.05}
                  />
                  <NumberField
                    id="fresh-signal-bars"
                    label="新信号有效K数"
                    value={form.fresh_signal_bars}
                    onChange={set("fresh_signal_bars")}
                    hint="判定买点形成的最新K线窗口"
                    unit="根"
                    min={1}
                  />
                </div>
              </div>

              {/* MACD 参数 */}
              <div className="space-y-3">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  MACD 指标与容差
                </h3>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <NumberField
                    id="macd-fast"
                    label="MACD 快线"
                    value={form.macd_fast}
                    onChange={set("macd_fast")}
                    unit="周期"
                    min={1}
                  />
                  <NumberField
                    id="macd-slow"
                    label="MACD 慢线"
                    value={form.macd_slow}
                    onChange={set("macd_slow")}
                    unit="周期"
                    min={1}
                  />
                  <NumberField
                    id="macd-signal"
                    label="MACD 信号平滑"
                    value={form.macd_signal}
                    onChange={set("macd_signal")}
                    unit="周期"
                    min={1}
                  />
                  <NumberField
                    id="zero-axis-tolerance"
                    label="0轴邻近容差"
                    value={form.zero_axis_tolerance}
                    onChange={set("zero_axis_tolerance")}
                    hint="判定回踩0轴的绝对误差范围"
                    step={0.001}
                  />
                </div>
              </div>

              {/* 量价风控与评分 */}
              <div className="space-y-3">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  量价配合与多空评分阈值
                </h3>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <NumberField
                    id="moderate-volume-min"
                    label="温和放量下倍数"
                    value={form.moderate_volume_min}
                    onChange={set("moderate_volume_min")}
                    hint="对比前日成交量最低倍率"
                    unit="倍"
                    step={0.1}
                  />
                  <NumberField
                    id="moderate-volume-max"
                    label="温和放量上倍数"
                    value={form.moderate_volume_max}
                    onChange={set("moderate_volume_max")}
                    hint="超过此倍率防范巨量诱多"
                    unit="倍"
                    step={0.1}
                  />
                  <NumberField
                    id="buy-threshold"
                    label="多头买入评分线"
                    value={form.buy_threshold}
                    onChange={set("buy_threshold")}
                    hint="综合得分高于此值视为买入共振"
                    unit="分"
                    min={0}
                    max={100}
                  />
                  <NumberField
                    id="sell-threshold"
                    label="空头卖出评分线"
                    value={form.sell_threshold}
                    onChange={set("sell_threshold")}
                    hint="综合减仓/卖出参考门槛"
                    unit="分"
                    min={0}
                    max={100}
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <NumberField
                  id="min-confirmations"
                  label="确认条件数下限"
                  value={form.min_confirmations}
                  onChange={set("min_confirmations")}
                  hint="0轴金叉需满足的额外条件数（如量能放大、分型确认等）"
                  min={0}
                  step={1}
                />
                <NumberField
                  id="llm-context-bars"
                  label="AI 自动解读 K 线窗口"
                  value={form.llm_context_bars}
                  onChange={set("llm_context_bars")}
                  hint="提示词中送给多大模型的行情切片长度"
                  unit="根"
                  min={10}
                />
              </div>
            </TabsContent>

            {/* TAB 2: 入场执行模式 */}
            <TabsContent value="execution" className="space-y-4 pt-4">
              <div className="rounded-lg border bg-muted/20 p-3 text-xs text-muted-foreground">
                <span className="font-medium text-foreground">💡 执行模式说明：</span>
                <span className="ml-1">
                  选择「仅观察」将持续在监控和分析页中高亮标识信号，但不会在实盘及策略回测中触发独立开仓；卖出与止损策略不受影响。
                </span>
              </div>

              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <ExecutionModeField
                  id="execution-default"
                  label="未明确配置信号的默认策略"
                  value={form.execution_default}
                  onChange={set("execution_default")}
                  hint="其他拓展买卖形态未指定时的默认入场处理"
                />
                <ExecutionModeField
                  id="buy-1-mode"
                  label="缠论一买 (趋势背驰转折点)"
                  value={form.buy_1_mode}
                  onChange={set("buy_1_mode")}
                  hint="左侧抄底第一类买点，波动较大"
                />
                <ExecutionModeField
                  id="buy-2-mode"
                  label="缠论二买 (回踩不破前低)"
                  value={form.buy_2_mode}
                  onChange={set("buy_2_mode")}
                  hint="系统默认配置为仅观察，降低反复震荡洗盘损耗"
                />
                <ExecutionModeField
                  id="buy-3-mode"
                  label="缠论三买 (中枢突破后首次回抽)"
                  value={form.buy_3_mode}
                  onChange={set("buy_3_mode")}
                  hint="强趋势主升浪标志，主攻确定性爆发机会"
                />
                <ExecutionModeField
                  id="macd-above-mode"
                  label="MACD 0 轴上金叉回落确认"
                  value={form.macd_above_mode}
                  onChange={set("macd_above_mode")}
                  hint="处于强势多头格局中的空中加油"
                />
                <ExecutionModeField
                  id="macd-near-mode"
                  label="MACD 0 轴附近金叉回落确认"
                  value={form.macd_near_mode}
                  onChange={set("macd_near_mode")}
                  hint="多空平衡打破、转强确认买点"
                />
              </div>
            </TabsContent>

            {/* TAB 3: 股票池风控 */}
            <TabsContent value="pool" className="space-y-4 pt-4">
              <div className="flex items-center justify-between rounded-xl border bg-card p-4">
                <div className="space-y-0.5">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-sm">股票池前置硬性过滤</span>
                    <Badge variant={form.stock_pool_enabled ? "default" : "secondary"}>
                      {form.stock_pool_enabled ? "已开启全局风控" : "过滤已停用"}
                    </Badge>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    盘中实时全市场扫描与策略回测共用；回测严格以信号发生当日指标动态过滤，杜绝幸存者偏差。
                  </p>
                </div>
                <Switch
                  id="stock-pool-enabled"
                  checked={form.stock_pool_enabled}
                  onCheckedChange={(val) =>
                    setForm((prev) => ({ ...prev, stock_pool_enabled: val }))
                  }
                />
              </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                <NumberField
                  id="min-market-cap"
                  label="流通市值下限"
                  value={form.min_market_cap}
                  onChange={set("min_market_cap")}
                  unit="亿元"
                  min={0}
                  disabled={!form.stock_pool_enabled}
                  hint="过滤微盘壳股及流动性缺失标的"
                />
                <NumberField
                  id="max-market-cap"
                  label="流通市值上限"
                  value={form.max_market_cap}
                  onChange={set("max_market_cap")}
                  unit="亿元"
                  min={0}
                  disabled={!form.stock_pool_enabled}
                  hint="排除超大盘金融/基建权重（视风格选填）"
                />
                <NumberField
                  id="min-listing-days"
                  label="最少上市交易日"
                  value={form.min_listing_trade_days}
                  onChange={set("min_listing_trade_days")}
                  unit="天"
                  min={0}
                  step={1}
                  disabled={!form.stock_pool_enabled}
                  hint="技术指标至少需积累足够历史日线"
                />
                <NumberField
                  id="amount-window"
                  label="平均成交额统计窗口"
                  value={form.amount_window}
                  onChange={set("amount_window")}
                  unit="交易日"
                  min={1}
                  step={1}
                  disabled={!form.stock_pool_enabled}
                />
                <NumberField
                  id="min-avg-amount"
                  label="最低平均成交额"
                  value={form.min_avg_amount}
                  onChange={set("min_avg_amount")}
                  unit="亿元"
                  min={0}
                  disabled={!form.stock_pool_enabled}
                  hint="过滤冷门僵尸股与换手枯竭股"
                />
                <NumberField
                  id="turnover-window"
                  label="平均换手率统计窗口"
                  value={form.turnover_window}
                  onChange={set("turnover_window")}
                  unit="交易日"
                  min={1}
                  step={1}
                  disabled={!form.stock_pool_enabled}
                />
                <NumberField
                  id="min-avg-turnover"
                  label="平均换手率下限"
                  value={form.min_avg_turnover_rate}
                  onChange={set("min_avg_turnover_rate")}
                  unit="%"
                  min={0}
                  max={100}
                  disabled={!form.stock_pool_enabled}
                />
                <NumberField
                  id="max-avg-turnover"
                  label="平均换手率上限"
                  value={form.max_avg_turnover_rate}
                  onChange={set("max_avg_turnover_rate")}
                  unit="%"
                  min={0}
                  max={100}
                  disabled={!form.stock_pool_enabled}
                  hint="防范连续极端非理性换手"
                />
                <div className="flex flex-col gap-1.5 rounded-lg border bg-background/50 p-3">
                  <Label htmlFor="missing-data-policy" className="text-xs font-medium">
                    指标缺失处理策略
                  </Label>
                  <select
                    id="missing-data-policy"
                    className="h-8 rounded-md border bg-background px-2 text-xs disabled:cursor-not-allowed disabled:opacity-50"
                    value={form.missing_data_policy}
                    onChange={set("missing_data_policy")}
                    disabled={!form.stock_pool_enabled}
                  >
                    <option value="reject">拒绝入选候选池（严谨推荐）</option>
                    <option value="allow">记录警告并破格放行</option>
                  </select>
                  <p className="text-[11px] text-muted-foreground">行情源缺失时的兜底风控策略</p>
                </div>
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="flex items-center justify-between rounded-lg border bg-muted/40 px-3.5 py-3">
                  <div>
                    <Label htmlFor="exclude-st" className="text-sm font-medium">
                      严禁 ST / *ST 标的
                    </Label>
                    <p className="text-xs text-muted-foreground">根据当前及历史证券名称自动过滤警示股</p>
                  </div>
                  <Switch
                    id="exclude-st"
                    checked={form.exclude_st}
                    disabled={!form.stock_pool_enabled}
                    onCheckedChange={(val) => setForm((prev) => ({ ...prev, exclude_st: val }))}
                  />
                </div>
                <div className="flex items-center justify-between rounded-lg border bg-muted/40 px-3.5 py-3">
                  <div>
                    <Label htmlFor="exclude-delisting" className="text-sm font-medium">
                      排除退市风险股
                    </Label>
                    <p className="text-xs text-muted-foreground">名称含有“退”等进入退市整理期的品种</p>
                  </div>
                  <Switch
                    id="exclude-delisting"
                    checked={form.exclude_delisting}
                    disabled={!form.stock_pool_enabled}
                    onCheckedChange={(val) =>
                      setForm((prev) => ({ ...prev, exclude_delisting: val }))
                    }
                  />
                </div>
              </div>
            </TabsContent>

            {/* TAB 4: 周期与自选 */}
            <TabsContent value="monitor" className="space-y-4 pt-4">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label className="text-sm font-medium">多周期监控级别</Label>
                  <Input
                    className="font-mono text-sm"
                    value={form.timeframes}
                    onChange={set("timeframes")}
                    placeholder="1m, 5m, 15m, 30m, 60m, 120m, 1d"
                  />
                  <p className="text-xs text-muted-foreground">
                    用逗号分隔，如 30m, 60m, 1d。监控扫描器将依次计算各级别缠论分型与MACD状态。
                  </p>
                </div>

                <div className="space-y-1.5">
                  <Label className="text-sm font-medium">K 线请求容量上限（bar_limit）</Label>
                  <Input
                    type="number"
                    className="font-mono text-sm"
                    value={form.bar_limit}
                    onChange={set("bar_limit")}
                    placeholder="300"
                  />
                  <p className="text-xs text-muted-foreground">
                    每个周期向行情源请求的最大历史 K 线根数，通常 300 根已满足笔和中枢划分。
                  </p>
                </div>
              </div>

              <div className="space-y-1.5 rounded-xl border bg-muted/20 p-4">
                <Label className="text-sm font-medium">全局自选股 Watchlist（只读状态）</Label>
                <Input
                  value={form.watchlist}
                  disabled
                  placeholder="000001.SZ, 600036.SH"
                  className="bg-muted font-mono text-xs text-muted-foreground"
                />
                <p className="text-xs text-muted-foreground">
                  自选股票池由「股票与持仓」页面统一下发和动态管理，新增或移除标的后自动在此处同步生效。
                </p>
              </div>
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>
    </div>
  );
}
