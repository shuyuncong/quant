/** Read-only projection of existing engine reports. No signal recalculation. */
export interface DataBar {
  datetime: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  dif: number | null;
  dea: number | null;
  hist: number | null;
}

export interface ReportCenter {
  zd: number;
  zg: number;
  start_time: string | null;
  end_time: string | null;
}

export interface ReportSignal {
  signal_type: string;
  side: string;
  price: number | null;
  structure_time: string | null;
  confirmed_at: string | null;
  execution_mode: string | null;
  actionable: boolean | null;
}

export type IndicatorValue = string | number | boolean | null | string[];
export interface DataTimeframe {
  timeframe: string;
  status: string;
  latest_time: string | null;
  latest_price: number | null;
  /** Number of bars saved in the original report; retained for old clients. */
  bar_count: number;
  history_bar_count: number | null;
  displayed_bar_count: number;
  buy_score: number | null;
  sell_score: number | null;
  error: string | null;
  bars: DataBar[];
  indicators: Record<string, IndicatorValue>;
  latest_center: ReportCenter | null;
  fresh_signals: ReportSignal[];
  events: ReportSignal[];
}

export interface DataResult {
  symbol: string;
  name: string;
  status: string | null;
  analyzed_at: string | null;
  timeframes: DataTimeframe[];
}

export interface DataSource {
  mode: string;
  analyzed_at: string | null;
  scanned_at: string | null;
  market_context: Record<string, unknown> | null;
  delivery: Record<string, unknown> | null;
  results: DataResult[];
  candidates: Array<Record<string, unknown>>;
  errors: Array<Record<string, unknown>>;
}

const BAR_LIMIT = 120;
const INDICATORS = [
  "history_complete", "source_mode", "source_warning", "analysis_warning",
  "dif", "dea", "hist", "golden_cross", "death_cross", "golden_cross_state",
  "golden_cross_entry_ready", "golden_cross_entry_zone", "golden_cross_pullback_touched",
  "golden_cross_first_confirmation_time", "golden_cross_cross_time", "golden_cross_zone_label",
  "confirmation_count", "confirmation_items", "above_ma60", "ma60", "ma_long",
  "distance_to_ma_long", "recent_return", "volume_ratio", "price_change",
  "high_position_risk", "high_volume_risk", "position_risk_flags",
] as const;

export function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}
export function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function textOrNull(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}
function recordOrNull(value: unknown): Record<string, unknown> | null {
  const record = asRecord(value);
  return Object.keys(record).length ? record : null;
}
function signals(value: unknown): ReportSignal[] {
  if (!Array.isArray(value)) return [];
  return value.slice(-30).filter((item) => typeof asRecord(item).signal_type === "string").map((item) => {
    const row = asRecord(item);
    const evidence = asRecord(row.evidence);
    const actionable = evidence.actionable ?? row.actionable;
    return {
      signal_type: String(row.signal_type), side: textOrNull(row.side) ?? "unknown",
      price: finiteNumber(row.price), structure_time: textOrNull(row.structure_time),
      confirmed_at: textOrNull(row.confirmed_at),
      execution_mode: textOrNull(evidence.execution_mode ?? row.execution_mode),
      actionable: typeof actionable === "boolean" ? actionable : null,
    };
  });
}

