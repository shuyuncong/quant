# 五条策略改进：统一回测与模型分工

这份说明是给其他模型/审查者的固定操作手册。主模型负责写入研究代码和预注册规则；其他模型只运行下面的同一套命令、核对哈希和原始成交，并报告可复现的数字。不要在聊天里自行计算收益、投票或修改阈值。

## 数据边界

- 只使用本地缓存和 `D:\tmp` 研究文件，不连接生产 PostgreSQL，不写业务表。
- `train` 用于开发，`val` 用于模型选择，`test` 是已查看测试集，只做一次最终开发评估。
- 盲测 Holdout 不属于开发数据。所有命令默认拒绝路径名包含 `holdout` 的输入；没有单独授权时不要加 `--allow-holdout`。
- 每个报告都记录输入文件 SHA256、候选 ID 清单 SHA256、历史缓存 SHA256、配置 SHA256 和随机种子 `20260830`。

## 分工

1. 主模型（本次）维护 `backtest_winrate.py`、`exit_experiment.py`、`exit_experiment_compare.py` 和 `strategy_improvement_experiment.py`，负责因果时序、成本、组合分配、统计口径和测试。
2. 其他模型 A 复跑退出保护实验并核对逐笔成交、退出原因和配对覆盖率。
3. 其他模型 B 复跑 `benchmark` 与 `regime`，重点审查指数日期对齐、风险闸门的因果性和组合资金曲线。
4. 其他模型 C 复跑 `value` 与 `meta`，重点审查 PIT 公告日、负 PE 分组、样本覆盖率、特征泄漏和 train/val/test 隔离。
5. 任一模型发现实现或数据问题，只提交证据（命令、哈希、行号、原始交易 ID），不要自行改变规则后继续比较。

## 0. 先跑测试

```powershell
cd D:\development\github\quant\quant-python\signal_system
python -m pytest tests/test_backtest_winrate.py tests/test_candidate_integrity.py tests/test_exit_experiment.py tests/test_strategy_improvement_experiment.py -q
python -m pytest -q
```

通过标准：全部通过；当前 v2 聚焦用例为 `58 passed`，完整测试为
`959 passed, 2 skipped, 11 subtests passed`。

## 1. 退出保护（MFE 锁盈 / ATR 跟踪）

以下命令先把固定入场候选规范为唯一 ID，再使用生产风险成本 SL8%/TP30% 运行。规范化只删除内容完全相同的重复行；同一 ID 内容冲突时直接失败。所有变体必须写入同一目录：

```powershell
cd D:\development\github\quant\quant-python\signal_system
$source = 'D:\tmp\candidates_exit'
$canonical = 'D:\tmp\candidates_exit_unique_v2'
$out = 'D:\tmp\strategy_improvement_exit_v2'

if (-not (Test-Path -LiteralPath $canonical)) {
  python candidate_integrity.py --input-dir $source --output-dir $canonical
}

python exit_experiment.py --variant baseline --execution-profile production_risk --input-dir $canonical --output-dir $out
python exit_experiment.py --variant mfe_profit_lock --execution-profile production_risk --input-dir $canonical --output-dir $out
python exit_experiment.py --variant atr_trailing --execution-profile production_risk --input-dir $canonical --output-dir $out

python exit_experiment_compare.py --root $out --variants mfe_profit_lock atr_trailing --output "$out\comparison.json"
```

v2 比较报告必须同时审查两种组合口径：

- `profiles`：完整动态候选池，允许提前退出后接纳替换交易。
- `baseline_entry_cohort`：只保留 baseline 实际接纳的入场候选，用于隔离纯退出和现金流效果。

`capital_release_attribution` 将总组合收益变化拆为固定入场集合的退出效果、资金释放/替换交易效果和 baseline 集合复现误差。还必须报告平均仓位、资金利用率、空仓比例、仓位占用 session、换手率、交易成本与 `max_positions` 拒绝数。这些指标仅用于机制诊断，不改变 v1 已冻结的有效性门槛。

固定参数：MFE 锁盈在浮盈 8% 后锁定入场价上方 2%；ATR 跟踪在浮盈 8% 后使用 ATR14×2.5，且止损不低于入场价。动态止损只使用前一根已收盘日线，不允许同日高点/低点偷看。

## 2. 基准、市场状态和估值/小盘过滤

```powershell
cd D:\development\github\quant\quant-python\signal_system
$exit = 'D:\tmp\strategy_improvement_exit_v2'
$out = 'D:\tmp\strategy_improvement_unified_v2'
python strategy_improvement_experiment.py benchmark --baseline-dir "$exit\baseline" --output-dir $out
python strategy_improvement_experiment.py regime --baseline-dir "$exit\baseline" --output-dir $out
python strategy_improvement_experiment.py value --baseline-dir "$exit\baseline" --output-dir $out
```

`benchmark` 输出策略收益、上证综指 `000001.SH` 收益、复合超额收益、年化收益/波动、Sharpe、最大回撤、Beta、年化 Alpha、跟踪误差和信息比率。无持仓日会按资金曲线前值对齐，起点增加前一指数交易日资金锚点。

`regime` 是唯一预注册的 `composite_risk_off_v1`：

- 触发：`close < MA20 且 5 日收益 < 0`，或 5 日收益 ≤ -3%，或 20 日下行波动率 ≥ 2.5%。
- 恢复：连续 3 个收盘价高于 MA10 且 5 日收益为正。
- 所有特征只用信号日收盘及更早数据；风险闸门只排除 signal-day 已处于 risk-off 的候选。

`value` 使用 `symbol|signal_day|signal_type` 精确连接 PIT 基本面；公告日必须不晚于信号日且不能是估算日期。PE>0 才计算 `earnings_yield=1/PE`，负/零 PE 单独统计为不可用/亏损组。每个信号日内按候选相对市值排除底 30%，再保留收益率上半部。报告会显示覆盖率和样本门槛；当前基本面文件很小，不能把候选相对排名描述成全市场底 30%。

## 3. ML meta-label

```powershell
cd D:\development\github\quant\quant-python\signal_system
python strategy_improvement_experiment.py meta --baseline-dir D:\tmp\strategy_improvement_exit_v2\baseline --output-dir D:\tmp\strategy_improvement_unified_v2
```

这是纯 NumPy、固定 L2=1、阈值 0.5 的逻辑回归基线，不增加生产依赖。标签为 `trade_pnl_pct > 0`。特征只来自信号日及以前的本地 QFQ 历史：5/20/60 日收益、20 日波动/下行波动、ATR14%、距 MA20/60/250、MA20 五日斜率、成交量比、市值对数、指数风险特征、信号类型和原始 regime one-hot。禁止使用 entry/exit/MFE/MAE/future/post-exit/pnl 等结果字段。

模型阶段固定为：`train -> val`；然后 `train+val -> viewed test`。不在 test 上调阈值或参数。报告同时给分类指标和“全部 eligible 候选”与“meta_probability≥0.5 子集”的逐笔/资金组合结果；如果筛选后样本过少，结果只能视为诊断，不能上线。

