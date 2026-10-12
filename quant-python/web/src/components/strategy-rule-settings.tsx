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
  /** null = 固定止盈全清；否则为 resolve_exit_plan 的深层计划对象。 */
  exitPlan: ExitPlanForm | null;
  /** 库里存在带移动止盈等本表单读不懂的计划：只读展示并提示用 API/YAML 编辑。 */
  advancedExitPlan: boolean;
  /** advancedExitPlan 时保留的原始计划，保存时原样回写。 */
  advancedExitPlanRaw?: Record<string, unknown> | null;
  /** 策略级计划：继承全局 / 单独关闭分批 / 使用自定义计划。 */
  strategyPlans: Record<string, StrategyPlanForm>;
}

/**
 * 网页端分批止盈编辑器只覆盖普通阶梯 (initial_fraction / first_gain / tiers) 与保本止损；
 * 若库里存在带移动止盈 (trailing_*) 或这里读不懂的计划，按"原样保留"处理，
 * 避免用简化表单把高级字段静默删掉。
 */
interface ExitPlanForm {
  firstGain: string;
  initialPercent: string;
  tiers: Array<{ gain: string; fraction: string }>;
  protectTail: boolean;
  /** 原计划里本表单无法编辑的字段，保存时原样并回。 */
  preserved: Record<string, unknown>;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * 三种状态互为补充：`inherit` = 未配置 (null)，跟随全局计划；`off` = 显式空对象 `{}`，
 * 该策略保持一次性全清；`custom` = 该策略自己的计划。Python 端 resolve_exit_plan 用
 * 同样的语义解析，因此这里的空对象必须原样保存，不能被当成"没有配置"删掉。
 */
type StrategyPlanMode = "inherit" | "off" | "custom";

interface StrategyPlanForm {
  mode: StrategyPlanMode;
  plan: ExitPlanForm | null;
  advanced: boolean;
  /** 读到但本表单不支持的原始计划，原样带回以免保存时丢失移动止盈等字段。 */
  raw?: Record<string, unknown>;
}

function exitPlanFromConfig(config: unknown, path: string): { plan: ExitPlanForm | null; advanced: boolean; raw: Record<string, unknown> | null } {
  const raw = at(config, path);
  if (raw == null) return { plan: null, advanced: false, raw: null };
  if (!isPlainObject(raw)) return { plan: null, advanced: true, raw: null };
  const preserved: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(raw)) {
    if (!["initial_fraction", "first_gain", "tiers", "protect_tail"].includes(key)) preserved[key] = value;
  }
  const tiers = Array.isArray(raw.tiers) ? raw.tiers : [];
  const simple = tiers.every((tier) => isPlainObject(tier) && Object.keys(tier).every((key) => key === "gain" || key === "fraction"));
  // 任何本表单读不懂的字段 (移动止盈、附加条件) 都按只读处理：表单显示的首档/比例是补位默认值，
  // 若允许编辑就会把一份"只剩移动止盈"的计划悄悄改成阶梯计划。
  if (!simple || Object.keys(preserved).length > 0) return { plan: null, advanced: true, raw };
  return {
    plan: {
      firstGain: percentToInput(Number(raw.first_gain ?? 0.15)),
      initialPercent: percentToInput(Number(raw.initial_fraction ?? 0.5)),
      tiers: tiers.map((tier) => ({ gain: percentToInput(Number((tier as Record<string, unknown>).gain ?? 0.30)),
                                    fraction: percentToInput(Number((tier as Record<string, unknown>).fraction ?? 0.5)) })),
      protectTail: raw.protect_tail === "breakeven",
      preserved,
    },
    advanced: false,
    raw,
  };
}

/** Strategy-scoped plan: empty object means "this strategy keeps the single all-out exit". */
function strategyPlanFromConfig(config: unknown, id: string): StrategyPlanForm {
  const raw = at(config, `risk.strategy_exit_plan.${id}`);
  if (raw == null) return { mode: "inherit", plan: null, advanced: false };
  if (isPlainObject(raw) && Object.keys(raw).length === 0) return { mode: "off", plan: null, advanced: false };
  const parsed = exitPlanFromConfig(config, `risk.strategy_exit_plan.${id}`);
  return { mode: "custom", plan: parsed.plan, advanced: parsed.advanced, raw: parsed.raw ?? undefined };
}

