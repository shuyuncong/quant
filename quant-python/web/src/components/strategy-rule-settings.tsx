"use client";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { STRATEGIES } from "@/lib/analysis-types";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

const GLOBAL_STOP = "risk.stop_loss_pct";
const STOP_PROFIT = "risk.stop_profit_pct";
const fields = [
  [STOP_PROFIT, "固定止盈比例（%）", .30],
  ["backtest.chan_zero_axis.max_holding_bars", "最多持有日线数", 40],
  ["macd_divergence.min_volume_ratio", "底背离最低量比", 1.5],
  ["macd_divergence.volume_window", "底背离均量窗口", 20],
  ["macd_divergence.long_ma_slope_window", "底背离年线斜率窗口", 20],
  ["macd_divergence.min_macd_segment_bars", "最短 MACD 柱区间", 2],
] as const;

function at(value: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((item, key) => item && typeof item === "object" ? (item as Record<string, unknown>)[key] : undefined, value);
}

function fieldId(key: string): string {
  return `rule-${key.replaceAll(".", "-")}`;
}

/**
 * `/api/config` keeps every rate as a fraction (stop_loss_pct 0.08, stop_profit_pct 0.30), while this
 * form is all percent. Rounding both directions at 10 decimals removes the float noise of the
 * conversion so a reload never reads back as a pending edit.
 */
function percentToInput(fraction: number): string {
  return String(Number((fraction * 100).toFixed(10)));
}

function inputToFraction(percent: number): number {
  return Number((percent / 100).toFixed(10));
}

interface RulesForm {
  values: Record<string, string>;
  stops: Record<string, string | null>;
  globalFromConfig: boolean;
  enabled: boolean;
  mode: string;
  modeName: string;
}

function formFromConfig(config: unknown): RulesForm {
  const global = at(config, GLOBAL_STOP);
  const stopProfit = at(config, STOP_PROFIT);
  return {
    values: {
      ...Object.fromEntries(fields.map(([path, , fallback]) => [path, String(at(config, path) ?? fallback)])),
      [STOP_PROFIT]: percentToInput(typeof stopProfit === "number" ? stopProfit : .30),
      [GLOBAL_STOP]: percentToInput(typeof global === "number" ? global : .08),
    },
    stops: Object.fromEntries(STRATEGIES.map(({ id }) => {
      const value = at(config, `risk.strategy_stop_loss_pct.${id}`);
      return [id, value == null ? null : percentToInput(Number(value))];
    })),
    globalFromConfig: typeof global === "number",
    enabled: at(config, "macd_divergence.enabled") === true,
    mode: JSON.stringify(at(config, "backtest.exit_rules") ?? { mode: "fixed" }),
    modeName: String(at(config, "backtest.exit_rules.mode") ?? "fixed"),
  };
}

/** Numeric inputs compare by value, so retyping 30 as 30.0 (or clearing a field) stays one edit. */
function numberTextDiffers(saved: string, current: string): boolean {
  if (saved === current) return false;
  if (saved.trim() === "" || current.trim() === "") return true;
  return Number(saved) !== Number(current);
}

/** Edits are measured against the last successful load/save, so reverting a field clears it again. */
function countChangedValues(baseline: RulesForm | null, current: RulesForm | null): number {
  if (!baseline || !current) return 0;
  let changed = 0;
  for (const [key] of fields) {
    if (numberTextDiffers(baseline.values[key], current.values[key])) changed += 1;
  }
  if (numberTextDiffers(baseline.values[GLOBAL_STOP], current.values[GLOBAL_STOP])) changed += 1;
  for (const { id } of STRATEGIES) {
    const savedStop = baseline.stops[id];
    const currentStop = current.stops[id];
    const differs = savedStop == null || currentStop == null ? savedStop !== currentStop : numberTextDiffers(savedStop, currentStop);
    if (differs) changed += 1;
  }
  if (baseline.enabled !== current.enabled) changed += 1;
  return changed;
}