## 统一验收

其他模型提交时必须附：

- 命令原文、Python 版本、代码 git diff/hash；
- 退出比较报告和四个统一实验报告的 SHA256；
- 每个 split 的候选数、配对/覆盖数、胜率、平均交易收益、组合总收益、最大回撤；
- 原始成交 JSONL 中随机抽查的 candidate ID、entry/exit day、exit reason、quantity、pnl；
- 对任何“提升”的结论，说明是否同时满足 train/val、样本覆盖和未触碰 Holdout 的门槛。
- 确认每个 split 的 `candidate_id` 唯一，不允许比较器静默覆盖重复行。
- 对退出策略同时报告动态候选池、baseline 入场集合、资金释放归因和组合资金利用率。

只有在 `val` 预注册门槛通过、组合结果未恶化、实现审查无泄漏，并由独立冻结流程在全新 Holdout 上复核后，才可以讨论生产配置；本次研究命令不会修改生产 `config.yaml`。

## v2 当前参考哈希

同一代码和本地缓存快照下，预期结果为：

- `D:\tmp\candidates_exit_unique_v2\candidate_integrity_manifest.json`：`31164AF556BDE20EF6B1ECA54AD211812377CDE3D51A1B89451C02BB705C9248`
- `D:\tmp\strategy_improvement_exit_v2\comparison.json`：`08A2C3CF6732F9313442224B1636D59850C61C52D7051B1403F906E9D2BA3589`
- `benchmark/report.json`：`7E89FAA13CF240130AE745899A580DB33098C920CD978846741EACD8403E965A`
- `regime/report.json`：`75BA1DDCF198C20EF62537D930308BD2B0398ECF8EDB503C81B142BBDB66A2D7`
- `value/report.json`：`D765C2184CF8898BC7067554FCCFE363C5C92D951893C0E59EA7A6E3C63985B1`
- `meta/report.json`：`0E32CAAF76DABECC947E40964E11D259E1C0FF5207DB71A917064E05167BF17C`

报告哈希不同时先核对代码、候选文件、QFQ 历史、指数、配置和运行环境；不得把哈希差异直接解释为策略差异。

## v3：候选顺序与满仓敏感性实验

v2 已证明退出策略的动态组合改善大部分来自提前释放资金后接纳了替换候选，且
候选级有效性门槛仍然失败。下一步先审计组合结果是否依赖 `symbol_asc` 的偶然
顺序，不调整退出参数，也不使用 viewed-test 选择模型。

```powershell
cd D:\development\github\quant\quant-python\signal_system

python portfolio_order_sensitivity_experiment.py `
  --exit-root D:\tmp\strategy_improvement_exit_v2 `
  --output-dir D:\tmp\strategy_improvement_order_v3 `
  --random-seeds 200 `
  --seed-start 20260830
```

复核者必须确认：

- baseline、MFE、ATR 每个 split 使用相同的配对候选 ID 和相同随机种子；
- 排序输入只有入场日、P0 信号优先级及预先声明的确定性/随机 tie-break；
- `exit_day`、PnL、MFE/MAE、未来收益与 post-exit 收益均未参与排序；
- 随机种子分布只是算法顺序敏感性，不得描述成市场置信区间；
- 稳健性 screen 只读取 train/val，viewed-test 不参与门槛；
- 即使顺序稳健性通过，也不得覆盖已有候选级 screen 失败结论，更不得上线。

输出包括：

- `D:\tmp\strategy_improvement_order_v3\report.json`
- `random_seed_runs_{train,val,test}.jsonl`
- `paired_seed_deltas_{train,val,test}.jsonl`

## v4：同日同优先级 pairwise 候选排序

v3 证明组合结果对任意候选顺序高度敏感，MFE/ATR 的退出改善也未通过顺序稳健性
门槛。因此 v4 冻结 baseline 退出和 P0 信号类型优先级，只学习同一入场日、同一
优先级桶内部的候选次序。

```powershell
cd D:\development\github\quant\quant-python\signal_system

python portfolio_pairwise_ranking_experiment.py `
  --exit-root D:\tmp\strategy_improvement_exit_v2 `
  --order-report D:\tmp\strategy_improvement_order_v3\report.json `
  --output-dir D:\tmp\strategy_improvement_ranking_v4
```

设计在运行结果前冻结：

- 模型为按入场日等权的 pairwise logistic，`L2=1.0`，不搜索参数；
- confirmation 字段从候选载荷读取；MACD、MA、ATR 和近期收益从本地 QFQ 缓存
  重新计算，严格截断到 `datetime <= signal_day`，并记录缓存 manifest；
- 连续特征只使用同日、同优先级桶内的横截面中位秩；
- P0 信号类型优先级不可跨越，模型只替换原来的任意 symbol/hash 次序；
- train 采用至少 5 个历史入场日后的逐日 walk-forward；
- validation 固定为 `train -> val`；
- viewed-test 固定为 `train+val -> test`，不参与 screen；
- 排序分数相同时使用固定 hash，不用 symbol 编号；
- `exit_day`、PnL、MFE/MAE、future 和 post-exit 字段禁止进入特征。

预注册门槛全部满足才算通过研究 screen：

- train walk-forward 至少 5 个模型评估日，pairwise accuracy > 50%；
- val pairwise accuracy > 50%；
- val 组合收益高于 v3 baseline 随机顺序中位数；
- val 组合收益位于随机顺序分布至少第 75 百分位；
- val 最大回撤不高于随机顺序中位数。

即使通过，也只说明候选排序值得进入独立复核，不代表生产可用，更不能直接使用
viewed-test 或 Holdout 调参。

## v5：长历史候选快照与 Purged Expanding Walk-Forward 折

v4 已因 train walk-forward 的 pairwise accuracy 低于随机、validation 回撤未优于
随机顺序中位数而淘汰。不要继续围绕现有 train/val/test 调模型权重或排序规则。
下一步只冻结更长历史的 baseline 候选快照和季度时间折；本阶段不拟合、不选择、
也不评估新模型。

先运行完全只读的 preflight。v5 固定使用 2026-08-24 已存在、且早于本轮 QFQ
快照生成的 `stock_list_v3_120` A 股清单；不能改成 QFQ/none 文件交集，也不能改用
运行日的在线股票列表：

```powershell
cd D:\development\github\quant\quant-python\signal_system

python backtest_winrate.py `
  --start 2023-09-01 `
  --end 2026-09-01 `
  --history-bars 1200 `
  --mode signal `
  --adjust qfq `
  --no-fetch-missing-adjusted `
  --local-data-only `
  --preflight-only `
  --universe-file D:\development\github\quant\quant-python\signal_system\cache\49c74bcce8953772e779e483af108c878820d52b5b8425db964fddb98a07f2b6.pkl `
  --universe-sha256 5d65ddbd8294102149bdd5924f3e13b004822993df37fe60732d7348ea34d3a5 `
  --index-data D:\development\github\quant\quant-python\signal_system\cache\index_000001_sh.pkl `
  --experiment-id p0_long_history_v5 `
  --dataset-role full
```

