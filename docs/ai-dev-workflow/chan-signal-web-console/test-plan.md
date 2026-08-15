# 测试计划

## 单元测试（Python）
- test_web_bridge.py：
  - config 命令返回脱敏密钥、合并 overrides 生效；
  - normalize 解析混合分隔符与带名称行；
  - calendar 返回交易日/交易时段判断结构；
  - 参数错误返回 exit 2，业务失败返回 exit 1；
  - 未知命令返回 exit 2。
- 回归：现有 signal_system 全量单测保持通过。

## 单元测试（Node，vitest）
- db：迁移幂等、settings 点路径读写、CRUD。
- config merge：环境变量 > DB > YAML 优先级；脱敏回显。
- llm：mock fetch 的解读/图片识别/失败重试/超时。
- pool：文本解析、确认入库、重复 symbol 处理。
- scheduler：schedule 表到 cron 表达式映射、交易日过滤逻辑。

## 集成测试
- 桥接真实调用（config/normalize/calendar/outbox-status，不依赖网络）。
- /api/run 使用 mock 桥接进程，验证 jobs 状态流转 pending -> running -> success/failed。
- 图片导入：mock LLM 返回候选，确认后入库。

## 手工 UAT（见 uat-cases.md）
- 浏览器完整走查六页 + 文本/图片导入 + 定时触发 + 手动触发。

## 验证命令
- python -m unittest discover -s quant-python/signal_system/tests -q
- cd quant-python/web && npm run lint && npm test && npm run build