export function StrategyRuleSettings({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void } = {}) {
  const [form, setForm] = useState<RulesForm | null>(null);
  const [baseline, setBaseline] = useState<RulesForm | null>(null);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const errorSummary = useRef<HTMLDivElement>(null);
  const changedCount = countChangedValues(baseline, form);
  const dirtyCallback = useRef(onDirtyChange);

  useEffect(() => {
    dirtyCallback.current = onDirtyChange;
  }, [onDirtyChange]);

  useEffect(() => {
    dirtyCallback.current?.(changedCount > 0);
  }, [changedCount]);

  useEffect(() => {
    const abort = new AbortController();
    void fetch("/api/config", { signal: abort.signal }).then(async response => {
      const data = await response.json();
      if (!response.ok) throw new Error(data.error ?? "读取规则配置失败");
      const loaded = formFromConfig(data.config);
      setForm(loaded);
      setBaseline(loaded);
      setLoadError("");
    }).catch(error => {
      if (!abort.signal.aborted) setLoadError(String(error));
    });
    return () => abort.abort();
  }, []);

  function showErrors(next: Record<string, string>) {
    setErrors(next);
    requestAnimationFrame(() => errorSummary.current?.focus());
  }

  function setValue(key: string, value: string) {
    setForm(current => current && { ...current, values: { ...current.values, [key]: value }, globalFromConfig: key === GLOBAL_STOP || current.globalFromConfig });
  }

  function setStop(id: string, value: string | null) {
    setForm(current => current && { ...current, stops: { ...current.stops, [id]: value } });
    // Re-editing that strategy or toggling inheritance clears only its own error (and any save error).
    if (value == null || Object.keys(errors).length) setErrors(current => Object.fromEntries(Object.entries(current).filter(([key]) => key !== `risk.strategy_stop_loss_pct.${id}` && key !== "request")));
  }

  function discard() {
    if (!baseline) return;
    setForm(baseline);
    setErrors({});
  }

  async function save() {
    if (!form || saving) return;
    const nextErrors: Record<string, string> = {};
    const payload: Record<string, number | boolean | null> = { "macd_divergence.enabled": form.enabled };
    for (const [key, label] of [[GLOBAL_STOP, "全局默认止损"], ...fields] as const) {
      const raw = form.values[key];
      const value = Number(raw);
      if (!raw?.trim() || !Number.isFinite(value)) nextErrors[key] = `${label}必须填写有效数字`;
      else if (key === GLOBAL_STOP && (value < .1 || value > 99)) nextErrors[key] = "全局默认止损必须在 0.1%～99% 之间";
      else if (key === STOP_PROFIT && (value < .1 || value > 1000)) nextErrors[key] = "固定止盈比例必须在 0.1%～1000% 之间";
      payload[key] = key === GLOBAL_STOP || key === STOP_PROFIT ? inputToFraction(value) : value;
    }
    for (const { id, name } of STRATEGIES) {
      const key = `risk.strategy_stop_loss_pct.${id}`;
      const raw = form.stops[id];
      if (raw == null) payload[key] = null;
      else {
        const value = Number(raw);
        if (!raw.trim() || !Number.isFinite(value) || value < .1 || value > 99) nextErrors[key] = `${name}止损必须在 0.1%～99% 之间`;
        payload[key] = inputToFraction(value);
      }
    }
    if (Object.keys(nextErrors).length) { showErrors(nextErrors); return; }
    setErrors({});
    setSaving(true);
    let saved = false;
    try {
      const response = await fetch("/api/config/strategies", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.errors?.join("；") ?? data.error ?? "保存失败");
      saved = true;
      const refreshed = await fetch("/api/config");
      const effective = await refreshed.json();
      if (!refreshed.ok) throw new Error(effective.error ?? "重新读取配置失败");
      const reloaded = formFromConfig(effective.config);
      setForm(reloaded);
      setBaseline(reloaded);
      toast.success("止损与退出规则已保存，后续任务生效");
    } catch (error) {
      showErrors({ request: `${saved ? "配置已保存，但重新读取失败：" : ""}${String(error)}` });
    } finally {
      setSaving(false);
    }
  }

  return <section className="space-y-4 rounded-lg border p-4" aria-labelledby="strategy-rules-heading">
    <div>
      <h2 id="strategy-rules-heading" className="font-semibold">三策略规则与退出参数</h2>
      <p className="mt-1 text-sm text-muted-foreground">年线入场六条件固定在代码中。策略止损优先于全局默认；未归属策略的实际持仓沿用全局止损，不按多个信号中的最小值执行。新任务冻结提交时的规则，修改不追溯既有任务和历史报告。</p>
    </div>
    {!!Object.keys(errors).length && <div ref={errorSummary} tabIndex={-1} role="alert" className="rounded-md border border-destructive p-3 text-sm text-destructive">
      <p className="font-medium">请检查规则配置</p>
      <ul className="mt-1 list-inside list-disc">
        {Object.entries(errors).map(([key, error]) => <li key={key}>{key === "request" ? error : <a className="underline" href={`#${fieldId(key)}`}>{error}</a>}</li>)}
      </ul>
    </div>}
    {!form ? <p className="text-sm text-muted-foreground">{loadError ? "规则配置未加载，暂不能编辑。" : "正在加载规则配置…"}</p> : <form noValidate onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset disabled={saving} className="space-y-4">
        <legend className="sr-only">策略止损与退出规则</legend>
        <div className="max-w-sm space-y-1">
          <label className="text-sm font-medium" htmlFor={fieldId(GLOBAL_STOP)}>全局默认止损（%）</label>
          <Input id={fieldId(GLOBAL_STOP)} type="number" min="0.1" max="99" step="any" value={form.values[GLOBAL_STOP]} onChange={event => setValue(GLOBAL_STOP, event.target.value)} aria-invalid={!!errors[GLOBAL_STOP]} aria-describedby={errors[GLOBAL_STOP] ? `${fieldId(GLOBAL_STOP)}-error global-stop-hint` : "global-stop-hint"} />
          {errors[GLOBAL_STOP] && <p id={`${fieldId(GLOBAL_STOP)}-error`} className="text-xs text-destructive">{errors[GLOBAL_STOP]}</p>}
          <p id="global-stop-hint" className="text-xs text-muted-foreground">按百分比填写：8 表示 8%（保存为 0.08）。未独立设置的策略及未归属持仓使用此值。</p>
        </div>
        <div className="grid gap-3 lg:grid-cols-3">
          {STRATEGIES.map(({ id, name }) => {
            const key = `risk.strategy_stop_loss_pct.${id}`;
            const inherited = form.stops[id] == null;
            const value = inherited ? form.values[GLOBAL_STOP] : form.stops[id]!;
            const valid = value.trim() !== "" && Number.isFinite(Number(value)) && Number(value) >= .1 && Number(value) <= 99;
            const source = inherited ? (form.globalFromConfig ? "继承全局" : "代码默认") : "策略独立配置";
            return <div key={id} className="space-y-3 rounded-lg border p-3">
              <h3 className="text-sm font-medium">{name}</h3>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={inherited} aria-label={`${name}继承全局止损`} onChange={event => setStop(id, event.target.checked ? null : form.values[GLOBAL_STOP])} />继承全局止损
              </label>
              <div className="space-y-1">
                <label className="text-xs text-muted-foreground" htmlFor={fieldId(key)}>{name}止损（%）</label>
                <Input id={fieldId(key)} type="number" min="0.1" max="99" step="any" disabled={inherited} value={value} aria-invalid={!!errors[key]} aria-describedby={`${fieldId(key)}-status`} onChange={event => setStop(id, event.target.value)} />
                <p id={`${fieldId(key)}-status`} className={`text-xs ${errors[key] ? "text-destructive" : "text-muted-foreground"}`} aria-live="polite">
                  {errors[key] ?? (valid ? `${changedCount > 0 ? "生效预览" : "当前生效"}：${Number(value)}% · ${source}` : "请填写有效止损比例")}
                </p>
              </div>
            </div>;
          })}
        </div>
        <p className="text-xs text-muted-foreground">勾选继承并保存会清除该策略的独立覆盖。未保存的编辑仅作预览，不会改变正在运行的任务；不会自动应用回测中的候选参数。</p>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {fields.map(([key, label]) => <div key={key} className="grid gap-1 text-sm">
            <label htmlFor={fieldId(key)}>{label}</label>
            <Input id={fieldId(key)} type="number" step="any" min={key === STOP_PROFIT ? "0.1" : undefined} max={key === STOP_PROFIT ? "1000" : undefined} value={form.values[key]} onChange={event => setValue(key, event.target.value)} aria-invalid={!!errors[key]} aria-describedby={errors[key] ? `${fieldId(key)}-error` : key === STOP_PROFIT ? `${fieldId(key)}-hint` : undefined} />
            {errors[key] ? <p id={`${fieldId(key)}-error`} className="text-xs text-destructive">{errors[key]}</p> : null}
            {key === STOP_PROFIT ? <p id={`${fieldId(key)}-hint`} className="text-xs text-muted-foreground">按百分比填写：30 表示 30%（保存为 0.30）；允许 0.1～1000。</p> : null}
          </div>)}
        </div>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.enabled} onChange={event => {
          const enabled = event.target.checked;
          setForm(current => current && { ...current, enabled });
        }} />启用零轴＋底背离研究池</label>
        <details className="rounded-md border p-3 text-xs text-muted-foreground">
          <summary className="cursor-pointer font-medium text-foreground">退出作用范围（原始 exit_rules）</summary>
          <p className="mt-2">
            {form.modeName === "divergence_trend"
              ? "当前规则集 divergence_trend：用「止损＋日线 MACD 顶背离＋跌破年线＋持仓超时」替代固定止盈。"
              : form.modeName === "fixed"
                ? "当前规则集 fixed：只按固定止盈比例与最多持有日线数离场。"
                : `当前规则集「${form.modeName}」不是 fixed / divergence_trend，回测读取该配置时会直接报错。`}
            下方 <code>apply_to_signal_types</code> 限定规则集作用的入场信号，未列出的信号仍按 fixed 口径回测。
          </p>
          <p className="mt-2">本页面只原样回传这段 JSON，不修改其结构；切换规则集需在 config.yaml 的 <code>backtest.exit_rules</code> 中调整。</p>
          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono text-[11px] text-foreground">{form.mode}</pre>
        </details>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit">{saving ? "保存中…" : "保存止损与退出规则"}</Button>
          <Button type="button" variant="outline" onClick={discard} disabled={saving || changedCount === 0}>放弃修改</Button>
          <span className="text-xs text-muted-foreground" role="status" aria-live="polite">
            {saving ? "保存中，输入已冻结…" : changedCount > 0 ? `${changedCount} 项修改未保存` : "已保存（无未保存修改）"}
          </span>
        </div>
      </fieldset>
    </form>}
  </section>;
}