preflight 只向 stdout 输出 JSON，不写报告、缓存或候选；退出码 `0` 才允许继续。
它是运行前的保守数据完备性检查，不等价于正式 source/fold audit。退出码 `1` 时按
`blocking_by_reason` 和逐 symbol 记录修复本地数据，然后原参数重跑；不得剔除缺失
symbol、放宽股票池门槛或启用联网补取。

截至 2026-09-11，Codex 已在用户授权的数据维护阶段补齐本地股票池历史哈希 PKL
缓存；这些文件位于 `signal_system/cache/<sha256>.pkl`，不是 PostgreSQL 数据。上述
固定参数的全量 preflight 已得到 exit code `0`、`passes_preflight=true`、5007 symbols、
`blocking_symbols=0`、`stock_pool_coverage_ready=true`。回测模型仍必须自行原参数重跑
preflight，以确认其开始执行时缓存与代码快照没有漂移；通过后才运行以下正式命令。

首次正式目录 `D:\tmp\strategy_long_history_v5_retry1` 因 4 组同日同类型 MACD 确认路径
重复而在 fold 门禁停止，必须保留为失败证据，不得覆盖。信号生成器现固定为：同一确认
日、同一 zero-axis zone 仅保留最近一次金叉路径；因此后续正式与 verify 目录分别使用
`retry2` 和 `retry2_verify`。

preflight 通过后，从同一 frozen universe 和本地 QFQ 缓存生成完整 baseline 候选快照：

```powershell
cd D:\development\github\quant\quant-python\signal_system

python backtest_winrate.py `
  --start 2023-09-01 `
  --end 2026-09-01 `
  --history-bars 1200 `
  --mode signal `
  --adjust qfq `
  --no-fetch-missing-adjusted `
  --local-data-only `
  --universe-file D:\development\github\quant\quant-python\signal_system\cache\49c74bcce8953772e779e483af108c878820d52b5b8425db964fddb98a07f2b6.pkl `
  --universe-sha256 5d65ddbd8294102149bdd5924f3e13b004822993df37fe60732d7348ea34d3a5 `
  --index-data D:\development\github\quant\quant-python\signal_system\cache\index_000001_sh.pkl `
  --experiment-id p0_long_history_v5 `
  --dataset-role full `
  --out D:\tmp\strategy_long_history_v5_retry2\baseline_full.json
```

正式 strict-universe 回测会在进入信号计算前自动重跑同一 preflight。失败时返回 `1`，
且不创建 `--out` 的父目录或任何回测产物；成功时会在 source report 同目录额外保存
`<report-stem>_preflight.json`。source report 记录该完整证据的 SHA256，折构建器与独立
审计器都会重新读取、重算哈希，并核对固定 checks、运行窗口、配置、universe、指数、
逐 symbol QFQ/股票池输入；不能用手写摘要代替该证据文件。

再从冻结候选构建 expanding training + quarterly evaluation folds：

```powershell
python long_history_walk_forward_dataset.py `
  --candidates D:\tmp\strategy_long_history_v5_retry2\baseline_full_signal_trades.jsonl `
  --source-report D:\tmp\strategy_long_history_v5_retry2\baseline_full.json `
  --index-data D:\development\github\quant\quant-python\signal_system\cache\index_000001_sh.pkl `
  --output-dir D:\tmp\strategy_long_history_v5_retry2\folds `
  --dataset-start 2023-09-01 `
  --first-evaluation-start 2024-07-01 `
  --last-evaluation-end 2026-06-30 `
  --evaluation-months 3
```

固定规则：

- 不传 `--allow-holdout`，任何路径包含 `holdout` 时默认直接失败；全部输出均为
  development/viewed 数据，不是盲测。
- 不联网补缓存；源报告必须为 `qfq`、`local_data_only=true`、
  `fetch_missing_adjusted=false`、`allow_incomplete=false`，且回测窗口覆盖折窗口。
- universe 必须来自上面固定路径且 SHA256 完全一致；清单中的每只股票都计入
  `symbols_requested`。目录中额外的 `588000_none.pkl` 只记为 drift，不得进入 universe；
  清单内缺任一所需历史时必须失败。当前股票清单用于历史窗口存在幸存者偏差，因此
  v5 仅用于开发折构建，不能称为历史时点成分股回测。
- 严格 universe 禁止与 `--limit` 组合。source report 必须记录并产出完整 universe
  的 QFQ 逐文件 manifest，同时锚定实际指数文件、`backtest_winrate.py` 与
  `strategy/macd.py` 的 SHA256；
  折构建器和独立审计器会复算，不能只哈希最终产生候选的股票。
- 候选产物中的 symbol 必须是未规范化的六位数字原值且属于 frozen universe；
  `2`、`000002.SZ` 等别名直接失败，不得借表示差异绕过 preflight 豁免检查。
- 股票池过滤只允许读取本地 `*_none.pkl` 或既有哈希缓存中的历史换手率/流通市值；
  源报告记录实际使用文件的 manifest。任一需要股票池判断的代码缺少本地历史时，
  折构建器拒绝该候选快照，不能把数据缺失静默当成策略拒绝。
- `stock_pool.missing_data_policy` 必须为 `reject`；`allow` 会令 source audit 硬失败。
- 每折训练集只含 `entry_day < evaluation_start` 且
  `exit_day < evaluation_start` 的成熟标签；跨越评估起点的交易写入
  `purged_training_labels.jsonl`，不得进入该折训练。
- 评估窗口固定为连续三个月，训练窗口只扩张不滚动。不得读取逐折指标后修改
  折边界、样本门槛或 purge 规则。
- 在读取任何逐折评估结果之前，必须先独立冻结下一代模型的特征、标签、损失、
  正则化、决策阈值、排序与聚合门槛。构建器自身只产出数据，不拟合模型，报告中
  `model_fitted=false`、`production_eligible=false`。

### v5 分工与机器审计

只保留两个角色：Codex 负责实现、测试和冻结规则；另一个模型负责全部只读回测与审计。
回测模型先运行 preflight；退出码不是 `0` 或 `passes_preflight` 不是 `true` 时立即停止
并报告缺失，不得继续正式回测。通过后生成正式目录，再把正式回测与折构建命令中的
根目录改为 `D:\tmp\strategy_long_history_v5_retry2_verify` 完整重复一次。不得用复制文件
代替第二次回测，也不得覆盖第一次的冻结目录。

同一个回测模型随后运行这条只读审计命令：

```powershell
cd D:\development\github\quant\quant-python\signal_system

python long_history_walk_forward_audit.py `
  --source-report D:\tmp\strategy_long_history_v5_retry2\baseline_full.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --verify-source-report D:\tmp\strategy_long_history_v5_retry2_verify\baseline_full.json `
  --verify-fold-report D:\tmp\strategy_long_history_v5_retry2_verify\folds\report.json
