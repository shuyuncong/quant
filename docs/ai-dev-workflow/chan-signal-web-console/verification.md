# 验证记录

## 已执行（2026-08-15）

### Python（signal_system）
- `python -m unittest discover -s tests -q`：36 项全部通过（含 test_web_bridge 10 项）。
- 桥接冒烟：config（覆盖/脱敏/env 优先标记）、normalize（混合文本/未知行）、calendar、outbox-status、未知命令 exit 2。

### Node（web）
- `npm test`：20 项全部通过（config 优先级、symbols、llm mock、scheduler）。
- `npm run lint`：通过。
- `npm run build`：通过（Next.js 16.3.1 生产构建）。

### 端到端（dev server, 127.0.0.1:3111）
- /api/config/strategies、/api/results、/api/schedule、/api/pool、/api/jobs、/api/models 均返回正常。
- POST /api/run analyze(000001.SZ) -> job 成功 -> output/analysis_*.json 生成。
- 文本导入 -> pending -> confirm -> 股票池入库闭环通过。

## 待人工 UAT
- 浏览器走查 uat-cases.md（UC-1 ~ UC-8），其中图片导入需配置视觉模型。
