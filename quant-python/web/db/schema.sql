CREATE SCHEMA IF NOT EXISTS quant;

CREATE TABLE IF NOT EXISTS quant.schema_meta (
  version INTEGER PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS quant.settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quant.model_profiles (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  base_url TEXT NOT NULL,
  model TEXT NOT NULL,
  api_key TEXT NOT NULL DEFAULT '',
  env_key TEXT NOT NULL DEFAULT '',
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  vision_supported BOOLEAN NOT NULL DEFAULT TRUE,
  priority INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  proxy TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS quant.stock_pool (
  symbol TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quant.pending_imports (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,
  raw TEXT NOT NULL DEFAULT '',
  candidates TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quant.jobs (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  payload TEXT NOT NULL DEFAULT '{}',
  result_path TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS quant.analysis_notes (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT,
  symbol TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  result_path TEXT
);

CREATE TABLE IF NOT EXISTS quant.schedule (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL UNIQUE,
  time TEXT NOT NULL DEFAULT '15:20',
  interval_seconds INTEGER NOT NULL DEFAULT 60,
  trading_days_only BOOLEAN NOT NULL DEFAULT TRUE,
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TEXT NOT NULL,
  fixed_times TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS quant.operation_logs (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT,
  level TEXT NOT NULL DEFAULT 'info',
  module TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operation_logs_created
  ON quant.operation_logs(created_at);

CREATE TABLE IF NOT EXISTS quant.holdings (
  symbol TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  shares DOUBLE PRECISION NOT NULL DEFAULT 0,
  cost_price DOUBLE PRECISION NOT NULL DEFAULT 0,
  total_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

INSERT INTO quant.schema_meta (version) VALUES (1)
ON CONFLICT (version) DO NOTHING;

REVOKE ALL ON SCHEMA quant FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA quant FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA quant FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA quant REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA quant REVOKE ALL ON SEQUENCES FROM PUBLIC;

DO $permissions$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON SCHEMA quant FROM anon;
    REVOKE ALL ON ALL TABLES IN SCHEMA quant FROM anon;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA quant FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON SCHEMA quant FROM authenticated;
    REVOKE ALL ON ALL TABLES IN SCHEMA quant FROM authenticated;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA quant FROM authenticated;
  END IF;
END
$permissions$;

-- v2: self-hosted PostgreSQL 角色授权块（切换 commit）
-- 角色本体由 db:setup 的 QUANT_SETUP_ROLES=1 通道创建（带密码、幂等）；
-- 本块只做授权，且仅在角色已存在时生效，避免未初始化环境 DDL 失败。
INSERT INTO quant.schema_meta (version) VALUES (2)
ON CONFLICT (version) DO NOTHING;

DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quant_app') THEN
    GRANT USAGE ON SCHEMA quant TO quant_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA quant TO quant_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA quant TO quant_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA quant
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quant_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA quant
      GRANT USAGE, SELECT ON SEQUENCES TO quant_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quant_backup') THEN
    GRANT USAGE ON SCHEMA quant TO quant_backup;
    GRANT SELECT ON ALL TABLES IN SCHEMA quant TO quant_backup;
    -- pg_dump 需读取序列 last_value；缺此权限时备份报
    -- "permission denied for sequence xxx_id_seq"。
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA quant TO quant_backup;
    ALTER DEFAULT PRIVILEGES IN SCHEMA quant
      GRANT SELECT ON TABLES TO quant_backup;
    ALTER DEFAULT PRIVILEGES IN SCHEMA quant
      GRANT USAGE, SELECT ON SEQUENCES TO quant_backup;
  END IF;
END
$grants$;

-- v3: 模型使用排序 —— 已启用模型按 priority 升序组成 AI 解读/图片识别的
-- 降级链：优先用排序靠前的模型，调用失败自动切换下一个。
-- 既有库通过幂等 ALTER 补齐列；新库直接建表已含 priority。
ALTER TABLE quant.model_profiles ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0;
INSERT INTO quant.schema_meta (version) VALUES (3)
ON CONFLICT (version) DO NOTHING;

-- v4: reproducible per-stock analysis and append-only manual trade journal.
ALTER TABLE quant.holdings ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0;
ALTER TABLE quant.holdings ALTER COLUMN cost_price TYPE NUMERIC(24,8) USING cost_price::numeric;
ALTER TABLE quant.holdings ALTER COLUMN total_amount TYPE NUMERIC(24,8) USING total_amount::numeric;
CREATE TABLE IF NOT EXISTS quant.holding_events (
  id BIGSERIAL PRIMARY KEY,
  symbol TEXT NOT NULL,
  kind TEXT NOT NULL,
  before_state JSONB NOT NULL,
  after_state JSONB NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quant.holding_trades (
  id BIGSERIAL PRIMARY KEY,
  request_key TEXT NOT NULL UNIQUE,
  request_hash TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
  quantity BIGINT NOT NULL CHECK (quantity > 0),
  price NUMERIC(24,8) NOT NULL CHECK (price > 0),
  fees NUMERIC(24,8) NOT NULL CHECK (fees >= 0),
  amount NUMERIC(24,8) NOT NULL,
  cash_amount NUMERIC(24,8) NOT NULL,
  realized_pnl NUMERIC(24,8) NOT NULL,
  traded_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  holding_version BIGINT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_holding_trades_symbol_time ON quant.holding_trades(symbol, traded_at DESC);
CREATE TABLE IF NOT EXISTS quant.stock_analyses (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT NOT NULL REFERENCES quant.jobs(id),
  symbol TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  document JSONB NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(job_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_stock_analyses_recent ON quant.stock_analyses(id DESC);
CREATE TABLE IF NOT EXISTS quant.schedule_runs (
  run_key TEXT PRIMARY KEY,
  schedule_kind TEXT NOT NULL,
  scheduled_at TEXT NOT NULL,
  job_id BIGINT REFERENCES quant.jobs(id),
  status TEXT NOT NULL DEFAULT 'pending',
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
ALTER TABLE quant.schedule ADD COLUMN IF NOT EXISTS scope JSONB NOT NULL DEFAULT '{"holdings":true,"watchlist":true,"pools":[],"symbols":[]}';
DO $schedule_v4$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM quant.schema_meta WHERE version=4) THEN
    INSERT INTO quant.schedule(kind,time,interval_seconds,fixed_times,trading_days_only,enabled,updated_at,scope)
      SELECT 'monitor_fixed',time,interval_seconds,fixed_times,trading_days_only,enabled,updated_at,scope
      FROM quant.schedule WHERE kind='monitor_cycle' AND fixed_times <> '[]'
      ON CONFLICT(kind) DO NOTHING;
    UPDATE quant.schedule SET enabled=FALSE,fixed_times='[]' WHERE kind='monitor_cycle' AND fixed_times <> '[]';
  END IF;
END
$schedule_v4$;
INSERT INTO quant.schema_meta (version) VALUES (4) ON CONFLICT (version) DO NOTHING;