```

审计器不写文件，并且没有 Holdout override。它会独立检查：

- source report 的本地/QFQ/完整交易约束、symbol 失败数和候选 ID 唯一性；
- 完整 preflight artifact 的 SHA256、固定 checks 与 source report 输入绑定；
- frozen universe 文件、排序 symbol manifest、requested 数量与逐文件 SHA256；
- 股票池哈希缓存清单中的每个文件 SHA256，以及 stale/missing/fetch failure 为零；
- QFQ、指数、backtest engine、MACD signal engine、builder、候选和每折 JSONL 的记录哈希；
- 每个 candidate 独立重算后的 train/purged/evaluation 归属；
- 评估窗口连续性、expanding training、逐折最低样本和 evaluation 完整覆盖；
- 正式与 verify 的全部候选/折产物逐字节一致，报告剥离路径差异后结构一致。

只有 `passes_audit=true`、`determinism.checked=true` 且两个 normalized report
检查均为 true，v5 数据冻结才算完成。回测模型必须在一份报告中同时覆盖输入与哈希、
边界、Purge、覆盖率和确定性；不得修改代码或产物，也不得根据数据结果提出新模型参数。

## v6：冻结的长历史 Pairwise 排序复核

v5 `retry2` 已完成 source、fold 和确定性审计。v6 原样复用 v4 的 day-weighted pairwise
logistic，不搜索参数，只在 `fold_03` 至 `fold_08` 上进行 purged expanding
walk-forward 复核。Codex 只负责实现、测试和冻结规格；唯一另一个模型负责以下两次真实
实验运行和只读 audit，不再拆分 A/B/C/D 多个角色。

运行前确认以下两个输出目录均不存在；不得覆盖旧结果，不得复制第一次产物伪装第二次运行：

```powershell
Test-Path D:\tmp\strategy_long_history_pairwise_v6
Test-Path D:\tmp\strategy_long_history_pairwise_v6_verify
```

两项都必须为 `False`。然后执行第一次实验：

```powershell
cd D:\development\github\quant\quant-python\signal_system

python long_history_pairwise_ranking_experiment.py `
  --source-report D:\tmp\strategy_long_history_v5_retry2\baseline_full.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --output-dir D:\tmp\strategy_long_history_pairwise_v6
```

命令退出码必须为 `0`。任何 v5 输入审计、固定折、候选一致性、QFQ manifest 或输出目录
门禁失败时立即停止并原样报告；不得修改代码、折、候选、特征、种子或门槛。成功后用同一
冻结输入进行第二次独立拟合和组合回放：

```powershell
python long_history_pairwise_ranking_experiment.py `
  --source-report D:\tmp\strategy_long_history_v5_retry2\baseline_full.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --output-dir D:\tmp\strategy_long_history_pairwise_v6_verify
```

第二次也必须真实运行，不能从第一个目录复制 JSON/JSONL。两次成功后，由同一个回测模型
执行只读完整重放 audit：

```powershell
python long_history_pairwise_ranking_audit.py `
  --primary-report D:\tmp\strategy_long_history_pairwise_v6\report.json `
  --verify-report D:\tmp\strategy_long_history_pairwise_v6_verify\report.json
```

audit 不写文件。它会重新执行 v5 source/fold 审计，重新读取本地 QFQ，按每折
`train.jsonl` 独立拟合，重算 evaluation 分数、pairwise 指标、ranked/symbol/hash 组合和
全部 200 个随机种子，并逐行核对 42 个 JSONL。随后检查两次运行的 JSONL 逐字节一致，
以及剥离两个输出根目录后 `report.json` 结构一致。

固定契约：

- 只允许 `fold_03..fold_08` 六折；`purged_training_labels.jsonl` 永不训练；
- `L2=1.0`、`max_iterations=100`、`tolerance=1e-10`，特征和 missing indicators 与 v4 一致；
- 技术特征严格来自本地 QFQ 的 `datetime <= signal_day`；
- P0 signal priority 不可跨越，模型分数裁剪为 ±100，priority gap 为 1000，同分固定 hash；
- 随机基准精确使用 `20260830..20261029` 共 200 个连续种子；
- 路径含 `holdout` 时直接失败，无 override；不连接数据库、不联网、不修改生产配置；
- 无配对训练数据时使用零模型/hash 回退并令研究 screen 失败，不得为通过 screen 放宽规则；
- 随机顺序分布只表示算法顺序敏感性，不是独立市场样本或置信区间。

只有 audit 输出 `passes_audit=true`、`determinism.checked=true`、全部
`artifact_byte_identical=true` 且 `normalized_report_equal=true` 后，才允许读取和解释
`screen`。无论 `passes_research_screen` 为真或假，`production_eligible` 永远为 `false`；
本轮不使用 Holdout，也不构成上线验证。

## v6 后续：Pairwise 排序机制只读诊断

v6 研究 screen 已失败，不能修改门槛或围绕同一结果调参。本阶段只解释“全量折级排序不稳定，
但 stitched 四仓组合收益较强”的矛盾，为是否值得另行预注册 v7 提供机制证据。诊断不重新
训练、不增加因子、不改变 v6 结论，也没有新的研究通过门槛。

仍只保留两个角色：Codex 编写和测试代码；唯一另一个模型运行两次诊断与只读 audit。
先确认两个新目录不存在：

```powershell
Test-Path D:\tmp\strategy_long_history_pairwise_v6_diagnostic
Test-Path D:\tmp\strategy_long_history_pairwise_v6_diagnostic_verify
```

两项都必须为 `False`。第一次读取 v6 primary：

```powershell
cd D:\development\github\quant\quant-python\signal_system

python long_history_pairwise_ranking_diagnostic.py `
  --v6-report D:\tmp\strategy_long_history_pairwise_v6\report.json `
  --output-dir D:\tmp\strategy_long_history_pairwise_v6_diagnostic
```

退出码必须为 `0`。然后第二次独立读取 v6 verify：

```powershell
python long_history_pairwise_ranking_diagnostic.py `
  --v6-report D:\tmp\strategy_long_history_pairwise_v6_verify\report.json `
  --output-dir D:\tmp\strategy_long_history_pairwise_v6_diagnostic_verify
```

两次均成功后运行只读完整重放 audit：

```powershell
python long_history_pairwise_ranking_diagnostic_audit.py `
  --primary-report D:\tmp\strategy_long_history_pairwise_v6_diagnostic\report.json `
  --verify-report D:\tmp\strategy_long_history_pairwise_v6_diagnostic_verify\report.json
