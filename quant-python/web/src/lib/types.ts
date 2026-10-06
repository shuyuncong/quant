export interface JobRow {
  id: number;
  kind: string;
  status: "pending" | "running" | "success" | "failed";
  payload: string;
  result_path: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ModelProfile {
  /** Older profiles default to Chat Completions. */
  protocol?: import("./model-protocol").ModelProtocol;
  reasoning_effort?: import("./model-protocol").ReasoningEffort;
  id: number;
  name: string;
  base_url: string;
  model: string;
  api_key: string;
  env_key: string;
  proxy: string;
  enabled: boolean;
  vision_supported: boolean;
  priority: number;
  created_at: string;
  updated_at: string;
}

export interface PoolRow {
  symbol: string;
  name: string;
  source: string;
  created_at: string;
}

export interface PendingImport {
  id: number;
  kind: "text" | "image";
  raw: string;
  candidates: string;
  status: "pending" | "confirmed" | "cancelled";
  created_at: string;
}

export interface ScheduleRow {
  id: number;
  kind: "daily_scan" | "monitor_cycle" | "monitor_fixed";
  time: string;
  interval_seconds: number;
  fixed_times: string[];
  scope?: import("./analysis-types").AnalysisScope;
  trading_days_only: boolean;
  enabled: boolean;
  updated_at: string;
}

export interface AnalysisNote {
  id: number;
  job_id: number | null;
  symbol: string;
  content: string;
  model: string;
  result_path: string | null;
  created_at: string;
}

export interface OperationLog {
  id: number;
  job_id: number | null;
  level: "info" | "warning" | "error";
  module: string;
  message: string;
  detail: string | null;
  created_at: string;
}

export interface HoldingRow {
  symbol: string;
  name: string;
  shares: number;
  cost_price: number;
  total_amount: number;
  created_at: string;
  updated_at: string;
}

/** 一笔已登记成交（quant.holding_trades 的一行；portfolioSnapshot 每股最多取最近 50 笔）。 */
export interface HoldingTradeRow {
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  fees: number;
  amount: number;
  realized_pnl: number;
  traded_at: string;
}

/** 持仓/成交上下文：把"没有数据"和"确实是零"分开，避免模型把 0 当成事实。 */
export interface PortfolioContextInput {
  holdings: HoldingRow[];
  /** null / 省略 = 本次没附带成交记录（不是"一笔没成交"）；[] = 确实没登记过成交。 */
  trades?: HoldingTradeRow[] | null;
  totalCapital?: number;
  historyComplete?: boolean;
}
