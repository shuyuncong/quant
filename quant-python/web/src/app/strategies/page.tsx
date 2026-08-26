"use client";

import { useCallback, useEffect, useState } from "react";
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
import { Save } from "lucide-react";

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
  min?: number;
  max?: number;
  step?: number | "any";
  disabled?: boolean;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type="number"
        step={step}
        min={min}
        max={max}
        value={value}
        onChange={onChange}
        disabled={disabled}
      />
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
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
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <select
        id={id}
        className="h-9 rounded-lg border bg-transparent px-3 text-sm"
        value={value}
        onChange={onChange}
      >
        <option value="enabled">启用</option>
        <option value="observe_only">仅观察</option>
        <option value="disabled">禁用</option>
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
        min_confirmations: num(signal.macd?.min_confirmations ?? signal.chan_zero_axis?.min_confirmations, "0"),
        llm_context_bars: num(signal.llm_context_bars, "48"),
        buy_threshold: num(signal.scoring?.buy_threshold, "60"),
        sell_threshold: num(signal.scoring?.sell_threshold, "60"),
        execution_default: String(executionPolicy.default ?? "enabled"),
        buy_1_mode: String(signalModes.buy_1 ?? "enabled"),
        buy_2_mode: String(signalModes.buy_2 ?? "observe_only"),
        buy_3_mode: String(signalModes.buy_3 ?? "enabled"),
        macd_above_mode: String(signalModes.macd_golden_cross_pullback_confirmed_above ?? "enabled"),
        macd_near_mode: String(signalModes.macd_golden_cross_pullback_confirmed_near ?? "enabled"),
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
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const set = (key: keyof StrategiesForm) =>
    (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((prev) => ({ ...prev, [key]: event.target.value }));

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
      "signal_strategy.execution_policy.signals.macd_golden_cross_pullback_confirmed_above": form.macd_above_mode,
      "signal_strategy.execution_policy.signals.macd_golden_cross_pullback_confirmed_near": form.macd_near_mode,
      "monitor.timeframes": form.timeframes.split(/[\s,，;；]+/).filter(Boolean),
      "monitor.watchlist": form.watchlist.split(/[\s,，;；]+/).filter(Boolean),
      "monitor.bar_limit": Number(form.bar_limit),
      "monitor.max_symbols_per_cycle": Number(form.max_symbols_per_cycle),
      "scan.universe_mode": form.universe_mode,
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
      const data = (await response.json().catch(() => ({}))) as { error?: string; errors?: string[] };
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

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>策略配置</CardTitle>
          <CardDescription>
            修改后保存到 Web 设置库；下一次分析/扫描立即生效。优先级：环境变量 &gt; Web 设置 &gt; config.yaml。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-6">
          {formError ? (
            <Alert variant="destructive">
              <AlertTitle>配置有误</AlertTitle>
              <AlertDescription>{formError}</AlertDescription>
            </Alert>
          ) : null}

          <section className="flex flex-col gap-4">
            <div>
              <h2 className="font-medium">技术信号参数</h2>
              <p className="text-sm text-muted-foreground">缠论、MACD、评分和扫描批次设置。</p>
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <NumberField id="min-bi-bars" label="缠论：最小笔 K 数" value={form.min_bi_bars} onChange={set("min_bi_bars")} />
              <NumberField id="divergence-ratio" label="缠论：背驰比" value={form.divergence_ratio} onChange={set("divergence_ratio")} />
              <NumberField id="fresh-signal-bars" label="缠论：新信号K数" value={form.fresh_signal_bars} onChange={set("fresh_signal_bars")} />
              <NumberField id="macd-fast" label="MACD：快线" value={form.macd_fast} onChange={set("macd_fast")} />
              <NumberField id="macd-slow" label="MACD：慢线" value={form.macd_slow} onChange={set("macd_slow")} />
              <NumberField id="macd-signal" label="MACD：信号线" value={form.macd_signal} onChange={set("macd_signal")} />
              <NumberField id="zero-axis-tolerance" label="MACD：0轴容差" value={form.zero_axis_tolerance} onChange={set("zero_axis_tolerance")} />
              <NumberField id="moderate-volume-min" label="温和放量下限" value={form.moderate_volume_min} onChange={set("moderate_volume_min")} />
              <NumberField id="moderate-volume-max" label="温和放量上限" value={form.moderate_volume_max} onChange={set("moderate_volume_max")} />
              <NumberField id="min-confirmations" label="确认条件数下限" value={form.min_confirmations} onChange={set("min_confirmations")} />
              <NumberField id="buy-threshold" label="买入评分阈值" value={form.buy_threshold} onChange={set("buy_threshold")} />
              <NumberField id="sell-threshold" label="卖出评分阈值" value={form.sell_threshold} onChange={set("sell_threshold")} />
              <NumberField id="bar-limit" label="K线数量（bar_limit）" value={form.bar_limit} onChange={set("bar_limit")} />
              <NumberField id="llm-context-bars" label="AI每周期K线数" value={form.llm_context_bars} onChange={set("llm_context_bars")} />
              <NumberField id="max-symbols-per-cycle" label="盘中每批股票数" value={form.max_symbols_per_cycle} onChange={set("max_symbols_per_cycle")} />
            </div>
          </section>

          <section className="flex flex-col gap-4 rounded-xl border p-4">
            <div>
              <h2 className="font-medium">入场信号执行策略</h2>
              <p className="text-sm text-muted-foreground">
                “仅观察”会继续记录和展示信号，但不会把它作为独立入场依据；卖出与风控退出不受影响。
              </p>
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <ExecutionModeField
                id="execution-default"
                label="未配置信号默认策略"
                value={form.execution_default}
                onChange={set("execution_default")}
              />
              <ExecutionModeField
                id="buy-1-mode"
                label="缠论一买"
                value={form.buy_1_mode}
                onChange={set("buy_1_mode")}
              />
              <ExecutionModeField
                id="buy-2-mode"
                label="缠论二买"
                value={form.buy_2_mode}
                onChange={set("buy_2_mode")}
                hint="默认仅观察，不进入回测或实时交易候选。"
              />
              <ExecutionModeField
                id="buy-3-mode"
                label="缠论三买"
                value={form.buy_3_mode}
                onChange={set("buy_3_mode")}
              />
              <ExecutionModeField
                id="macd-above-mode"
                label="0轴上金叉回落确认"
                value={form.macd_above_mode}
                onChange={set("macd_above_mode")}
              />
              <ExecutionModeField
                id="macd-near-mode"
                label="0轴附近金叉回落确认"
                value={form.macd_near_mode}
                onChange={set("macd_near_mode")}
              />
            </div>
          </section>

          <section className="flex flex-col gap-4 rounded-xl border p-4">
            <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
              <div>
                <h2 className="font-medium">股票池前置过滤</h2>
                <p className="text-sm text-muted-foreground">
                  实时扫描与回测共用；回测按信号日计算，不使用今天的市值筛历史。
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Switch
                  id="stock-pool-enabled"
                  checked={form.stock_pool_enabled}
                  onCheckedChange={(value) => setForm((prev) => ({ ...prev, stock_pool_enabled: value }))}
                />
                <Label htmlFor="stock-pool-enabled">启用过滤</Label>
              </div>
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <NumberField id="min-market-cap" label="流通市值下限" value={form.min_market_cap} onChange={set("min_market_cap")} hint="单位：亿元" min={0} disabled={!form.stock_pool_enabled} />
              <NumberField id="max-market-cap" label="流通市值上限" value={form.max_market_cap} onChange={set("max_market_cap")} hint="单位：亿元" min={0} disabled={!form.stock_pool_enabled} />
              <NumberField id="min-listing-days" label="最少上市交易日" value={form.min_listing_trade_days} onChange={set("min_listing_trade_days")} hint="技术指标至少仍需60根日线" min={0} max={5000} step={1} disabled={!form.stock_pool_enabled} />
              <NumberField id="amount-window" label="平均成交额窗口" value={form.amount_window} onChange={set("amount_window")} hint="单位：交易日" min={1} max={250} step={1} disabled={!form.stock_pool_enabled} />
              <NumberField id="min-avg-amount" label="最低平均成交额" value={form.min_avg_amount} onChange={set("min_avg_amount")} hint="单位：亿元" min={0} disabled={!form.stock_pool_enabled} />
              <NumberField id="turnover-window" label="平均换手率窗口" value={form.turnover_window} onChange={set("turnover_window")} hint="单位：交易日" min={1} max={250} step={1} disabled={!form.stock_pool_enabled} />
              <NumberField id="min-avg-turnover" label="平均换手率下限" value={form.min_avg_turnover_rate} onChange={set("min_avg_turnover_rate")} hint="单位：%" min={0} max={100} disabled={!form.stock_pool_enabled} />
              <NumberField id="max-avg-turnover" label="平均换手率上限" value={form.max_avg_turnover_rate} onChange={set("max_avg_turnover_rate")} hint="单位：%" min={0} max={100} disabled={!form.stock_pool_enabled} />
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="missing-data-policy">指标缺失时</Label>
                <select
                  id="missing-data-policy"
                  className="h-9 rounded-lg border bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-50"
                  value={form.missing_data_policy}
                  onChange={set("missing_data_policy")}
                  disabled={!form.stock_pool_enabled}
                >
                  <option value="reject">拒绝候选（推荐）</option>
                  <option value="allow">记录警告并放行</option>
                </select>
              </div>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex items-center justify-between rounded-lg bg-muted/50 px-3 py-2">
                <div>
                  <Label htmlFor="exclude-st">排除 ST</Label>
                  <p className="text-xs text-muted-foreground">实时扫描按当前证券名称识别。</p>
                </div>
                <Switch id="exclude-st" checked={form.exclude_st} disabled={!form.stock_pool_enabled} onCheckedChange={(value) => setForm((prev) => ({ ...prev, exclude_st: value }))} />
              </div>
              <div className="flex items-center justify-between rounded-lg bg-muted/50 px-3 py-2">
                <div>
                  <Label htmlFor="exclude-delisting">排除退市风险</Label>
                  <p className="text-xs text-muted-foreground">名称中含“退”的股票不进入候选。</p>
                </div>
                <Switch id="exclude-delisting" checked={form.exclude_delisting} disabled={!form.stock_pool_enabled} onCheckedChange={(value) => setForm((prev) => ({ ...prev, exclude_delisting: value }))} />
              </div>
            </div>
          </section>
          <div className="flex flex-col gap-1.5">
            <Label>监控周期（逗号分隔）</Label>
            <Input value={form.timeframes} onChange={set("timeframes")} placeholder="1m, 5m, 15m, 30m, 60m, 120m, 1d" />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>自选股 watchlist（逗号分隔）</Label>
            <Input
              value={form.watchlist}
              disabled
              placeholder="000001.SZ, 600036.SH"
              className="bg-muted text-muted-foreground"
            />
            <p className="text-xs text-muted-foreground">
              自选股票池由「股票池」页统一维护，增删股票后自动同步到这里，此处不可编辑。
            </p>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>扫描范围</Label>
            <select
              className="h-9 rounded-lg border bg-transparent px-3 text-sm"
              value={form.universe_mode}
              onChange={set("universe_mode")}
            >
              <option value="watchlist">自选股（watchlist）</option>
              <option value="all_a">全市场（all_a）</option>
            </select>
          </div>
          <div>
            <Button onClick={() => void save()} disabled={saving}>
              <Save className="size-4" /> {saving ? "保存中..." : "保存配置"}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
