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
