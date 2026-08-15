# 缠论信号监控 Web 控制台 - 实施计划

## 阶段 0 脚手架
- create-next-app@latest 生成 quant-python/web（TS、Tailwind、App Router、ESLint）。
- shadcn init（优先 Base UI 适配；不可用时回退默认 Radix 并记录）；Lucide 图标。
- 基础布局：左侧导航 + 六个页面壳（结果/策略/推送/模型/定时/股票池）。
- 验证：npm run dev 可启动，首页可访问，lint 通过。

## 阶段 1 数据层
- web/src/lib/db.ts：better-sqlite3 初始化 + 迁移（上述表）。
- 种子：从 signal_system/config/config.yaml 提取默认值写入 settings。
- 验证：vitest 单测（迁移幂等、CRUD、点路径读写）。

## 阶段 2 Python 桥接
- signal_system/web_bridge.py：config/normalize/analyze/scan/monitor-once/test-notify/outbox-status/calendar。
- signal_system/tests/test_web_bridge.py：脱敏、normalize、calendar、参数校验、错误码。
- 验证：python -m unittest discover -s tests -q（全量用例保持通过）。

## 阶段 3 配置 API + 设置页
- API：GET/PUT /api/config/{section}（strategies/notification/models/schedule）。
- 优先级合并、脱敏回显、类型校验。
- UI：策略配置、推送配置、模型配置、定时配置四页。
- 验证：vitest 合并优先级；浏览器保存后经桥接 config 可见生效。

## 阶段 4 LLM 客户端
- web/src/lib/llm.ts：chat 解读、图片识别、连通性测试。
- API：POST /api/models/{id}/test。
- 验证：vitest mock fetch；有 key 时真实调用手测。

## 阶段 5 结果与任务
- API：GET /api/results、/api/results/{id}、/api/jobs、POST /api/run（analyze/scan/monitor-once/test-notify）。
- 后台执行：桥接 spawn + jobs 状态流转。
- UI：结果列表 + 详情（含 AI 解读）、任务页。
- 验证：mock 桥接集成测试 + 一次真实 analyze 手测。

## 阶段 6 定时调度
- web/src/lib/scheduler.ts：node-cron 注册/重建，trading_days_only 调 calendar。
- API：GET/PUT /api/schedule。
- UI：定时配置页（启停、时刻、间隔、交易日限制、最近/下次运行）。
- 验证：设置 30 秒间隔实测触发并可见。

## 阶段 7 股票池
- API：/api/pool CRUD、/api/pool/import/text、/api/pool/import/image、/api/pool/import/confirm。
- 文本导入调桥接 normalize 解析；图片上传 -> LLM 识别 -> pending -> 确认入库。
- UI：股票池表格 + 导入对话框 + 待确认列表。
- 验证：vitest 解析；有模型时图片导入手测。

## 阶段 8 收尾
- README（运行方式、配置优先级、密钥安全、桥接命令）。
- 全量测试、独立代码评审、修复、更新 verification.md。

## 兼容性
- 不修改现有 CLI 入口与引擎模块；如需小改，必须单独说明并跑全量回归。
- config.yaml 保持原样作为默认基线，Web 设置存 app.db 不覆盖 YAML。