function parsePercent(text: string): number | null {
  if (!text.trim()) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

/** Build the plan object from the form, or a list of human-readable errors. Returns null on error. */
function planFromForm(form: ExitPlanForm): { plan: Record<string, unknown> } | { errors: string[] } {
  const errors: string[] = [];
  const firstGain = parsePercent(form.firstGain);
  const initial = parsePercent(form.initialPercent);
  if (firstGain == null || firstGain <= 0 || firstGain > 1000) errors.push("首个减仓涨幅必须在 0.1%～1000% 之间");
  if (initial == null || initial <= 0 || initial > 100) errors.push("首档减仓比例必须在 0.1%～100% 之间");
  const tiers = [] as Array<{ gain: number; fraction: number }>;
  const seen = new Set<number>();
  let lastGain = firstGain ?? 0;
  let totalFraction = (initial ?? 0) / 100;
  form.tiers.forEach((tier, index) => {
    const gain = parsePercent(tier.gain);
    const fraction = parsePercent(tier.fraction);
    if (gain == null || gain <= 0 || gain > 1000) errors.push(`第 ${index + 1} 档涨幅必须在 0.1%～1000% 之间`);
    if (fraction == null || fraction <= 0 || fraction > 100) errors.push(`第 ${index + 1} 档减仓比例必须在 0.1%～100% 之间`);
    if (gain == null || fraction == null) return;
    if (gain <= lastGain) errors.push(`第 ${index + 1} 档涨幅必须大于前一档 (${lastGain}%)`);
    if (seen.has(gain)) errors.push(`第 ${index + 1} 档涨幅与前面的档位重复`);
    seen.add(gain);
    lastGain = gain;
    totalFraction += fraction / 100;
    tiers.push({ gain: gain / 100, fraction: fraction / 100 });
  });
  if (totalFraction > 1 + 1e-9) errors.push("各档减仓比例之和不能超过 100%");
  if (errors.length) return { errors };
  return { plan: { first_gain: (firstGain as number) / 100, initial_fraction: (initial as number) / 100,
                   tiers, protect_tail: form.protectTail ? "breakeven" : "none", ...form.preserved } };
}

function plansEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** Plans plus their mode differ in shape, so compare the serialized pair as one unit. */
function strategyPlanSignature(form: StrategyPlanForm | undefined) {
  return JSON.stringify([form?.mode ?? null, form?.advanced ?? false, form?.plan ?? null]);
}

function formFromConfig(config: unknown): RulesForm {
  const global = at(config, GLOBAL_STOP);
  const stopProfit = at(config, STOP_PROFIT);
  const exitPlan = exitPlanFromConfig(config, "risk.exit_plan");
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
    exitPlan: exitPlan.plan,
    advancedExitPlan: exitPlan.advanced,
    advancedExitPlanRaw: exitPlan.advanced ? exitPlan.raw : null,
    strategyPlans: Object.fromEntries(STRATEGIES.map(({ id }) => [id, strategyPlanFromConfig(config, id)])),
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
  if (!plansEqual(baseline.exitPlan ?? {}, current.exitPlan ?? {})) changed += 1;
  for (const { id } of STRATEGIES) {
    if (strategyPlanSignature(baseline.strategyPlans[id]) !== strategyPlanSignature(current.strategyPlans[id])) changed += 1;
  }
  return changed;
}

/**
 * 全局计划与策略级计划共用同一组字段：档位 = 首档 (涨幅/比例) + 追加档位 + 尾仓保本。
 * `idPrefix` 决定控件 id，让错误摘要能锚定到具体策略的字段上。
 */
function PlanFields({ plan, idPrefix, onChangeField, onTierChange, onAddTier, onRemoveTier }: {
  plan: ExitPlanForm;
  idPrefix: string;
  onChangeField: <K extends "firstGain" | "initialPercent" | "protectTail">(key: K, value: ExitPlanForm[K]) => void;
  onTierChange: (index: number, key: "gain" | "fraction", value: string) => void;
  onAddTier: () => void;
  onRemoveTier: (index: number) => void;
}) {
  return <>
    <div className="grid gap-3 sm:grid-cols-3">
      <div className="grid gap-1 text-sm">
        <label htmlFor={fieldId(`${idPrefix}.first_gain`)}>首档涨幅（%）</label>
        <Input id={fieldId(`${idPrefix}.first_gain`)} type="number" min="0.1" max="1000" step="any" value={plan.firstGain} onChange={event => onChangeField("firstGain", event.target.value)} />
      </div>
      <div className="grid gap-1 text-sm">
        <label htmlFor={fieldId(`${idPrefix}.initial_fraction`)}>首档减仓（原始仓位%）</label>
        <Input id={fieldId(`${idPrefix}.initial_fraction`)} type="number" min="0.1" max="100" step="any" value={plan.initialPercent} onChange={event => onChangeField("initialPercent", event.target.value)} />
      </div>
      <label className="flex items-end gap-2 pb-2 text-sm">
        <input type="checkbox" checked={plan.protectTail} onChange={event => onChangeField("protectTail", event.target.checked)} />首档后尾仓止损上移到成本价
      </label>
    </div>
    {plan.tiers.length > 0 && <div className="space-y-2">
      {plan.tiers.map((tier, index) => <div key={index} className="flex items-end gap-2">
        <div className="grid gap-1 text-sm">
          <label htmlFor={fieldId(`${idPrefix}.tiers.${index}.gain`)}>第 {index + 2} 档涨幅（%）</label>
          <Input id={fieldId(`${idPrefix}.tiers.${index}.gain`)} type="number" min="0.1" max="1000" step="any" value={tier.gain} onChange={event => onTierChange(index, "gain", event.target.value)} />
        </div>
        <div className="grid gap-1 text-sm">
          <label htmlFor={fieldId(`${idPrefix}.tiers.${index}.fraction`)}>减仓（原始仓位%）</label>
          <Input id={fieldId(`${idPrefix}.tiers.${index}.fraction`)} type="number" min="0.1" max="100" step="any" value={tier.fraction} onChange={event => onTierChange(index, "fraction", event.target.value)} />
        </div>
        <Button type="button" variant="outline" onClick={() => onRemoveTier(index)}>删除</Button>
      </div>)}
    </div>}
    <div className="flex flex-wrap items-center gap-3">
      <Button type="button" variant="outline" onClick={onAddTier}>增加档位</Button>
      <p className="text-xs text-muted-foreground">涨幅为相对成本价的绝对涨幅；各档之和不得超过 100%。</p>
    </div>
  </>;
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

  function setPlan(next: ExitPlanForm | null) {
    setForm(current => current && { ...current, exitPlan: next });
    if (Object.keys(errors).length) setErrors(current => Object.fromEntries(Object.entries(current).filter(([key]) => key !== "risk.exit_plan" && key !== "request")));
  }

  function togglePlan(enabledPlan: boolean) {
    setPlan(enabledPlan
      ? { firstGain: "15", initialPercent: "50", tiers: [], protectTail: true, preserved: {} }
      : null);
  }

  function setPlanField<K extends "firstGain" | "initialPercent" | "protectTail">(key: K, value: ExitPlanForm[K]) {
    setForm(current => current?.exitPlan ? { ...current, exitPlan: { ...current.exitPlan, [key]: value } } : current);
  }

  function setTier(index: number, key: "gain" | "fraction", value: string) {
    setForm(current => current?.exitPlan
      ? { ...current, exitPlan: { ...current.exitPlan, tiers: current.exitPlan.tiers.map((tier, at) => at === index ? { ...tier, [key]: value } : tier) } }
      : current);
  }

  function addTier() {
    setForm(current => {
      if (!current?.exitPlan) return current;
      const gain = parsePercent(current.exitPlan.tiers.at(-1)?.gain ?? current.exitPlan.firstGain) ?? 30;
      return { ...current, exitPlan: { ...current.exitPlan, tiers: [...current.exitPlan.tiers, { gain: String(gain + 15), fraction: "50" }] } };
    });
  }

  function removeTier(index: number) {
    setForm(current => current?.exitPlan
      ? { ...current, exitPlan: { ...current.exitPlan, tiers: current.exitPlan.tiers.filter((_, at) => at !== index) } }
      : current);
  }

  function setScopedMode(id: string, mode: StrategyPlanMode) {
    setForm(current => {
      if (!current) return current;
      const existing = current.strategyPlans[id];
      // 切到自定义时沿用全局计划作为起点，避免用户从空白开始；切回继承/关闭则丢掉本地计划。
      const plan = mode === "custom"
        ? (existing.plan ?? current.exitPlan ?? { firstGain: "15", initialPercent: "50", tiers: [], protectTail: true, preserved: {} })
        : null;
      return { ...current, strategyPlans: { ...current.strategyPlans, [id]: { mode, plan, advanced: mode === "custom" ? existing.advanced : false } } };
    });
    if (Object.keys(errors).length) setErrors(current => Object.fromEntries(Object.entries(current).filter(([key]) => key !== `risk.strategy_exit_plan.${id}` && key !== "request")));
  }

  function setScopedPlanField<K extends "firstGain" | "initialPercent" | "protectTail">(id: string, key: K, value: ExitPlanForm[K]) {
    setForm(current => {
      const scoped = current?.strategyPlans[id];
      return current && scoped?.plan ? { ...current, strategyPlans: { ...current.strategyPlans, [id]: { ...scoped, plan: { ...scoped.plan, [key]: value } } } } : current;
    });
  }

  function setScopedTier(id: string, index: number, key: "gain" | "fraction", value: string) {
    setForm(current => {
      const scoped = current?.strategyPlans[id];
      return current && scoped?.plan
        ? { ...current, strategyPlans: { ...current.strategyPlans, [id]: { ...scoped, plan: { ...scoped.plan, tiers: scoped.plan.tiers.map((tier, at) => at === index ? { ...tier, [key]: value } : tier) } } } }
        : current;
    });
  }

  function addScopedTier(id: string) {
    setForm(current => {
      const scoped = current?.strategyPlans[id];
      if (!current || !scoped?.plan) return current;
      const gain = parsePercent(scoped.plan.tiers.at(-1)?.gain ?? scoped.plan.firstGain) ?? 30;
      return { ...current, strategyPlans: { ...current.strategyPlans, [id]: { ...scoped, plan: { ...scoped.plan, tiers: [...scoped.plan.tiers, { gain: String(gain + 15), fraction: "50" }] } } } };
    });
  }

  function removeScopedTier(id: string, index: number) {
    setForm(current => {
      const scoped = current?.strategyPlans[id];
      return current && scoped?.plan
        ? { ...current, strategyPlans: { ...current.strategyPlans, [id]: { ...scoped, plan: { ...scoped.plan, tiers: scoped.plan.tiers.filter((_, at) => at !== index) } } } }
        : current;
    });
  }

  function discard() {
    if (!baseline) return;
    setForm(baseline);
    setErrors({});
  }

  async function save() {
    if (!form || saving) return;
    const nextErrors: Record<string, string> = {};
    const payload: Record<string, number | boolean | null | Record<string, unknown>> = { "macd_divergence.enabled": form.enabled };
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
    // 读不懂的计划按原值回写：既不改动它，也避免把整段配置清空。
    const planPayload: Record<string, unknown> = { "risk.exit_plan": null };
    if (form.advancedExitPlan) {
      if (form.advancedExitPlanRaw) planPayload["risk.exit_plan"] = form.advancedExitPlanRaw;
      else nextErrors["risk.exit_plan"] = "当前分批止盈计划格式无法识别，请改用 API 或 config.yaml 修正";
    } else if (form.exitPlan) {
      const built = planFromForm(form.exitPlan);
      if ("errors" in built) nextErrors["risk.exit_plan"] = built.errors.join("；");
      else planPayload["risk.exit_plan"] = built.plan;
    }
    for (const { id, name } of STRATEGIES) {
      const scoped = form.strategyPlans[id];
      const key = `risk.strategy_exit_plan.${id}`;
      if (scoped.mode === "inherit") { planPayload[key] = null; continue; }
      // 空对象是"该策略关闭分批"的有效取值，必须原样下发，不能被当成缺省。
      if (scoped.mode === "off") { planPayload[key] = {}; continue; }
      if (scoped.advanced) {
        if (scoped.raw) planPayload[key] = scoped.raw;
        else nextErrors[key] = `${name}的分批止盈计划格式无法识别，请改用 API 或 config.yaml 修正`;
        continue;
      }
      if (!scoped.plan) { nextErrors[key] = `${name}的分批止盈计划无法编辑，请改用 API 或 config.yaml`; continue; }
      const built = planFromForm(scoped.plan);
      if ("errors" in built) nextErrors[key] = built.errors.join("；");
      else planPayload[key] = built.plan;
    }
    Object.assign(payload, planPayload);
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
        <div id={fieldId("risk.exit_plan")} tabIndex={-1} className="space-y-3 rounded-lg border p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-medium">分批止盈（分批减仓）</h3>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.exitPlan != null} onChange={event => togglePlan(event.target.checked)} />启用分批减仓
            </label>
          </div>
          {form.advancedExitPlan
            ? <p className="text-xs text-destructive">当前计划含移动止盈等本表单不支持的字段，已按只读处理以免保存时被覆盖；请改用 <code>/api/config/strategies</code> 或 config.yaml 编辑。</p>
            : form.exitPlan == null
              ? <p className="text-xs text-muted-foreground">未启用时沿用「固定止盈比例」一次性全清。启用后各档按原始仓位的百分比在指定涨幅处减仓；最后一档会自动吸收不足一手的零头。</p>
              : <PlanFields plan={form.exitPlan} idPrefix="risk.exit_plan" onChangeField={setPlanField} onTierChange={setTier} onAddTier={addTier} onRemoveTier={removeTier} />}
          {errors["risk.exit_plan"] && <p className="text-xs text-destructive">{errors["risk.exit_plan"]}</p>}
        </div>
        <div className="space-y-3 rounded-lg border p-3">
          <div>
            <h3 className="text-sm font-medium">按策略覆盖分批计划</h3>
            <p className="mt-1 text-xs text-muted-foreground">
              双样本回测结论：日线零轴金叉适合「半仓@+15% + 尾仓保本」，零轴＋底背离适合「阶梯 1/3@15/30/45」，年线趋势保持一次性全清。选择「继承全局」表示跟随上面的计划；选择「关闭分批」写入空计划，该策略始终一次性全清。
            </p>
          </div>
          <div className="grid gap-3 lg:grid-cols-3">
            {STRATEGIES.map(({ id, name }) => {
              const key = `risk.strategy_exit_plan.${id}`;
              const scoped = form.strategyPlans[id];
              return <div key={id} className="space-y-3 rounded-lg border p-3">
                <h3 className="text-sm font-medium">{name}</h3>
                <div className="grid gap-1 text-sm">
                  <label className="text-xs text-muted-foreground" htmlFor={fieldId(key)}>{name}分批止盈</label>
                  <select id={fieldId(key)} aria-label={`${name}分批止盈方式`} className="h-9 rounded-md border bg-transparent px-2 text-sm" value={scoped.mode} onChange={event => setScopedMode(id, event.target.value as StrategyPlanMode)} aria-invalid={!!errors[key]}>
                    <option value="inherit">继承全局计划</option>
                    <option value="off">关闭分批（一次性全清）</option>
                    <option value="custom">使用自定义计划</option>
                  </select>
                </div>
                {scoped.mode === "custom" && scoped.advanced && <p className="text-xs text-destructive">该策略的计划含移动止盈等本表单不支持的字段，已按只读处理；请改用 API 或 config.yaml 编辑。</p>}
                {scoped.mode === "custom" && scoped.plan && !scoped.advanced && <PlanFields plan={scoped.plan} idPrefix={key} onChangeField={(field, value) => setScopedPlanField(id, field, value)} onTierChange={(index, field, value) => setScopedTier(id, index, field, value)} onAddTier={() => addScopedTier(id)} onRemoveTier={index => removeScopedTier(id, index)} />}
                {errors[key] && <p className="text-xs text-destructive">{errors[key]}</p>}
              </div>;
            })}
          </div>
          <p className="text-xs text-muted-foreground">这里的取值是回测研究结论，不是自动生效的推荐；启用前请先确认下单与提醒口径（线上提醒暂不驱动分批卖出）。</p>
        </div>
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
