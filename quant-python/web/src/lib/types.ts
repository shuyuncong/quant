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
  id: number;
  name: string;
  base_url: string;
  model: string;
  api_key: string;
  env_key: string;
  proxy: string;
  enabled: boolean;
  vision_supported: boolean;
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
  kind: "daily_scan" | "monitor_cycle";
  time: string;
  interval_seconds: number;
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
  created_at: string;
}
