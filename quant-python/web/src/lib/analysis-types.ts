export const STRATEGIES = [
  { id: "macd_zero_axis", name: "日线零轴金叉" },
  { id: "yearline_pullback", name: "年线趋势" },
  { id: "macd_divergence", name: "零轴＋底背离" },
] as const;
export type StrategyId = typeof STRATEGIES[number]["id"];

/** 持仓归属策略：空串 = 未归属（走全局止损/止盈口径），绝不猜归属。 */
export type HoldingStrategyId = StrategyId | "";

export function isStrategyId(value: unknown): value is StrategyId {
  return typeof value === "string" && STRATEGIES.some(strategy => strategy.id === value);
}

/**
 * 把外部传入的归属策略收敛成 "" 或合法策略 id。
 *
 * 空/null/undefined 一律读作"未归属"；未知 id 直接抛错，避免拼错策略名后
 * 静默退化成全局口径——那会让用户以为按策略计划已生效。
 */
export function normalizeHoldingStrategyId(value: unknown): HoldingStrategyId {
  if (value == null || value === "") return "";
  const text = String(value).trim();
  if (text === "") return "";
  if (!isStrategyId(text)) throw new Error(`未知的归属策略：${text}`);
  return text;
}

/** 分批减仓的执行建议：由 Python 引擎按档位、已执行进度与当日日线算出，前端与 AI 只负责展示。 */
export interface ExitPlanAdvice {
  symbol?: string;
  strategy_id?: string;
  cost_price?: number;
  shares?: number;
  original_shares?: number;
  basis?: string;
  taken: number;
  pending: number;
  triggered_today: boolean;
  breakeven_active: boolean;
  breakeven_stop_price: number | null;
  last_high: number | null;
  last_close: number | null;
  summary: string;
  levels: Array<{ index: number; gain: number; fraction: number; trigger_price: number; quantity: number; taken: boolean }>;
  next: { index: number; gain: number; fraction: number; trigger_price: number; quantity: number; taken: boolean } | null;
  trailing?: {
    activation_gain: number;
    pct: number;
    peak_close: number | null;
    peak_basis: string;
    activation_price: number;
    floor_price?: number;
    armed?: boolean;
    triggered_today?: boolean;
  };
}

export interface StrategyCondition {
  name: string;
  actual: string | number | boolean | null;
  expected: string;
  met: boolean | null;
  /** 分批/移动止盈这类没有单一数值的条件会带上引擎算好的执行明细。 */
  details?: ExitPlanAdvice | Record<string, unknown> | null;
  /** met 的判定依据（例如"日线最高价"），用于说明为什么这样算触发。 */
  trigger_basis?: string;
}
export interface StrategyResult {
  strategy_id: StrategyId;
  name: string;
  version: string;
  status: "ok" | "insufficient_data" | "error" | "disabled";
  as_of: string | null;
  buy: boolean;
  sell: boolean | null;
  buy_conditions: StrategyCondition[];
  sell_conditions: StrategyCondition[];
  reference_price: number | null;
  exit_rule: string;
  /** 分批减仓的可执行建议（档位触发价 / 建议股数 / 保本止损位）；未配置计划时为 null。 */
  exit_plan_advice?: ExitPlanAdvice | null;
  stop_loss?: {
    pct: number;
    source: "strategy" | "global" | "default" | "plan_breakeven";
    strategy_id: StrategyId | null;
  };
  warnings: string[];
  parameters: Record<string, unknown>;
}
export interface AnalysisStage {
  status: "pending" | "running" | "success" | "failed";
  content: string;
  model?: string;
  prompt_version?: string;
  error?: string;
  elapsed_ms?: number;
  completed_at?: string;
}
export interface AnalysisDocument {
  schema_version: 2;
  symbol: string;
  name: string;
  as_of: string;
  snapshot_hash: string;
  report: Record<string, unknown>;
  portfolio: Record<string, unknown>;
  strategies: StrategyResult[];
  technical: AnalysisStage;
  synthesis: AnalysisStage;
  revision: number;
}
export interface AnalysisRecord {
  id: number;
  job_id: number;
  symbol: string;
  name: string;
  status: string;
  created_at: string;
  updated_at: string;
  document: AnalysisDocument;
}
export interface AnalysisScope {
  holdings: boolean;
  watchlist: boolean;
  pools: StrategyId[];
  symbols: string[];
}
export const DEFAULT_ANALYSIS_SCOPE: AnalysisScope = {
  holdings: true, watchlist: true, pools: [], symbols: [],
};
