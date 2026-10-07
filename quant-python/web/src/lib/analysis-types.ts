export const STRATEGIES = [
  { id: "macd_zero_axis", name: "日线零轴金叉" },
  { id: "yearline_pullback", name: "年线趋势" },
  { id: "macd_divergence", name: "零轴＋底背离" },
] as const;
export type StrategyId = typeof STRATEGIES[number]["id"];
export interface StrategyCondition {
  name: string;
  actual: string | number | boolean | null;
  expected: string;
  met: boolean | null;
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
  stop_loss?: {
    pct: number;
    source: "strategy" | "global" | "default";
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