```

每次诊断都会先完整重放对应的 v6 单报告 audit，然后固定计算：

- 同日同 signal type 桶内 Top-1 最佳命中、结果百分位和相对桶均值优势；
- 候选数至少 5 的桶内 Top-4 相对剩余候选的均值/中位数差和边界 pairwise accuracy；
- 实际 accepted 与同日同 signal type、因 `max_positions` 被拒绝候选的冻结收益差；
- 桶内模型分数五分位的候选收益、胜率、桶等权收益和 Q5-Q1；
- 六折 18 个系数的符号稳定性及 15 组折间 cosine similarity；
- stitched 已实现交易按 entry day/month 的正收益集中度；
- 剔除净 PnL 最高 entry day、最高 entry month 后，不重新拟合，使用原 OOS 分数重放
  ranked/symbol/hash/200-seed 随机顺序。

产物固定为 7 个 JSONL：`bucket_diagnostics`、`capacity_diagnostics`、
`score_quintiles`、`signal_type_diagnostics`、`coefficient_stability`、`scope_summary`、
`leave_one_out_random_runs`。audit 必须返回：

- `passes_audit=true`；
- `determinism.checked=true`；
- 7/7 `artifact_byte_identical=true`；
- `normalized_report_equal=true`；
- `input_v6_determinism.checks_passed=true`。

任何输入/代码/artifact 哈希或重算结果不一致时立即停止，不得修结果。报告只能描述机制，
不得宣布 v6 复活或直接采用 v7；固定声明为 `diagnostic_status=post_hoc_exploratory`、
`holdout_used=false`、`production_eligible=false`。

## v7 第一阶段：新因子数据预检与冻结

v6 的分数五分位、Top-1/Top-4 和折级 OOS 排序证据不足，因此关闭继续调节旧 9 个技术因子、
Top-K 或 pairwise 参数的路线。v7 不直接训练，先只回答 19 个预注册新因子在 v5 冻结候选上
是否具有因果覆盖和可重复输入。该阶段不得读取收益来选因子，报告固定为
`model_fitted=false`、`outcomes_read=false`、`hyperparameters_selected=false`、
`holdout_used=false`、`production_eligible=false`。

正式运行仍只保留两个角色：Codex 写代码、测试和自审；唯一另一个模型执行两次独立预检与
只读 audit。先确认两个新目录均不存在：

```powershell
Test-Path D:\tmp\strategy_long_history_v7_factor_preflight
Test-Path D:\tmp\strategy_long_history_v7_factor_preflight_verify
```

两项必须为 `False`。随后在 `quant-python\signal_system` 运行：

```powershell
python long_history_v7_factor_preflight.py `
  --source-report D:\tmp\strategy_long_history_v5_retry2\baseline_full.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --index-data D:\development\github\quant\quant-python\signal_system\cache\index_000001_sh.pkl `
  --output-dir D:\tmp\strategy_long_history_v7_factor_preflight

python long_history_v7_factor_preflight.py `
  --source-report D:\tmp\strategy_long_history_v5_retry2\baseline_full.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --index-data D:\development\github\quant\quant-python\signal_system\cache\index_000001_sh.pkl `
  --output-dir D:\tmp\strategy_long_history_v7_factor_preflight_verify

python long_history_v7_factor_preflight_audit.py `
  --primary-report D:\tmp\strategy_long_history_v7_factor_preflight\report.json `
  --verify-report D:\tmp\strategy_long_history_v7_factor_preflight_verify\report.json
```

两次主程序都必须退出 0，且第二次必须真实重读、重算，不得复制第一份目录。audit 不写文件，
必须返回 `passes_audit=true`、`determinism.checked=true`、4/4
`artifact_byte_identical=true` 和 `normalized_report_equal=true`。预检通过只表示数据可冻结；
由用户看过逐因子 coverage 后，另行冻结最终因子、模型、标签和 screen，才能开始 v7 walk-forward。

## v7a：Top-4 边界排序 walk-forward

v7 factor preflight v2 已通过双运行和独立公式 audit。v7a 固定使用全部 19 因子，只验证
`entry_day + signal_type` 桶内实际 Top-4 与其余候选的排序边界，不运行资金组合。模型为固定
L2=1.0 的无截距线性 pairwise logistic；候选至少 5 只的桶才产生训练/评估指标；收益相同的
边界 pair 排除；每桶训练总权重相同。禁止调参、删因子、改 Top-K 或用组合收益挽救 screen 失败。

正式运行前确认两个目标目录均不存在：

```powershell
Test-Path D:\tmp\strategy_long_history_v7_top4
Test-Path D:\tmp\strategy_long_history_v7_top4_verify
```

两项必须为 `False`。随后真实独立执行两次：

```powershell
cd D:\development\github\quant\quant-python\signal_system

python long_history_v7_top4_ranking_experiment.py `
  --factor-report D:\tmp\strategy_long_history_v7_factor_preflight\report.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --output-dir D:\tmp\strategy_long_history_v7_top4

python long_history_v7_top4_ranking_experiment.py `
  --factor-report D:\tmp\strategy_long_history_v7_factor_preflight\report.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --output-dir D:\tmp\strategy_long_history_v7_top4_verify

python long_history_v7_top4_ranking_audit.py `
  --primary-report D:\tmp\strategy_long_history_v7_top4\report.json `
  --verify-report D:\tmp\strategy_long_history_v7_top4_verify\report.json
```

两次 experiment 退出码必须为 0；`passes_research_screen` 可以为 true 或 false，均不得修改规则后
重跑。audit 必须为 `passes_audit=true`、`determinism.checked=true`、14/14 artifact byte-identical、
`normalized_report_equal=true`。只有 audit 通过后才能解释 screen。即使 v7a screen 通过，仍只授权
另行冻结和实现 v7b 组合回放；`production_eligible` 始终为 false，不授权生产上线。

## v7a 失败后：Top-4 尾部稳定性只读诊断

v7a 已通过完整重放和 14/14 产物确定性审计，但固定 research screen 因 Top-4 平均收益优势仅 3/6 折为正而失败。该失败结论不可通过 post-hoc 诊断改写。下一步只读解释 fold_04、fold_07、fold_08 的负优势桶、月份、signal type、accuracy/收益错配、逐桶/逐月 leave-one-out、系数稳定性和冻结 Top-4 因子暴露；不拟合模型、不回放资金组合、不选择因子、不运行 v7b。

仍只保留两个角色：Codex 写代码、测试和本地自审；唯一另一个模型运行两次正式诊断与只读 audit。运行前先确认两个新目录均不存在：

```powershell
Test-Path D:\tmp\strategy_long_history_v7_top4_diagnostic
Test-Path D:\tmp\strategy_long_history_v7_top4_diagnostic_verify
```

两项必须均为 `False`。随后分别读取 v7a primary 与 verify：

```powershell
cd D:\development\github\quant\quant-python\signal_system

python long_history_v7_top4_ranking_diagnostic.py `
  --v7-report D:\tmp\strategy_long_history_v7_top4\report.json `
  --output-dir D:\tmp\strategy_long_history_v7_top4_diagnostic

python long_history_v7_top4_ranking_diagnostic.py `
  --v7-report D:\tmp\strategy_long_history_v7_top4_verify\report.json `
  --output-dir D:\tmp\strategy_long_history_v7_top4_diagnostic_verify

