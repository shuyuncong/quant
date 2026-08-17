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
  llm_context_bars: string;
  buy_threshold: string;
  sell_threshold: string;
  timeframes: string;
  watchlist: string;
  bar_limit: string;
  max_symbols_per_cycle: string;
  universe_mode: string;
}

function num(value: unknown, fallback = ""): string {
  return typeof value === "number" ? String(value) : fallback;
}

function list(value: unknown): string {
  return Array.isArray(value) ? (value as unknown[]).map(String).join(", ") : "";
}

function NumberField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (event: React.ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label>{label}</Label>
      <Input type="number" step="any" value={value} onChange={onChange} />
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
    llm_context_bars: "",
    buy_threshold: "",
    sell_threshold: "",
    timeframes: "",
    watchlist: "",
    bar_limit: "",
    max_symbols_per_cycle: "",
    universe_mode: "watchlist",
  });
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/config/strategies");
      if (!response.ok) throw new Error("加载配置失败");
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data = (await response.json()) as { config: Record<string, any> };
      const signal = data.config.signal_strategy ?? {};
      const monitor = data.config.monitor ?? {};
      const scan = data.config.scan ?? {};
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
        llm_context_bars: num(signal.llm_context_bars, "48"),
        buy_threshold: num(signal.scoring?.buy_threshold, "60"),
        sell_threshold: num(signal.scoring?.sell_threshold, "60"),
        timeframes: list(monitor.timeframes),
        watchlist: list(monitor.watchlist),
        bar_limit: num(monitor.bar_limit, "300"),
        max_symbols_per_cycle: num(monitor.max_symbols_per_cycle, "20"),
        universe_mode: String(scan.universe_mode ?? "watchlist"),
      });
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
      "signal_strategy.llm_context_bars": Number(form.llm_context_bars),
      "signal_strategy.scoring.buy_threshold": Number(form.buy_threshold),
      "signal_strategy.scoring.sell_threshold": Number(form.sell_threshold),
      "monitor.timeframes": form.timeframes.split(/[\s,，;；]+/).filter(Boolean),
      "monitor.watchlist": form.watchlist.split(/[\s,，;；]+/).filter(Boolean),
      "monitor.bar_limit": Number(form.bar_limit),
      "monitor.max_symbols_per_cycle": Number(form.max_symbols_per_cycle),
      "scan.universe_mode": form.universe_mode,
    };
    try {
      const response = await fetch("/api/config/strategies", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      const data = (await response.json().catch(() => ({}))) as { error?: string; errors?: string[] };
      if (!response.ok) {
        throw new Error(data.error || (data.errors ?? []).join("；") || "保存失败");
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
    <div className="flex max-w-3xl flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>策略配置</CardTitle>
          <CardDescription>
            修改后保存到 Web 设置库；下一次分析/扫描立即生效。优先级：环境变量 &gt; Web 设置 &gt; config.yaml。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-6">
          <div className="grid grid-cols-3 gap-4">
            <NumberField label="缠论：最小笔 K 数" value={form.min_bi_bars} onChange={set("min_bi_bars")} />
            <NumberField label="缠论：背驰比" value={form.divergence_ratio} onChange={set("divergence_ratio")} />
            <NumberField label="缠论：新信号K数" value={form.fresh_signal_bars} onChange={set("fresh_signal_bars")} />
            <NumberField label="MACD：快线" value={form.macd_fast} onChange={set("macd_fast")} />
            <NumberField label="MACD：慢线" value={form.macd_slow} onChange={set("macd_slow")} />
            <NumberField label="MACD：信号线" value={form.macd_signal} onChange={set("macd_signal")} />
            <NumberField label="MACD：0轴容差" value={form.zero_axis_tolerance} onChange={set("zero_axis_tolerance")} />
            <NumberField label="温和放量下限" value={form.moderate_volume_min} onChange={set("moderate_volume_min")} />
            <NumberField label="温和放量上限" value={form.moderate_volume_max} onChange={set("moderate_volume_max")} />
            <NumberField label="买入评分阈值" value={form.buy_threshold} onChange={set("buy_threshold")} />
            <NumberField label="卖出评分阈值" value={form.sell_threshold} onChange={set("sell_threshold")} />
            <NumberField label="K线数量（bar_limit）" value={form.bar_limit} onChange={set("bar_limit")} />
            <NumberField label="AI每周期K线数" value={form.llm_context_bars} onChange={set("llm_context_bars")} />
            <NumberField label="盘中每批股票数" value={form.max_symbols_per_cycle} onChange={set("max_symbols_per_cycle")} />
          </div>
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