export function compactTimeframe(timeframe: string, value: unknown): DataTimeframe {
  const tf = asRecord(value);
  const rawBars = Array.isArray(tf.recent_bars) ? tf.recent_bars : [];
  const sourceIndicators = asRecord(tf.indicators);
  const indicators: DataTimeframe["indicators"] = {};
  for (const key of INDICATORS) {
    const value = sourceIndicators[key];
    if (typeof value === "number") indicators[key] = finiteNumber(value);
    else if (typeof value === "string" || typeof value === "boolean" || value === null) indicators[key] = value;
    else if (Array.isArray(value)) indicators[key] = value.filter((v): v is string => typeof v === "string").slice(0, 20);
  }
  const chan = asRecord(tf.chan);
  const center = asRecord(chan.latest_center);
  const zd = finiteNumber(center.zd);
  const zg = finiteNumber(center.zg);
  const bars = rawBars.slice(-BAR_LIMIT).map((item): DataBar => {
    const bar = asRecord(item);
    return {
      datetime: textOrNull(bar.datetime) ?? "",
      open: finiteNumber(bar.open), high: finiteNumber(bar.high), low: finiteNumber(bar.low),
      close: finiteNumber(bar.close), volume: finiteNumber(bar.volume),
      dif: finiteNumber(bar.dif), dea: finiteNumber(bar.dea), hist: finiteNumber(bar.hist),
    };
  });
  return {
    timeframe, status: textOrNull(tf.status) ?? "unknown",
    latest_time: textOrNull(tf.latest_time), latest_price: finiteNumber(tf.latest_price),
    bar_count: rawBars.length, history_bar_count: finiteNumber(sourceIndicators.bar_count),
    displayed_bar_count: bars.length, buy_score: finiteNumber(tf.buy_score), sell_score: finiteNumber(tf.sell_score),
    error: textOrNull(tf.error), bars, indicators,
    latest_center: zd !== null && zg !== null && zd > 0 && zg >= zd ? {
      zd, zg, start_time: textOrNull(center.start_time), end_time: textOrNull(center.end_time),
    } : null,
    fresh_signals: signals(chan.fresh_signals), events: signals(tf.events),
  };
}

export function compactDataSource(value: unknown): DataSource {
  const report = asRecord(value);
  return {
    mode: textOrNull(report.mode) ?? "", analyzed_at: textOrNull(report.analyzed_at),
    scanned_at: textOrNull(report.scanned_at), market_context: recordOrNull(report.market_context),
    delivery: recordOrNull(report.delivery),
    results: Array.isArray(report.results) ? report.results.map((item) => {
      const result = asRecord(item);
      return {
        symbol: textOrNull(result.symbol) ?? "", name: textOrNull(result.name) ?? "",
        status: textOrNull(result.status), analyzed_at: textOrNull(result.analyzed_at),
        timeframes: Object.entries(asRecord(result.timeframes)).map(([key, tf]) => compactTimeframe(key, tf)),
      };
    }) : [],
    candidates: Array.isArray(report.candidates) ? report.candidates.slice(0, 100).map(asRecord) : [],
    errors: Array.isArray(report.errors) ? report.errors.slice(0, 50).map(asRecord) : [],
  };
}

