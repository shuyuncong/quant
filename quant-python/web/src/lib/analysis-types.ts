export const STRATEGIES = [
  { id: "macd_zero_axis", name: "日线零轴金叉" },
  { id: "yearline_pullback", name: "年线趋势" },
  { id: "macd_divergence", name: "零轴＋底背离" },
] as const;

/** 个股分析记录列表每页条数；服务端分页，前后端共用同一个值。 */
export const ANALYSIS_PAGE_SIZE = 10;
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
/**
 * 只读的市场环境 → 策略适配提示。后端在个股分析报告里生成；
 * 它不改变任何策略信号，也不能当作交易指令。
 */
export interface RegimeGuidance {
  version: string;
  regime: "bull" | "range" | "bear" | "unknown" | string;
  regime_label: string;
  primary_strategy: StrategyId | null;
  primary_name: string;
  secondary_strategy?: StrategyId | null;
  secondary_name?: string | null;
  avoid_strategies?: StrategyId[];
  avoid_names?: string[];
  confidence?: "low" | "medium" | "high" | "none" | string;
  summary: string;
  evidence?: string[];
  caveats?: string[];
  source?: string;
  read_only?: boolean;
  disclaimer?: string;
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
  /** 可选：旧记录没有这个字段，前端必须容忍缺失。 */
  regime_guidance?: RegimeGuidance;
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

/**
 * 列表摘要：报告正文（五页签）之外的一切都返回，唯独不带 `report` / `portfolio`。
 *
 * 这两个字段是载荷的主体：单条实测 ~129KB，其中 `timeframes`（1m/5m/15m/30m/60m/120m）
 * 占 118KB，而列表只用到 `strategies`（一致度徽标）与各阶段状态。
 * 列表按 10 条翻页时若带上它们，每页要传 ~1.2–2.4MB，且每 10 分钟静默轮询都会重传一遍——
 * 这是"翻页点了像没反应"的主要来源（服务器带宽被占满时首字节要等整包传完）。
 *
 * 五页签正文只读 `strategies` / `technical` / `synthesis` 与标头字段，
 * 因此本类型即可满足渲染，无需再取回 `report` / `portfolio`。
 */
export type AnalysisDocumentView = Omit<AnalysisDocument, "report" | "portfolio">;

export type AnalysisRecordSummary = Omit<AnalysisRecord, "document"> & {
  document: AnalysisDocumentView;
  /**
   * 分析时点该股票是否有持仓（记录内 `portfolio.holdings` 是否非空）。
   * null = 记录没有持仓快照，无法判断；不能当成空仓。
   */
  held: boolean | null;
};
export interface AnalysisScope {
  holdings: boolean;
  watchlist: boolean;
  pools: StrategyId[];
  symbols: string[];
}
export const DEFAULT_ANALYSIS_SCOPE: AnalysisScope = {
  holdings: true, watchlist: true, pools: [], symbols: [],
};