python long_history_v7_top4_ranking_diagnostic_audit.py `
  --primary-report D:\tmp\strategy_long_history_v7_top4_diagnostic\report.json `
  --verify-report D:\tmp\strategy_long_history_v7_top4_diagnostic_verify\report.json
```

两次诊断必须真实独立执行，不得复制目录或 JSONL。任一步 exit 非 0 时立即停止，不得修改输入、代码、门槛或诊断定义后续跑。audit 必须同时满足：

- `passes_audit=true`
- `determinism.checked=true`、`determinism.checks_passed=true`
- `determinism.artifact_count=8` 且 8/8 `artifact_byte_identical=true`
- `determinism.normalized_report_equal=true`
- `determinism.input_v7_determinism.artifact_count=14`
- 输入 v7a 14/14 `artifact_byte_identical=true`
- `determinism.input_v7_determinism.normalized_report_equal=true`
- `policy.full_diagnostic_replay=true`
- `policy.input_full_model_replay=true`
- `policy.portfolio_replayed=false`

只有上述 audit 全部通过后才能解释诊断。最终报告必须保留：`diagnostic_status=post_hoc_exploratory`、`changes_v7a_screen=false`、`selects_new_factors=false`、`authorizes_v7b=false`、`holdout_used=false`、`production_eligible=false`。诊断结果只能帮助形成未来另行预注册的新研究假设，不能挽救 v7a。

## v8a：收益差加权 Top-4 boundary ranking

v7a 尾部诊断表明，少数漏掉大赢家的 boundary 错序具有远高于普通错序的经济代价。v8a 是已查看 development 数据上的 post-hoc 后续实验，只改变训练 pair 权重：每个实际 Top-4 对 rest 的有效 pair 使用 `abs(trade_pnl_pct gap)`，随后在各自 `entry_day + signal_type` 桶内归一，使每桶训练总权重仍为 1。无权重截断、floor、幂次搜索或其他超参数。

19 因子、桶内 midrank、Top-K=4、最小桶大小5、fold_03..08、无截距线性 logistic、L2=1.0、打分、OOS 指标和 research screen 全部与 v7a 相同。禁止运行组合、删因子、硬编码月份/signal type 或加入可靠性 gate。

正式运行前确认两个新目录不存在：

```powershell
Test-Path D:\tmp\strategy_long_history_v8_gap_weighted_top4
Test-Path D:\tmp\strategy_long_history_v8_gap_weighted_top4_verify
```

两项必须均为 `False`。随后真实独立运行两次：

```powershell
cd D:\development\github\quant\quant-python\signal_system

python long_history_v8_gap_weighted_top4_ranking_experiment.py `
  --factor-report D:\tmp\strategy_long_history_v7_factor_preflight\report.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --output-dir D:\tmp\strategy_long_history_v8_gap_weighted_top4

python long_history_v8_gap_weighted_top4_ranking_experiment.py `
  --factor-report D:\tmp\strategy_long_history_v7_factor_preflight\report.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --output-dir D:\tmp\strategy_long_history_v8_gap_weighted_top4_verify

python long_history_v8_gap_weighted_top4_ranking_audit.py `
  --primary-report D:\tmp\strategy_long_history_v8_gap_weighted_top4\report.json `
  --verify-report D:\tmp\strategy_long_history_v8_gap_weighted_top4_verify\report.json
```

两次 experiment 均必须真实拟合，不得复制产物。任一步 exit 非 0 时立即停止。audit 必须满足：`passes_audit=true`、`determinism.checked=true`、`determinism.checks_passed=true`、`artifact_count=14`、14/14 `artifact_byte_identical=true`、`normalized_report_equal=true`、`input_factor_preflight_replayed=true`、`primary.independent_gap_weighting.checks_passed=true`、`verify.independent_gap_weighting.checks_passed=true`、`policy.full_model_replay=true`、`policy.gap_weighting_replayed=true`、`policy.independent_gap_weighting_checked=true`、`policy.v7a_screen_reused_without_change=true`、`policy.portfolio_replayed=false`。

audit 全绿前不得读取或解释 screen。若 `passes_research_screen=false`，停止当前线性 Top-4 排序路线，不修改权重或门槛后再跑；若为 true，也只授权另行冻结 v8b。任何结果均为 development 研究，`holdout_used=false`、`production_eligible=false`。

## v9a：底部 20% 风险过滤器

v8a 已通过确定性审计但 research screen 失败，当前线性 Top-4 赢家排序路线终止。v9a 不再预测赢家，而是测试冻结的 19 因子能否识别同一 `entry_day + signal_type` 桶内实际收益最低的 20% 候选。每桶风险数量固定为 `max(1, ceil(bucket_size * 0.20))`，实际标签按 `trade_pnl_pct asc, candidate_id asc` 确定；边界同收益使用 candidate_id 确定性拆分并保留。

训练仍使用桶内居中 midrank、missing=0、无截距 logistic、L2=1.0、100 iterations、tolerance=1e-10 和 score clip ±100。每个训练桶总权重为1，其中风险类合计0.5、安全类合计0.5；收益幅度不参与样本加权。不删因子、不调参、不运行组合、不使用 Holdout。

v9a 输入必须锚定已经审计的 v8a primary report：

```text
D:\tmp\strategy_long_history_v8_gap_weighted_top4\report.json
SHA256 = 4a87a1bd83d446913e83a77f44b33dabce8dc1e2367c767c2188436e01cbd22b
```

正式运行前确认两个新目录不存在：

```powershell
Test-Path D:\tmp\strategy_long_history_v9_bottom_tail_risk
Test-Path D:\tmp\strategy_long_history_v9_bottom_tail_risk_verify
```

两项必须均为 `False`。随后真实独立运行两次：

```powershell
cd D:\development\github\quant\quant-python\signal_system

python long_history_v9_bottom_tail_risk_experiment.py `
  --factor-report D:\tmp\strategy_long_history_v7_factor_preflight\report.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --v8a-report D:\tmp\strategy_long_history_v8_gap_weighted_top4\report.json `
  --output-dir D:\tmp\strategy_long_history_v9_bottom_tail_risk

python long_history_v9_bottom_tail_risk_experiment.py `
  --factor-report D:\tmp\strategy_long_history_v7_factor_preflight\report.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --v8a-report D:\tmp\strategy_long_history_v8_gap_weighted_top4\report.json `
  --output-dir D:\tmp\strategy_long_history_v9_bottom_tail_risk_verify

python long_history_v9_bottom_tail_risk_audit.py `
  --primary-report D:\tmp\strategy_long_history_v9_bottom_tail_risk\report.json `
  --verify-report D:\tmp\strategy_long_history_v9_bottom_tail_risk_verify\report.json
