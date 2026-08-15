# 缠论信号监控 Web 控制台 - 总体设计

## 目标与非目标

目标见 requirements.md。非目标（v1 不做）：交易下单、多租户、公网部署、复杂权限体系。

## 架构

浏览器 <-> Next.js 16（App Router / React 19 / TS / Tailwind 4 / shadcn-ui + Base UI / Lucide）
  |-- API Routes：配置、结果、股票池、模型、任务、定时
  |-- node-cron 进程内调度器
  |-- better-sqlite3：data/app.db（设置、模型、股票池、任务、AI 备注、定时）
  +-- Python 桥接（subprocess 一次性进程）
        +-- signal_system/web_bridge.py
              +-- 复用 SignalMonitor / load_config / SignalNotifier / SignalStore

- Web 与 Python 之间不直接 import，通过桥接命令 + JSON 交换。
- 分析/扫描等耗时操作后台执行，写入 jobs 表，前端轮询状态。

## 组件边界

- quant-python/web：Next.js 应用（页面 + API + lib）。
- quant-python/signal_system/web_bridge.py：Python 命令桥，保持引擎只读复用。
- 数据文件：
  - web/data/app.db：Web 设置、模型、股票池、任务、AI 备注、定时；
  - signal_system/data/signal_monitor.db：信号事件、outbox、状态（现有引擎持有）；
  - signal_system/output/*.json：分析/扫描结果（现有引擎写入）。

## 配置与优先级

- 合并规则：effective = deep_merge(load_config(yaml 含 env 展开), db_overrides)。
- 密钥类字段可配置 env_key；运行时若环境变量存在，则以环境变量覆盖 db_overrides，实现 环境变量 > Web 库 > YAML。
- 设置表 key 使用点路径（如 monitor.daily_scan_time），避免字段漂移。
- 配置回显时密钥一律脱敏（****），并提供“已从环境变量加载”标识。

## 数据契约（web/data/app.db）

- settings(key TEXT PK, value TEXT, updated_at)
- model_profiles(id PK, name, base_url, model, api_key, env_key, enabled, created_at, updated_at)
- stock_pool(symbol PK, name, source, created_at)
- pending_imports(id PK, kind text|image, raw, candidates JSON, status pending|confirmed|cancelled, created_at)
- jobs(id PK, kind, status pending|running|success|failed, payload JSON, result_path, error, created_at, started_at, finished_at)
- analysis_notes(id PK, job_id, symbol, content, model, created_at)
- schedule(id PK, kind daily_scan|monitor_cycle, time, interval_seconds, trading_days_only, enabled, updated_at)

## 桥接命令契约（stdout JSON，exit code 0 成功 / 1 业务失败 / 2 参数错误）

- config --payload overrides -> 合并后的有效配置（密钥脱敏）
- normalize --payload text -> {symbols:[{symbol,name}], raw_lines, unknown}
- analyze --payload {symbols, overrides} -> 报告 dict + result_path
- scan --payload {overrides} -> 报告 dict + result_path
- monitor-once --payload {overrides} -> 报告 dict + result_path
- test-notify --payload {overrides} -> {channel: {success, detail}}
- outbox-status -> outbox 摘要
- calendar -> {is_trading_day, is_trading_session, now}

## LLM 集成

- OpenAI 兼容 chat completions：POST {base_url}/chat/completions，Authorization: Bearer <key>。
- 文本解读：输入报告摘要与系统提示，输出 markdown，存 analysis_notes。
- 图片识别：messages 含 image_url（data URL），要求模型返回严格 JSON 候选列表，页面确认后入库。
- 超时 60s、失败重试 1 次；错误记录到 job.error。

## 调度

- node-cron 按 Asia/Shanghai 时区依据 schedule 表触发；trading_days_only 时先调桥接 calendar 判断交易日。
- 触发后写 jobs 行，后台执行桥接命令，页面轮询状态。

### 定时推送：当前实现 vs Web 方案

现状（CLI）：`python main.py monitor` 为常驻进程，`run_forever` 死循环完成全部定时逻辑：

- 每日扫描：交易日在 `monitor.daily_scan_time`（当前 15:20）执行一次 `scan_zero_axis`，同日只跑一次（run_state.last_daily_scan 去重）；
- 盘中监控：交易时段（9:30-11:30、13:00-15:00）每 `monitor.interval_seconds`（60 秒）执行一次 `run_monitor_cycle`；
- 推送：信号经 outbox 队列由 SignalNotifier 投递，非交易时段也持续补投失败重试；
- Windows 通过 scripts/run_monitor.ps1 启动（计划任务或开机自启）。

Web 方案：调度移入 Web 进程（Node），schedule 表两行：daily_scan（时刻）与 monitor_cycle（间隔秒数）。
实现采用 15 秒 tick 轮询而非 cron 表达式，以支持小于 60 秒的监控间隔；trading_days_only 复用桥接 calendar
判断交易日/交易时段。触发后统一写 jobs 表，桥接执行分析与推送，页面可见下次/最近运行状态。
调度默认 enabled=false（避免与 CLI monitor 双跑造成重复推送；outbox 以 event_id 去重，风险可控），README 说明二选一。

## 错误处理与安全

- 桥接非零退出：stderr 写入 job.error，API 返回结构化错误。
- 配置写入先做类型校验，失败不落库。
- v1 本地绑定 127.0.0.1；鉴权作为中间件预留点，上线前必须启用密码。
- 密钥 DB 明文存储风险在 README 标注；推荐密钥走环境变量。
- 桥接 payload 通过 stdin 传递（`--payload -`），避免密钥出现在命令行/进程列表；
  密钥优先级 环境变量 > Web 库 > YAML：Node 侧检测到对应环境变量时以 `__env__` 标记占位，桥接进程内取环境变量值。

## 验收标准

六类页面可用；文本/图片导入闭环；定时触发可见；配置优先级正确；CLI 行为不回归。

## 评审确认的关键决策（2026-08-15）

1. LLM AI 分析：支持 1~N 个 OpenAI 兼容接口（名称/base_url/model/key，key 可绑定环境变量名），AI 解读为结果/个股报告的补充字段；未配置模型时系统照常运行。
2. 图片导入：优先调用已配置的视觉模型识别，识别结果先入 pending 列表，页面确认后再入库；未配置模型时明确提示改用文本导入。
3. 技术栈：Next.js 16 App Router + React 19 + TypeScript + Tailwind CSS 4 + shadcn/ui（优先 Base UI 适配，不可用则回退 Radix 并记录）+ Lucide。
4. 定时配置：Web 页面可改每日扫描时刻与盘中监控间隔等；实现见上文“调度”。
5. 密钥配置：可存 Web 库也可配环境变量，环境变量存在时优先；上线前必须启用访问密码，v1 预留鉴权中间件扩展点。