export const TIMEFRAMES = ["1y", "1M", "1w", "1d", "120m", "60m", "30m", "15m", "5m", "1m"];
export function timeframeLabel(value: string): string {
  return ({ "1y": "年线", "1M": "月线", "1w": "周线", "1d": "日线", "120m": "120分", "60m": "60分", "30m": "30分", "15m": "15分", "5m": "5分", "1m": "1分" } as Record<string, string>)[value] ?? value;
}
export function orderedTimeframes(tfs: DataTimeframe[]): DataTimeframe[] {
  const rank = (value: string) => TIMEFRAMES.includes(value) ? TIMEFRAMES.indexOf(value) : TIMEFRAMES.length;
  return [...tfs].sort((a, b) => rank(a.timeframe) - rank(b.timeframe));
}
export function primaryTimeframe(result: DataResult): DataTimeframe | undefined {
  return result.timeframes.find((tf) => tf.timeframe === "1d")
    ?? orderedTimeframes(result.timeframes).find((tf) => tf.status === "ok")
    ?? result.timeframes[0];
}
export function formatPrice(value: unknown, digits = 2): string {
  const num = finiteNumber(value);
  return num === null ? "—" : num.toFixed(digits);
}
export function formatRatio(value: unknown): string {
  const num = finiteNumber(value);
  return num === null ? "—" : `${num > 0 ? "+" : ""}${(num * 100).toFixed(2)}%`;
}
/** Engine's timezone-naive ISO values are Asia/Shanghai, not browser local time. */
export function timestamp(value: string | null): number | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}(?:[T ]|$)/.test(value)) return null;
  const iso = value.replace(" ", "T");
  const zoned = iso.length === 10 ? `${iso}T00:00:00+08:00` : /(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : `${iso}+08:00`;
  const time = Date.parse(zoned);
  return Number.isFinite(time) ? time : null;
}
export function formatTime(value: string | null): string {
  const time = timestamp(value);
  if (time === null) return "时间未提供";
  return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(time);
}
export function priceChange(tf: DataTimeframe): number | null {
  // Never bridge missing bars or compare a stale last bar with a newer quote.
  const last = tf.bars.at(-1);
  const previous = tf.bars.at(-2);
  if (!last || !previous || tf.latest_price === null || last.close !== tf.latest_price) return null;
  const lastTime = timestamp(last.datetime);
  const quoteTime = timestamp(tf.latest_time);
  if (lastTime === null || quoteTime === null || lastTime !== quoteTime) return null;
  if (last.close === null || previous.close === null || previous.close <= 0) return null;
  return last.close / previous.close - 1;
}
export function structureLabel(tf: DataTimeframe): string {
  if (tf.status !== "ok") return "数据不足";
  if (!tf.latest_center || tf.latest_price === null) return "未提供中枢";
  return tf.latest_price > tf.latest_center.zg ? "中枢上方" : tf.latest_price < tf.latest_center.zd ? "中枢下方" : "中枢区间内";
}
export function confirmationLabel(tf: DataTimeframe | undefined): string {
  if (!tf || tf.status !== "ok") return "数据未提供";
  const i = tf.indicators;
  if (i.golden_cross_entry_ready === true) return "本根确认就绪";
  // Confirmation remains recorded after the one-bar entry-ready pulse ends.
  if (i.golden_cross_state === "confirmed_pullback") return "此前已确认";
  const labels: Record<string, string> = { pending_pullback: "等待回踩确认", invalidated: "结构已失效", expired: "确认窗口已过期", none: "无金叉记录" };
  return typeof i.golden_cross_state === "string" ? labels[i.golden_cross_state] ?? "状态未识别" : "数据未提供";
}
export type RiskState = "triggered" | "clear" | "unavailable";
export function positionRisk(tf: DataTimeframe | undefined): RiskState {
  if (!tf || tf.status !== "ok" || tf.timeframe !== "1d") return "unavailable";
  const i = tf.indicators;
  if (i.high_position_risk === true || i.high_volume_risk === true) return "triggered";
  // The producer defaults risk booleans to false when the long MA is missing.
  if (i.high_position_risk !== false || i.high_volume_risk !== false || i.history_complete === false ||
    finiteNumber(i.ma_long) === null || Number(i.ma_long) <= 0 || finiteNumber(i.volume_ratio) === null ||
    finiteNumber(i.distance_to_ma_long) === null || finiteNumber(i.recent_return) === null) return "unavailable";
  return "clear";
}
export function signalLabel(type: string): string {
  const labels: Record<string, string> = {
    buy_1: "一类买点", buy_2: "二类买点", buy_3: "三类买点", sell_1: "一类卖点", sell_2: "二类卖点", sell_3: "三类卖点",
    macd_golden_cross_detected_above: "轴上金叉 · 待回踩确认", macd_golden_cross_detected_near: "近轴金叉 · 待回踩确认",
    macd_golden_cross_pullback_confirmed_above: "轴上金叉回踩确认", macd_golden_cross_pullback_confirmed_near: "近轴金叉回踩确认",
    zero_axis_death_cross: "零轴附近死叉",
  };
  return labels[type] ?? type;
}
export function executionLabel(signal: ReportSignal): string {
  if (signal.execution_mode === "disabled") return "策略禁用";
  if (signal.execution_mode === "observe_only" || signal.actionable === false) return "仅观察";
  if (signal.execution_mode === "enabled" && signal.actionable === true) return "策略启用";
  return "执行状态未提供";
}
export function timeframeSignals(tf: DataTimeframe): ReportSignal[] {
  const unique = new Map<string, ReportSignal>();
  // Event evidence takes precedence over raw structure annotations.
  for (const signal of [...tf.fresh_signals, ...tf.events]) {
    unique.set(`${signal.signal_type}|${signal.side}|${signal.confirmed_at}`, signal);
  }
  return [...unique.values()].sort((a, b) => (timestamp(b.confirmed_at) ?? 0) - (timestamp(a.confirmed_at) ?? 0));
}