```

两次 experiment 均必须真实拟合，不得复制产物。任一步 exit 非0时立即停止并保留失败证据。audit 必须满足：`passes_audit=true`、`determinism.checked=true`、`determinism.checks_passed=true`、`artifact_count=14`、14/14 `artifact_byte_identical=true`、`normalized_report_equal=true`、`input_factor_preflight_replayed=true`、`parent_v8a_failure_anchored=true`、`primary/verify.parent_v8a.checks_passed=true`、`primary/verify.independent_risk_target.checks_passed=true`、`primary/verify.independent_screen.checks_passed=true`、`policy.full_model_replay=true`、`policy.independent_risk_target_and_weights_checked=true`、`policy.independent_research_screen_checked=true`、`policy.outcome_magnitude_used_as_training_weight=false`、`policy.factor_selection_performed=false`、`policy.portfolio_replayed=false`。

audit 全绿前不得读取或解释 screen。若 `passes_research_screen=false`，停止使用当前 19 因子建模，转回候选生成机制及新增原始因子研究；若为 true，也只授权另行冻结 v9b，不得直接运行组合或 Holdout。任何结果均为 development 研究，`production_eligible=false`。

## v10：12 个新原始因子的 outcome-blind 预检

v9a 已通过完整重放审计，但固定 research screen 失败，因此停止使用原 19 因子继续建模，不运行 v9b。v10 只冻结并计算 12 个与 v7 不重叠的新原始因子，覆盖市场状态、短期相对风险、价格路径和量价确认四类；不拟合模型、不读取候选收益、不选择因子、不回放组合，也不使用 Holdout。行业相对因子和市场宽度因子因冻结输入中没有独立数据而不纳入，禁止联网补齐。

正式运行前确认两个目标目录均不存在：

```powershell
Test-Path D:\tmp\strategy_long_history_v10_new_factor_preflight
Test-Path D:\tmp\strategy_long_history_v10_new_factor_preflight_verify
```

两项必须均为 `False`；若目录已存在，立即停止，不删除、不覆盖。随后在同一冻结代码和输入上真实独立运行两次：

```powershell
cd D:\development\github\quant\quant-python\signal_system

python long_history_v10_new_factor_preflight.py `
  --v7-factor-report D:\tmp\strategy_long_history_v7_factor_preflight\report.json `
  --source-report D:\tmp\strategy_long_history_v5_retry2\baseline_full.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --index-data D:\development\github\quant\quant-python\signal_system\cache\index_000001_sh.pkl `
  --v9a-report D:\tmp\strategy_long_history_v9_bottom_tail_risk\report.json `
  --output-dir D:\tmp\strategy_long_history_v10_new_factor_preflight

python long_history_v10_new_factor_preflight.py `
  --v7-factor-report D:\tmp\strategy_long_history_v7_factor_preflight\report.json `
  --source-report D:\tmp\strategy_long_history_v5_retry2\baseline_full.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --index-data D:\development\github\quant\quant-python\signal_system\cache\index_000001_sh.pkl `
  --v9a-report D:\tmp\strategy_long_history_v9_bottom_tail_risk\report.json `
  --output-dir D:\tmp\strategy_long_history_v10_new_factor_preflight_verify

python long_history_v10_new_factor_preflight_audit.py `
  --primary-report D:\tmp\strategy_long_history_v10_new_factor_preflight\report.json `
  --verify-report D:\tmp\strategy_long_history_v10_new_factor_preflight_verify\report.json
```

第二次运行必须重新读取全部冻结输入并重新计算 12 个因子，不得复制第一份目录或 JSONL。任一步 exit 非 0 时立即停止并原样报告错误，不得修改数据、因子公式、缺失值规则或输入锚点后继续跑。

audit 必须同时满足：`passes_audit=true`、`determinism.checked=true`、`determinism.checks_passed=true`、`determinism.artifact_count=4`、4/4 `artifact_byte_identical=true`、`determinism.normalized_report_equal=true`、`input_v7_factor_preflight_replayed=true`、`parent_v9a_failure_hash_anchored=true`、`primary/verify.independent_factor_implementation=true`、`policy.full_feature_replay=true`、`policy.independent_factor_implementation=true`、`policy.coverage_used_for_selection=false`、`policy.candidate_outcomes_read=false`、`policy.factor_selection_performed=false`、`policy.model_fitted=false`。

只有 audit 全绿后才可汇报逐因子 coverage 和缺失数量，但 coverage 仅用于判断数据可用性，不得据此删因子、选因子或授权 v10 训练。最终声明必须保留：`candidate_outcomes_read=false`、`factor_selection_performed=false`、`hyperparameters_selected=false`、`model_fitted=false`、`portfolio_replayed=false`、`holdout_used=false`、`production_eligible=false`。该阶段不是生产回测结论。

## v10a：9 个股票横截面新因子的 Bottom-Tail-Risk walk-forward

v10 outcome-blind preflight 已通过双运行和独立公式 audit，12 个新因子在 4471 个候选上完整覆盖。v10a 保持 v9a 的 Bottom 20% 标签、折、无截距 logistic、L2=1.0、每桶 risk/safe 各 0.5 权重、OOS 指标及 research screen 完全不变，只将原 19 因子替换为下列 9 个预注册股票横截面因子：

```text
excess_return_5
stock_index_correlation_20
residual_volatility_20
stock_realized_volatility_20
drawdown_from_high_60
price_efficiency_20
volume_mean_5_to_20
amount_mean_5_to_20
return_volume_change_correlation_20
```

`index_return_60`、`index_realized_volatility_20`、`index_drawdown_from_high_60` 三个市场状态因子不参与本轮股票排序。该分区不使用 outcome：冻结输入的 436 个 `entry_day + signal_type` 执行桶中，435 个只有一个 signal day；唯一混合两个 signal day 的执行桶会令两个指数因子产生两个值。指数因子在同一 signal day 的候选之间均恒定，因此不允许模型借混合信号日期的市场时点差异排序股票。该结构必须由 experiment 与 audit 分别复核，不得根据 OOS 结果改变分区。

正式运行前确认两个目标目录均不存在：

```powershell
Test-Path D:\tmp\strategy_long_history_v10_cross_sectional_bottom_tail_risk
Test-Path D:\tmp\strategy_long_history_v10_cross_sectional_bottom_tail_risk_verify
```

两项必须均为 `False`；若目录已存在，立即停止，不删除、不覆盖。随后真实独立运行两次：

```powershell
cd D:\development\github\quant\quant-python\signal_system

python long_history_v10_cross_sectional_bottom_tail_risk_experiment.py `
  --factor-report D:\tmp\strategy_long_history_v10_new_factor_preflight\report.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --v9a-report D:\tmp\strategy_long_history_v9_bottom_tail_risk\report.json `
  --output-dir D:\tmp\strategy_long_history_v10_cross_sectional_bottom_tail_risk

python long_history_v10_cross_sectional_bottom_tail_risk_experiment.py `
  --factor-report D:\tmp\strategy_long_history_v10_new_factor_preflight\report.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --v9a-report D:\tmp\strategy_long_history_v9_bottom_tail_risk\report.json `
  --output-dir D:\tmp\strategy_long_history_v10_cross_sectional_bottom_tail_risk_verify

python long_history_v10_cross_sectional_bottom_tail_risk_audit.py `
  --primary-report D:\tmp\strategy_long_history_v10_cross_sectional_bottom_tail_risk\report.json `
  --verify-report D:\tmp\strategy_long_history_v10_cross_sectional_bottom_tail_risk_verify\report.json
```

第二次 experiment 必须完整重放 v10 preflight 并真实独立拟合六折模型，不得复制第一份目录或 JSONL。任一步 exit 非 0 时立即停止并保留原始错误，不得修改因子、标签、权重、折、L2、门槛或输入锚点后重跑。

audit 必须同时满足：`passes_audit=true`、`determinism.checked=true`、`determinism.checks_passed=true`、`determinism.artifact_count=14`、14/14 `artifact_byte_identical=true`、`determinism.normalized_report_equal=true`、`input_v10_factor_preflight_replayed=true`、`parent_v9a_failure_anchored=true`、`primary/verify.input_v10_factor_preflight.checks_passed=true`、`primary/verify.independent_factor_partition.checks_passed=true`、`primary/verify.independent_risk_target.checks_passed=true`、`primary/verify.independent_screen.checks_passed=true`、`policy.full_model_replay=true`、`policy.independent_factor_partition_checked=true`、`policy.reserved_market_factors_same_signal_day_constancy_checked=true`、`policy.mixed_signal_day_market_timing_blocked_from_stock_ranking=true`、`policy.independent_risk_target_and_weights_checked=true`、`policy.independent_research_screen_checked=true`、`policy.v9a_target_model_weights_and_screen_reused_without_change=true`、`policy.factor_selection_performed=false`、`policy.portfolio_replayed=false`。

audit 全绿前不得读取或解释 `passes_research_screen`、折级指标或 stitched 指标。若 screen 失败，终止当前 9 因子 Bottom-Tail-Risk 路线，不得删因子、改权重或改门槛后重跑；若 screen 通过，也只授权另行冻结组合回放，不能直接运行组合或 Holdout。任何结果均为 development walk-forward，`production_eligible=false`。

## v10b：3 个市场状态因子的信号桶参与 Gate

v10a 已通过双运行与完整重放 audit，但固定 research screen 失败：折级风险识别和经济优势不足，stitched `kept−rejected` 均值为负，因此 9 个股票横截面因子的 Bottom-Tail-Risk 路线终止。v10b 是当前 v10 因子集合最后一条预注册路线，只检验“整个信号桶是否应该参与”，不做桶内选股或股票排序。

样本桶固定为 `signal_day + entry_day + signal_type`，桶收益为候选 `trade_pnl_pct` 的等权均值，标签固定为 `equal_weight_mean_trade_pnl_pct < 0`。模型只使用 `index_return_60`、`index_realized_volatility_20`、`index_drawdown_from_high_60` 三个市场状态因子。每折仅用训练桶经验 CDF：训练样本采用 training midrank 并居中到 `[-0.5, 0.5]`，OOS 值只映射到该折训练 reference distribution，禁止把 OOS 值加入 reference。模型为无截距二元 logistic，`L2=1.0`、`max_iter=100`、`tolerance=1e-10`、score clip `±100`；bad/non-bad 两类训练总权重各 0.5，同类内每桶等权，收益幅度不参与权重。Gate 固定为 `probability > 0.5` 才拒绝，恰好等于 0.5 时保留，不搜索阈值。

正式运行前确认两个目标目录均不存在：

```powershell
Test-Path D:\tmp\strategy_long_history_v10_market_regime_bucket_gate
Test-Path D:\tmp\strategy_long_history_v10_market_regime_bucket_gate_verify
```

两项必须均为 `False`；若目录已存在，立即停止，不删除、不覆盖。随后在同一冻结代码和输入上真实独立运行两次：

```powershell
cd D:\development\github\quant\quant-python\signal_system

python long_history_v10_market_regime_bucket_gate_experiment.py `
  --factor-report D:\tmp\strategy_long_history_v10_new_factor_preflight\report.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --v10a-report D:\tmp\strategy_long_history_v10_cross_sectional_bottom_tail_risk\report.json `
  --output-dir D:\tmp\strategy_long_history_v10_market_regime_bucket_gate

python long_history_v10_market_regime_bucket_gate_experiment.py `
  --factor-report D:\tmp\strategy_long_history_v10_new_factor_preflight\report.json `
  --fold-report D:\tmp\strategy_long_history_v5_retry2\folds\report.json `
  --v10a-report D:\tmp\strategy_long_history_v10_cross_sectional_bottom_tail_risk\report.json `
  --output-dir D:\tmp\strategy_long_history_v10_market_regime_bucket_gate_verify

python long_history_v10_market_regime_bucket_gate_audit.py `
  --primary-report D:\tmp\strategy_long_history_v10_market_regime_bucket_gate\report.json `
  --verify-report D:\tmp\strategy_long_history_v10_market_regime_bucket_gate_verify\report.json
```

第二次 experiment 必须重新读取冻结输入、重新构造全部桶、重新建立每折训练 ECDF 并独立拟合六折模型，不得复制第一次目录或 JSONL。任一步 exit 非 0 时立即停止并原样保留错误，不修改桶键、标签、因子、CDF、权重、L2、阈值、screen 或输入锚点后重跑。

audit 必须同时满足：`passes_audit=true`、`determinism.checked=true`、`determinism.checks_passed=true`、`determinism.artifact_count=14`、14/14 `artifact_byte_identical=true`、`determinism.normalized_report_equal=true`、`input_v10_factor_preflight_replayed=true`、`parent_v10a_failure_anchored=true`、`primary/verify.input_v10_factor_preflight.checks_passed=true`、`primary/verify.parent_v10a.checks_passed=true`、`primary/verify.independent_bucket_gate.checks_passed=true`、`primary/verify.independent_screen.checks_passed=true`、`policy.full_model_replay=true`、`policy.independent_bucket_construction_checked=true`、`policy.market_features_constant_inside_signal_bucket_checked=true`、`policy.independent_train_only_feature_transform_checked=true`、`policy.independent_bucket_target_and_weights_checked=true`、`policy.independent_research_screen_checked=true`、`policy.stock_ranking_performed=false`、`policy.factor_selection_performed=false`、`policy.portfolio_replayed=false`、`policy.holdout_used=false`、`policy.production_eligible=false`。

只有 audit 全绿后才能读取和解释 screen。固定 screen 要求：fold 恰为 03..08、六折均收敛、每折训练桶不少于 20 且两类各不少于 5、至少 4/6 折 balanced accuracy 大于 0.5、至少 4/6 折 kept 均值高于 rejected、两项折级中位数均为正、stitched 桶不少于 50 且 kept/rejected 各不少于 20、stitched balanced accuracy 大于 0.5、kept 均值高于 rejected、kept 负收益桶率低于 ungated baseline。若 `passes_research_screen=false`，整个当前 v10 因子集合终止，不得调整标签、权重或阈值重跑；若为 true，也只授权另行冻结组合回放方案，不能直接运行组合或 Holdout。任何结果均为 development walk-forward，`production_eligible=false`。
