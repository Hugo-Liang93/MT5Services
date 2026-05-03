# Research 信号挖掘系统

> 纯数据驱动的信号发现引擎。职责边界：**发现，不验证**——不涉及现有策略评估。
> 文档类型：研究子系统说明，不描述实时交易服务的启动链路、日志路径或健康探针。
> 如果目标是判断当前服务是否能启动、链路是否贯通，请回到 `docs/architecture.md` 与 `docs/design/full-runtime-dataflow.md`。

---

## ⚠️ 重要警告（2026-04-15 教训）：forward_return 与实盘 exit 的鸿沟

**MiningRunner 的 `DataMatrix.forward_return` 只测"入场后 N bar (3/5/10) 收盘价相对入场价收益"——短期定点观测**。

**回测引擎和实盘用 Chandelier Exit + trailing + regime exit + signal reversal + EOD close，平均持仓周期 20-50 bar**。

**同一入场条件下，短期 forward_return 胜率与长期 trailing-exit 胜率几乎无相关性**。

### 血的教训

2026-04-15 首轮挖掘，选 3 条 top rules 编码为策略回测：

| 策略 | 挖掘预期 | 回测实际 |
|---|---|---|
| weak_momentum_sell M15 | 63% / n=2829 | **17% / PF 0.04** |
| roc_accel_sell M30 | 61% / n=561 | **24% / PF 0.09** |
| squeeze_breakout_buy H1 | 60% / n=53 | **75% / PF 1.23** ✅ |

前两条完全失败，只有"盘整末期→趋势突破"类型（天然契合 trailing）幸存。

### 挖掘候选的正确使用姿势

1. **挖掘分数只是"入场条件质量"的粗筛**，不是策略质量保证
2. 候选规则必须**匹配 exit 模型才考虑编码**：
   - 反转/回吐类（短持仓）→ 挖掘短 forward_return 可近似成立
   - 突破/趋势类（长持仓）→ 挖掘与 trailing exit 天然契合
   - 情境类（条件持续几 bar 就失效）→ 挖掘评分无参考价值
3. **任何候选必须先过"回测胜率≥挖掘胜率 -10pp"的门槛**才能进 Paper
4. `--no-filters` 只能诊断过滤链影响，**不能诊断 exit 模型差异**

### 待整改（F-12 跟踪）— 2026-04-15 状态

- **F-12a** ✅ **已闭环**（commit `cf838d5`）：`DataMatrix` 新增 Triple-Barrier forward_return
  * `barrier_returns_long` / `barrier_returns_short` 字段，9 组默认 RR 网格
  * **研究层实现**：`src/research/core/barrier.py`（向量化 SL/TP/Time 先触，SL 与 TP 同 bar 保守取 SL）+ `src/research/core/data_matrix.py` 自动填充 + `tests/research/test_barrier.py`
  * 规则统计：`src/research/analyzers/rule_mining.py:_compute_barrier_stats_for_rule()` 每条 MinedRule 产 `BarrierStats`（TP/SL/Time 触发分布 + 命中率 + 平均收益）
- **F-12b** ✅ **已闭环**（commit `c0c0387`）：策略可声明 `ExitSpec(mode=BARRIER, ...)` 与挖掘 exit 语义对齐
  * **执行层实现**：`src/trading/positions/exit_rules.py` `evaluate_barrier_exit()`（回测与实盘复用）
  * ⚠️ 两层实现位置不同，互为契约：研究层用于 labeling / gap 诊断，执行层用于真实退出决策
  * **相关**：执行层一致性修复见 [`design/position-state-consistency.md`](design/position-state-consistency.md)；
    已修 bug 记录见 [`codebase-review.md §P0-83/84`](codebase-review.md)；
    未来 R-based 规划见 [`design/r-based-exit-plan.md`](design/r-based-exit-plan.md)
- **F-12c** ⏸️ **推迟至 P3**：Plan B 完成后诊断价值被实质覆盖

### 下次挖掘推荐流程

1. `MiningRunner.run()` 现在会自动填充 `DataMatrix.barrier_returns_*`
2. 产出 Top Rules 时**同时报告**朴素 forward_return 胜率 + barrier_returns 胜率（选 top-3 RR 组合）
3. Top candidate 选择时挑"两者都高"的规则 → 天然避开本次 C-1/I-1 这类 exit 不兼容陷阱
4. 候选编码策略时在 `_exit_spec()` 声明挖掘产出的最优 barrier 参数，回测自动走 barrier 路径

### 2026-04-22 — 本段血教训的后续缓解

详见 [`docs/codebase-review.md §F 2026-04-22 挖掘模块实操级 3 项 Gap 修复`](codebase-review.md)。
摘要：

1. **Gap 1**：`round_trip_cost_pct` 0.04 → 0.08%，反映真实 XAUUSD 往返成本
2. **Gap 2a**：`forward_horizons` `[1,3,5,10]` → `[3,10,30,60]`，覆盖 Chandelier trailing 期望持仓
3. **Gap 2b**：新增 `analyze_barrier_predictive_power` analyzer —— IC 直接基于
   Triple-Barrier 出场收益，与实盘 exit 模型同构。`MiningResult.barrier_predictive_power`
   字段承载结果，选择候选时**优先看 barrier IC** 而非旧 predictive_power.IC
4. **Gap 3**：weekend/holiday gap 自动 mask，排除周五→周一 ~50h 空档的
   forward_return / barrier_return

### barrier candidate 晋级口径（2026-04-27 收紧）

`MiningRunner._rank_findings()` 仅把同时满足以下两条的 barrier finding
推入 Top Findings：

1. `is_significant == True`（permutation/BH-FDR 校正后显著）
2. `mean_return_pct > 0`（cost-after 经济可行 — 已扣 `round_trip_cost_pct`）

显著但 cost-after 负收益的发现作为 **avoidance / reverse-direction
evidence**，不进 promotion 候选。**不叠加** tp_hit_rate vs sl_hit_rate
门控——R:R 偏向 TP 时低胜率组合（如 tp=30%/sl=45% + 3:1 R:R）仍可正
期望，hit-rate ratio 会误杀这类 trend-following 风格。

barrier finding 的 `summary` 字符串包含 `ret=±x.xxxx%` 字段，可在 JSON
产物里直接审计 cost-after 收益。

---

## 系统定位

```
Research (发现假设)
    ↓ 人工编码为策略
Backtest (历史验证)
    ↓ WF + Recommendation
Demo Validation (demo broker 真实下单验证 — 见 ADR-010)
    ↓ 人工确认 + promote deployment.status
Live Trading (active / active_guarded)
```

> **ADR-010 后职责重定位**：原 Paper Trading 模块已删除（详见 ADR-010），
> 该阶段职责由 `demo` environment + `deployment.status = demo_validation`
> 承接——在 demo broker 上真实下单验证组合表现，结果不进 live 但保留
> 完整 trace。promote 到 live 需把 status 改为 `active` / `active_guarded`。

Research 是整条链路的源头——从历史数据中挖掘"指标值 → 未来收益"的统计关系，输出可操作的发现供策略开发使用。

当前 research/mining 默认只产出 `FeatureCandidateSpec` 与 `StrategyCandidateSpec` 这类单策略候选工件，不生成 voting group / ensemble / `strategy="consensus"` 运行时合同。若未来需要组合策略，必须单独定义新工件类型，而不是复用当前单策略主线。

### State Edge / GPU Research Lab（2026-04-30）

新增 `src/research/state_edge/` 作为离线研究实验室，用于训练逐 TF 独立的三分类市场状态概率模型：

- 输出：`long_edge_prob` / `short_edge_prob` / `no_trade_prob`
- 支持 TF：`H4,H1,M30,M15`，但每个 TF 独立训练、独立 artifact、独立验收
- 标签：复用 `DataMatrix.barrier_returns_long/short` 默认 9 组 triple-barrier cost-after return；long/short 取两侧最优正收益方向，否则 `no_trade`
- 特征：只消费 `indicator_series`、FeatureHub 注入特征、hard/soft regime、session；manifest 层排除 `forward/future/barrier/outcome/label`
- 后端：`cpu` 使用 NumPy + scikit-learn；`gpu` 只做 fail-fast CUDA 栈诊断，不静默回退 CPU

CLI：

```bash
python -m src.ops.cli.state_edge_lab \
  --environment live \
  --tf H4,H1,M30,M15 \
  --start 2026-01-01 \
  --end 2026-04-01 \
  --backend cpu \
  --json-output artifacts/state_edge/report.json
```

产物：

- 每个 TF 写出独立 `state_edge_artifact.json`
- artifact 包含 model payload、feature manifest、label summary、OOS 指标、概率分布、top probability bucket 的 cost-after return / hit-rate lift / 样本数

Backtest overlay：

```bash
python -m src.ops.cli.backtest_runner \
  --environment live \
  --tf H1 \
  --start 2026-01-01 \
  --end 2026-04-01 \
  --state-edge-artifact artifacts/state_edge/state-edge-H1-.../state_edge_artifact.json \
  --state-edge-mode shadow
```

- `shadow`：只记录概率覆盖率与方向概率，不改变交易数/PnL
- `filter`：仅在回测中按方向概率过滤入场，可配 `--state-edge-threshold-grid 0.50,0.55,0.60,0.65,0.70`
- 该 overlay 通过 `BacktestEngine(state_edge_overlay=...)` 正式端口注入，不探测私有 artifact/model 字段
- 第一阶段不接入 demo/live runtime，不影响真实下单

#### 序列 K 线形态模型（实验分支）

`state_edge_lab` 支持 `--model-kind sequence_mlp`，用于把最近 N 根 bar 的
OHLC K 线形态与当前/历史可见特征拼成序列窗口：

```text
[sample_count, sequence_window, feature_count]
```

该分支会额外加入当前 bar 可见的 K 线形态特征：

- `candle.log_return`
- `candle.body_ratio`
- `candle.upper_wick_ratio`
- `candle.lower_wick_ratio`
- `candle.close_location`
- `candle.range_pct`

并生成 `shape_neighbors` 报告，用于检索当前窗口之前的历史相似形态，输出 analog 的标签分布与 cost-after return。第一阶段 `shape_neighbors` 只作为审查证据，不参与交易过滤。

实测注意事项：

- RTX 2060 SUPER + Windows + PyTorch CUDA 下，`sequence_tcn`/`Conv1d` 训练异常慢，不作为推荐路径。
- `sequence_mlp` 更贴近 GPU 的矩阵乘法优势；合成 20k bars、window=64、57 特征、4 epochs 下，CPU 约 9.97s，GPU 约 3.39s。
- H1 小窗口 smoke 只证明链路可用；`filter@0.50` 曾出现 PF 下降，不能作为有效模型验收。
- H1 `2026-03-01` 到 `2026-03-15`、`sequence_window=32`、`epochs=2`、`filter@0.50/0.55` 分段复测结论为 `rejected`：baseline 18 trades / PF 1.11 / expectancy 0.84；filter 后 13 trades / PF 0.429 / expectancy -4.25，原因是 `pf_expectancy_degraded`。

#### 序列评估流水线

序列模型验收入口是 research-only CLI：

```powershell
python -m src.ops.cli.state_edge_sequence_eval `
  --environment live `
  --tf H1 `
  --start 2026-03-01 `
  --end 2026-04-15 `
  --backend gpu `
  --model-kind sequence_mlp `
  --sequence-window 64 `
  --epochs 8 `
  --batch-size 512 `
  --threshold-grid 0.50,0.55,0.60,0.65,0.70 `
  --include-demo-validation `
  --json-output artifacts/state_edge_sequence_eval_h1.json
```

流水线会依次训练 artifact、运行 baseline backtest、运行 `shadow`、按阈值网格运行 `filter`，然后输出 `accepted` / `refit` / `rejected`：

- `accepted`：PnL、PF、expectancy 均不退化，且至少一个交易增量指标改善；max DD 没有比 baseline 恶化超过 10%。
- `refit`：交易数不足或没有明确增量，应该调整窗口、样本区间、标签或训练轮数。
- `rejected`：回撤破坏验收边界，或 PnL / expectancy 出现退化。

该状态只代表 Research + Backtest overlay 阶段的离线判断。即使某个 TF 被标为 `accepted`，也不能直接进入 demo/live；后续仍需人工审查、跨区间复核和独立的运行时接入设计。

当前 `H1` live 配置下没有允许 H1 的 `active` 策略，H1 research 评估需要加 `--include-demo-validation` 才会纳入 demo-validation 策略做 overlay 对比。该参数只影响离线回测准入，不改变真实 demo/live runtime。

`state_edge_sequence_eval` 会在 artifact、baseline、shadow、每个 filter 阈值和最终 threshold report 阶段写入 `--json-output` checkpoint。长窗口任务即使被人工中断，也应先查看该 JSON 的 `results[].stage` 判断卡点。

在进入 baseline/shadow/filter 之前，`state_edge_sequence_eval` 会先执行 artifact quality gate：

- `min_oos_samples`：默认 30，OOS 样本过少直接 `refit`。
- `min_top_bucket_samples`：默认 5，long/short top probability bucket 样本过少直接 `refit`。
- `min_label_class_samples`：默认 10，三分类任一类别样本过少直接 `refit`。
- 样本足够但 long/short top bucket 都没有正向 cost-after return 或 hit-rate lift 时，直接 `rejected`。

该门禁的目的不是替代 backtest 验收，而是避免把明显不可审查的 artifact 送入昂贵的 overlay 网格。确需强制跑 overlay 时可显式加 `--skip-artifact-quality-gate`，但报告必须说明原因。

长窗口训练探索应先使用 quality-only 模式：

```powershell
python -m src.ops.cli.state_edge_sequence_eval `
  --environment live `
  --tf H1 `
  --start 2026-01-01 `
  --end 2026-04-15 `
  --backend gpu `
  --model-kind sequence_mlp `
  --sequence-window 64 `
  --epochs 8 `
  --batch-size 512 `
  --artifact-quality-only `
  --include-demo-validation `
  --json-output artifacts/state_edge_quality_h1_long.json
```

`--artifact-quality-only` 会在 artifact quality gate 后停止，即使质量通过也不会继续跑 baseline/shadow/filter。只有 quality gate 通过后，才单独发起 overlay 网格评估。

H1 长窗口 quality-only 实测：

- 区间：`2026-01-01` 到 `2026-04-15`
- 模型：`sequence_mlp`，`sequence_window=64`，`epochs=8`，`backend=gpu`
- 结果：`artifact_quality.status=accepted`，耗时约 280.3 秒
- OOS：376 samples，accuracy 0.543
- 标签：long 831 / short 759 / no_trade 65
- top bucket：long 73 samples，mean cost-after return 0.0265，hit-rate lift 0.0712；short 72 samples，mean cost-after return 0.00365，hit-rate lift -0.0248

该结果说明长窗口样本量已经足够进入下一阶段，但边际主要集中在 long 侧；下一步不应盲目跑双向 filter 网格，应先评估 direction-aware overlay（例如 long-only）。

方向感知 overlay 通过 `--state-edge-directions` 控制，仅影响 backtest overlay：

```powershell
python -m src.ops.cli.backtest_runner `
  --environment live `
  --tf H1 `
  --start 2026-01-01 `
  --end 2026-04-15 `
  --include-demo-validation `
  --state-edge-artifact artifacts/state_edge_sequence_eval/state-edge-H1-20260430031204-ab24a6/state_edge_artifact.json `
  --state-edge-mode filter `
  --state-edge-threshold-grid 0.50 `
  --state-edge-directions buy `
  --json-output artifacts/state_edge_overlay_h1_long_buy_only.json
```

`--state-edge-directions buy` 表示只过滤 buy 入场，sell 入场只记录概率但不被 State Edge 阻断；`sell` 同理，默认 `buy,sell`。

#### Overlay 验收报告（2026-05-02）

单独跑 baseline / shadow / filter 后，必须用 `state_edge_overlay_report` 汇总，禁止只凭人工浏览多份 JSON 判断：

```powershell
python -m src.ops.cli.state_edge_overlay_report `
  --baseline artifacts/state_edge_overlay_h1_20260101_20260415_baseline.json `
  --shadow artifacts/state_edge_overlay_h1_20260101_20260415_shadow_buy.json `
  --filters `
    artifacts/state_edge_overlay_h1_20260101_20260415_filter_buy_050.json `
    artifacts/state_edge_overlay_h1_20260101_20260415_filter_buy_grid_055_070.json `
  --json-output artifacts/state_edge_overlay_h1_20260101_20260415_validation_report.json
```

报告内容：

- `shadow_check`：确认 shadow 不改变 baseline 的 trades / PnL / PF / expectancy / DD。
- `threshold_report`：逐阈值输出 `pnl_delta / pf_delta / expectancy_delta / max_dd_delta / trade_delta` 与 `accepted/refit/rejected`。
- `filter_diagnostics`：输出 state edge blocked 数、方向/原因分布、逐笔 `blocked_entry_events`（若 filter backtest 由新版引擎生成）、`blocked_trade_attribution`（若 baseline JSON 含 `raw_results.trades`），以及 baseline vs filter 的 strategy/regime 聚合差异。

注意：新版 filter backtest 会在 `execution_summary.blocked_entry_events` 写出正式阻断事件，报告此时标记 `diagnostic_scope=exact_blocked_entries`，可直接回答“哪一笔入场被 State Edge 阻断、对应策略/方向/概率/阈值是什么”。旧 JSON 没有该字段时仍会降级为 `aggregate_delta`，只能说明 filter 后最终交易分布变化，不能反推出逐笔被挡交易。

`blocked_trade_attribution` 只做保守精确匹配：`blocked_event.bar_time == baseline_trade.entry_time` 且 `strategy/direction` 相同。匹配成功表示这条 blocked event 在 baseline 中确实形成过成交；匹配不到只说明“filter 链路阻断了该入场意图，但 baseline 中没有同键成交”，不得强行推断其盈亏。

H1 `2026-01-01` 到 `2026-04-15` long-only overlay 实测：

- baseline：53 trades / PnL 581.49 / PF 2.299 / expectancy 10.97 / DD 3.69%。
- shadow@0.50：与 baseline 完全一致，`shadow_check.status=passed`。
- filter buy-only@0.50：45 trades / PnL 438.94 / PF 2.315 / expectancy 9.75 / DD 3.69%，判定 `rejected`，原因 `return_expectancy_degraded`。
- filter buy-only@0.55-0.70：44 trades / PnL 432.14 / PF 2.298 / expectancy 9.82 / DD 3.69%，判定 `rejected`。
- 正式报告：`artifacts/state_edge_overlay_h1_20260101_20260415_validation_report.json`，整体状态 `rejected`。
- 逐笔归因报告：`artifacts/state_edge_overlay_h1_20260101_20260415_validation_report_050_with_attribution.json`。`filter@0.50` 的 19 条 blocked events 中，8 条能精确匹配 baseline 成交，合计 PnL +140.29；其中 2 赢 6 亏，但两笔赢家贡献 +233.25，说明该 artifact 当前会挡掉关键盈利单。

结论：该 H1 长窗口 artifact 质量门通过，但交易增量验收失败，应标记为 overlay `rejected/refit`，不得进入多 TF 扩展或 runtime 设计。

#### Entry Meta-Label Overlay Lab（2026-05-03）

Entry Meta 是 State Edge 之后的 trade-aware 研究层，职责不是重新预测市场状态，而是判断已有策略 entry 是否值得保留：

- State Edge 预测市场状态概率：`long_edge_prob` / `short_edge_prob` / `no_trade_prob`。
- Entry Meta 预测交易入场保留概率：`take_entry_prob` / `block_entry_prob`，用于回答“这条已由策略产生的 entry 是否应被保留”。

第一阶段以 baseline backtest 的 `raw_results.trades` 作为 entry 样本与 PnL 标签来源，同时基于同一时间窗的 DataMatrix / FeatureHub / `indicator_series` / regime / session 提取当前或历史可见特征。不读取 demo/live runtime 状态，也不把模型接入真实下单链路。该实验只进入 Research + Backtest overlay，用离线回测证明其对既有策略 entry 的过滤价值。

正式入口：

- `entry_meta_lab`：从 baseline `raw_results.trades` 构建 trade-aware 训练样本并写出 Entry Meta artifact。
- `backtest_runner --entry-meta-artifact/--entry-meta-mode/--entry-meta-threshold-grid`：在 backtest overlay 中以 `shadow` 或 `filter` 模式评估 Entry Meta。
- `entry_meta_overlay_report`：汇总 baseline / shadow / filter，输出验收结论与阻断归因。

验收必须使用 `entry_meta_overlay_report`，不能只人工浏览多份 JSON。报告必须检查 `blocked_trade_attribution`，确认 Entry Meta 是否挡掉 baseline 中的大盈利单；若过滤收益来自减少交易数但同时挡掉关键盈利单，应按交易增量证据判定 `rejected/refit`，不得进入 runtime 设计。

Backtest 侧通过公开 `entry_meta_overlay` 端口注入，并通过 `record_blocked_entry()` 写出正式阻断事件；报告只消费 `execution_summary.blocked_entry_events` 与 baseline `raw_results.trades` 做保守归因，不探测引擎、portfolio 或 overlay 的私有字段。

动态打分端口（2026-05-03）：

- 新版 Entry Meta artifact 保存 JSON-native `logistic_regression_v1` scorer payload；overlay 先查历史 prediction，未命中时使用当前 signal entry 的 feature context 动态计算 `take_entry_prob / block_entry_prob`。
- `feature_context` 只来自 backtest 当前公开事实：bar time/index、strategy、direction、confidence、bar close、当前 indicators、hard regime、session；不读取 engine/portfolio/model 私有字段。
- 动态打分失败不会阻断交易，会通过 `missing_by_reason`、`score_source_counts`、`dynamic_score_failures` 进入 overlay report。
- 该能力仍只属于 Research + Backtest overlay，不接入 demo/live runtime。

已知限制：

- session 无法从当前 SessionFilter 判定时会写为 `unknown`，对应 artifact 若无 `unknown` 映射会降低动态打分覆盖率。
- pending-entry 当前仍在 signal 产生时用 signal bar/time/close 构建 `feature_context` 并打分，不会等到后续 fill time/price 重新打分。

---

## Research → Indicator / Signal 闭环

当前实现不再把 research 结果停留在文本摘要，而是分成两条正式晋升链路：

```text
MiningRunner
  -> Cross-TF Analysis
  -> FeatureCandidateSpec
  -> IndicatorPromotionDecision
  -> promoted shared indicator (IndicatorConfig / registry / pipeline)
  -> FeaturePromotionReport
  -> downstream strategy candidate / backtest / WF
```

```text
MiningRunner
  -> Cross-TF Analysis
  -> StrategyCandidateSpec
  -> Backtest ValidationDecision
  -> strategy_deployment.<strategy>
  -> demo_validation / active_guarded / active   # ADR-010 后命名
```

### 候选工件

`src/research/strategies/candidates.py` 现在会产出 `StrategyCandidateSpec`，核心字段包括：

- `candidate_id`
- `target_timeframe`
- `allowed_timeframes`
- `robustness_tier`
- `promotion_decision`
- `why / when / where` 条件骨架
- `thresholds`
- `evidence_types / evidence`
- `validation_gates`
- `research_provenance`

`src/research/features/candidates.py` 现在会产出 `FeatureCandidateSpec`，核心字段包括：

- `candidate_id`
- `feature_name`
- `formula_summary`
- `dependencies / runtime_state_inputs`
- `live_computable`
- `compute_scope`
- `robustness_tier`
- `strategy_roles`
- `promotion_decision`
- `research_provenance`

`IndicatorPromotionDecision` 当前固定为：

- `reject`
- `refit`
- `research_only`
- `promote_indicator`
- `promote_indicator_and_strategy_candidate`

注意：`promote_indicator` 只表示“进入共享指标流可计算”，不表示默认允许 live；下游策略仍要走 `StrategyCandidateSpec -> ValidationDecision -> strategy_deployment`。

### 跨 TF 分层

- `robust`：2 个及以上 TF 显著且方向一致，可进入普通晋升路径。
- `tf_specific`：只在目标 TF 显著，或仅目标 TF 有效但邻近 TF 不翻转；允许晋升，但必须带永久护栏。
- `divergent`：2 个及以上 TF 显著但方向翻转，不能直接晋升为策略，必须拆成新的独立假设重新研究。

### 单 TF 策略护栏

`tf_specific` 候选不能直接走普通 `active` 路径，当前正式部署状态机为：

- `candidate`：代码已注册，但不参与 live runtime。
- `demo_validation`（ADR-010 后命名，原 `paper_only`）：仅 demo-main 实例装配，在 demo broker 真实下单评估组合表现。live-main 拒绝装配。
- `active_guarded`：允许 live，但必须保持永久护栏（小手数 + demo 镜像）。
- `active`：仅 `robust` 策略允许进入（live 完整手数 + demo 持续镜像）。

`active_guarded` 的硬约束如下：

- 必须声明 `locked_timeframes`
- 必须声明 `locked_sessions`
- `min_final_confidence` 至少高于同 TF 基线 `+0.05`，且不低于 `0.50`
- `max_live_positions = 1`
- `require_pending_entry = true`
- `demo_validation_required = true`（§0dh 后字段名同步去 Paper 化；§0dj 起为合同正式校验项，旧字段 `paper_shadow_required` 已物理移除，配置中保留旧名将让护栏静默失效）

运行时不再允许用 “`regime_affinity.* = 0`” 这种隐式冻结方式代替部署状态；冻结、候选、demo_validation、guarded 都必须通过 `strategy_deployment.<strategy>` 正式声明。

### 首个真实 indicator promotion 结果

本轮已经交付第一条真实的 `feature -> indicator -> strategy consumer -> validation` 证据链：

- research feature：`derived.momentum_consensus`
- promoted shared indicator：`momentum_consensus14`
- downstream strategy consumer：`structured_trend_h4_momentum`
- deployment contract：`demo_validation + tf_specific + locked_timeframes=H1 + locked_sessions=london,new_york`（ADR-010 后命名）

该策略 consumer 沿用 `StructuredTrendContinuation` 骨架，只把 `momentum_consensus14` 接入 `why` 层和 lineage 元数据，不复制第二套策略框架。当前验证产物位于：

- `data/artifacts/feature-candidates/xauusd_diagnostic_feature_scan_20251230_20260330.json`
- `data/artifacts/feature-backtests/xauusd_h1_structured_trend_h4_momentum_research_20251001_20260330.json`
- `data/artifacts/feature-backtests/xauusd_h1_structured_trend_h4_momentum_execution_20251001_20260330.json`
- `data/artifacts/feature-walkforward/xauusd_h1_structured_trend_h4_momentum_wf_20251001_20260330.json`
- `data/artifacts/indicator-promotions/momentum_consensus14_xauusd_h1_20251001_20260330.json`

当前结论不是“直接晋升 live”，而是 `refit`：

- research backtest 仅 `7` 笔交易，`PF=0.863`
- execution feasibility 全部被 `below_min_volume_for_execution_feasibility` 拒绝
- Monte Carlo 未通过
- Walk-Forward `consistency_rate=40%`

因此，indicator promotion 已成立，但 downstream strategy 仍只处于 `demo_validation / refit` 阶段（ADR-010 后命名），不能把 promoted indicator 的存在误解成"策略已经可上线"。

### ValidationDecision 的双结果语义

由于回测已经正式拆成 `research` 与 `execution_feasibility` 两种执行语义，当前 `ValidationDecision` 也同步支持双结果输入：

- `research backtest result`：负责统计指标、Monte Carlo、历史覆盖期判断
- `execution feasibility result`：负责 accepted/rejected entry ratio 与拒单原因判断

不能再假设“一份 BacktestResult 同时承载研究指标和执行可行性结论”。这条合同用于避免把研究型回测误当成真实可执行性结果。

---

## 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│  MiningRunner.run()                                         │
│                                                             │
│  ① 数据加载                                                  │
│     DB OHLC → warmup + test bars                            │
│     pipeline.compute() 逐 bar 计算 28 个指标                 │
│                                                             │
│  ② DataMatrix 构建                                           │
│     对齐的 [n_bars × indicators × regime × forward_returns]  │
│     + Session 标签 + MFE/MAE + 70/30 Train/Test (含 gap)    │
│                                                             │
│  ③ 特征工程                                                  │
│     FeatureEngineer.enrich() → 3 个研究特征注入              │
│                                                             │
│  ④ 三大分析器（漏斗）                                        │
│     Predictive Power → Threshold Sweep → Rule Mining         │
│                                                             │
│  ⑤ _rank_findings() → Top 30 排名                           │
│                                                             │
│  ⑥ Candidate Discovery（robust / tf_specific / divergent）  │
│                                                             │
│  ⑦ MiningResult / StrategyCandidateSpec 输出 + 持久化       │
└─────────────────────────────────────────────────────────────┘
```

---

## 统计工具层（core/statistics.py）

所有分析器共享的纯统计原语，集中在 `src/research/core/statistics.py`，单一职责：可复用的统计计算，不涉及交易逻辑或 DataMatrix 结构。

| 函数 | 用途 |
|------|------|
| `auto_block_size(values)` | 基于 ACF 衰减自适应选择 block size（替代硬编码 10） |
| `effective_sample_size(n, acf)` | Kish 公式：自相关序列的有效样本量 |
| `effective_n_for_overlapping_windows(n, w, s)` | 重叠窗口的有效独立窗口数 |
| `minimum_detectable_ic(n, alpha, power)` | Fisher z 变换：给定样本量可检测的最小 IC |
| `required_sample_size(target_ic)` | 反向计算：检测目标 IC 所需样本量 |
| `binomial_test_p(k, n, p0)` | 双尾 binomial 精确检验（scipy / 正态近似回退） |
| `block_shuffle(values, block_size, rng)` | Block permutation 共用 shuffle 原语 |
| `newey_west_se(residuals)` | Newey-West HAC 自相关一致标准误 |
| `estimate_autocorrelation(values)` | 序列 ACF(0..max_lag) 计算 |

**设计原则**：各分析器不应各自实现 block_shuffle、ACF 等统计工具——统一从 `core/statistics.py` 导入。

---

## 三大分析器（核心）

三者是**漏斗关系**——从粗筛到精定位到组合发现：

### 1. Predictive Power — "谁有预测力？"

**筛选阶段**：从几百个 (indicator.field × horizon × regime) 组合中筛出有统计关系的指标。

```
输入: 每个组合的 (indicator_values, forward_returns) 对
方法:
  · Spearman 秩相关 (IC)
  · Pearson 相关
  · 方向命中率（指标 > 中位数时正收益比例）
  · Rolling IC + 校正 IR（重叠窗口偏差校正，eff_n_windows）
  · 自适应 Block Permutation 排列检验 (1000 次，block size 由 ACF 自动确定)
  · BH-FDR 批量显著性校正（使用 n_total_attempted 计数，含被过滤的组合）
  · 统计效力报告（min_detectable_ic，基于 Fisher z 变换）

输出示例:
  rsi14.rsi [ranging] IC=+0.30, IR=0.98, perm_p=0.01, min_ic=0.12, 10-bar
  → "RSI 在 ranging 市场对 10 bar 后的收益有稳定预测力，可检测 IC ≥ 0.12"
```

**关键指标**：
- `IC` (Information Coefficient)：指标值与未来收益的秩相关，|IC| > 0.05 有意义
- `IR` (Information Ratio)：IC 的稳定性 = mean(IC) / std_corrected(IC)，IR > 0.5 = 稳定
  - std_corrected 使用 `effective_n_for_overlapping_windows()` 校正重叠窗口导致的 IR 虚高
- `perm_p`：排列检验 p-value，< 0.05 排除 data snooping（BH-FDR 校正优先使用此值）
- `min_detectable_ic`：当前样本量下可检测的最小效应量，辅助判断 "无显著性" 是真无效还是样本不足

### 2. Threshold Sweep — "最佳阈值是多少？"

**定量阶段**：知道某指标有预测力后，找到最优的买入/卖出阈值。

```
输入: 单个 indicator.field × horizon × regime
方法:
  · 50 点网格扫描（5th~95th 分位数范围）
  · 每个阈值计算: 命中率 / 期望收益 / Sharpe / avg_win / avg_loss / profit_factor
  · 期望收益 = hit_rate × avg_win - (1-hit_rate) × avg_loss（非简单 mean_return）
  · 信号聚集去重（连续触发只计第一次）
  · 5-fold 时序 CV 一致性检验（支持 expanding / sliding 模式）
  · 自适应 Block Permutation 排列检验 (200 次，block size 由 ACF 确定)
  · 显著性 = perm_p ≤ 0.05 AND CV ≥ 0.60
  · 测试集外验证

输出示例:
  rsi14.rsi [ranging] BUY <= 35.20  hit=58.3%  exp=0.0012  CV=78%  perm_p=0.02  SIG
  → "RSI 跌破 35.2 时买入，期望收益 0.12%，统计显著"
```

**关键指标**：
- `CV`：交叉验证一致性，≥ 0.60 才算稳健，< 0.60 标记 FRAGILE
- `perm_p`：最优阈值的排列检验，排除 50×2=100 次检验的 data snooping
- `SIG`：perm_p ≤ 0.05 AND CV ≥ 0.60 双重门控通过
- `expectancy`：正确的期望收益公式，区分胜率型 vs 盈亏比型策略
- `profit_factor`：total_wins / total_losses，衡量盈亏比

### 3. Rule Mining — "什么组合最赚？"

**组合阶段**：用决策树发现多条件交互效应，并通过三层统计检验验证可靠性。

```
输入: 全部无量纲指标字段（含 8 个新指标 + 3 个研究特征）
方法:
  · DecisionTreeClassifier (max_depth=3, sample_weight=|return|)
  · 仅使用无量纲字段（_DIMENSIONLESS_FIELDS 过滤绝对价格）
  · DFS 遍历叶节点 → 提取盈利路径
  · 每个条件自动映射到 Why/When/Where 角色
  · Train hit_rate ≥ 55% + Test hit_rate ≥ 52% 才保留
  · [统计检验] 三层显著性验证：
    ① Permutation Test：打乱标签后重训树，构建 null 分布，验证 hit_rate 是否显著
    ② CV Consistency：跨 fold 检查同一规则模式是否反复出现（条件归一化匹配）
    ③ Binomial Test：测试集上用精确二项检验验证 hit_rate > 50%

输出示例:
  IF adx14.adx > 28.5 AND rsi14.rsi <= 35.2 THEN buy
    Why:   adx14.adx > 28.5    (方向确认)
    When:  rsi14.rsi <= 35.2   (入场时机)
    train=62.9%/n=35  test=57.1%/n=15
    perm_p=0.03  binom_p=0.04  cv=0.60  SIG
  → 直接对应 StructuredStrategyBase 的 _why() + _when()
```

**规则显著性判定**（双路径，任一通过即标记 `is_significant`）：
- **路径 A**：perm_p ≤ 0.05 AND cv_consistency ≥ threshold
- **路径 B**：binomial_p ≤ 0.05 AND test_hit_rate ≥ min_test_hit_rate

**角色映射**：
| 角色 | 指标 | 策略方法 |
|------|------|---------|
| Why（方向确认） | ADX, MACD, Supertrend, EMA | `_why()` 硬门控 |
| When（入场时机） | RSI, StochRSI, CCI, ROC | `_when()` 硬门控 |
| Where（结构位） | Bollinger, Donchian, Keltner, ATR | `_where()` 软门控 |

---

## 数据层

### DataMatrix

所有分析器的统一输入，一次构建多次消费：

| 字段 | 类型 | 说明 |
|------|------|------|
| `bar_times/opens/highs/lows/closes/volumes` | List[float] | 原始 OHLC |
| `indicators` | List[Dict] | 每 bar 的指标快照 |
| `indicator_series` | Dict[(ind,field), List] | 扁平化指标序列 |
| `regimes` | List[RegimeType] | 每 bar 的 Regime |
| `soft_regimes` | List[Dict] | Regime 概率分布 |
| `forward_returns` | Dict[horizon, List] | 前瞻收益（next-bar open 入场，扣成本） |
| `forward_mfe_mae` | Dict[horizon, List] | 最大顺向/逆向偏移 |
| `sessions` | List[str] | 时段标签 (asia/london/new_york) |
| `train_end_idx / split_idx` | int | Train/Gap/Test 边界 |

### forward_return 计算

```python
# 入场价 = 下一根 bar 的 open（非 close，避免前瞻偏差）
# 扣除往返交易成本 (默认 0.04%)
entry = opens[i + 1]
exit = closes[i + h]
return = (exit - entry) / entry - round_trip_cost
```

### Train/Test 分割

```
[0 ──── train_end_idx) [gap = max_horizon bars) [split_idx ──── n_bars)
         训练集                   排除区                    测试集
```

Gap 区间防止 forward_return 在 train/test 之间信息泄露。

---

## 三层指标架构

```
Layer 1: 正式指标 (29 个, indicators.json)
  · 注册即持久化（OHLC.indicators JSONB）
  · 实盘/回测/研究全系统共享
  · 趋势/动量/波动/量能/价格行为/复合

Layer 2: 研究特征 (~85 个, features/hub.py + 6 Providers)
  · 仅 DataMatrix 生命周期内存在
  · 可以依赖 Layer 1 指标，也可以依赖 regime / soft_regime 等研究态上下文
  · 由 FeatureHub 聚合 6 个 Provider 的输出，全部 numpy 向量化

Layer 3: 分析器输出 → MiningResult → top_findings
```

**判定准则**：
- 实盘策略能用 → Layer 1 指标
- 依赖跨指标组合 + 运行时状态 → Layer 2 研究特征
- 通过挖掘验证有效 → 可晋升为 Layer 1

### 半自动晋升流程

首轮实现采用“半自动晋升”，明确禁止 research 结果直接写入运行时：

```text
research feature
  -> FeatureCandidateSpec
  -> IndicatorPromotionDecision
  -> 手工实现正式 indicator 函数
  -> indicators.json 注册
  -> research / backtest / signal 三路复用验证
  -> FeaturePromotionReport
```

当前首个按该流程落地的新共享指标为：

- `momentum_consensus14`
  - 来源：`derived.momentum_consensus`
  - 公式：`sign(macd.hist) + sign(rsi14-50) + sign(stoch_rsi14-50)` 的 3 路动量投票
  - 用途：已接入 `structured_breakout_follow` 作为方向确认硬门控

---

## 过拟合防护（7 层）

| 层 | 防护 | 实现 |
|----|------|------|
| L1 | Train/Test 分割 | 70/30 + gap = max(horizons) bars |
| L2 | BH-FDR 多重校正 | 使用 `n_total_attempted`（含被过滤组合）校正 FDR ≤ 5%；优先使用排列检验 p-value |
| L3 | 时序交叉验证 | 5-fold，支持 expanding（训练窗口逐步扩大）和 sliding（固定窗口滑动）两种模式 |
| L4 | 最小样本量 | n ≥ 30 per bucket |
| L5 | 效应量门槛 | \|IC\| ≥ 0.05 or hit_dev ≥ 3% |
| L6 | Block Permutation | IC: 1000 次 / 阈值: 200 次 / 规则: 200 次；自适应 block size（ACF 衰减确定） |
| L7 | 统计效力分析 | 每个结果报告 `min_detectable_ic`，区分"真无效"和"样本不足" |

### 自适应 Block Size

传统做法硬编码 `block_size=10`，无法适应不同 TF 的自相关结构。`auto_block_size()` 基于 ACF 衰减长度自动选择：

```
算法: 找到 ACF 首次降至 |acf| < 0.05 的 lag → 取该 lag 作为 block size
  M5 数据自相关衰减快 → block_size ≈ 5-8
  H1 数据自相关持续久 → block_size ≈ 15-30
  D1 数据自相关更久   → block_size ≈ 30-50
```

### BH-FDR 改进

旧实现：仅对"存活"到 BH-FDR 阶段的结果做校正，低估了真实检验数量。
新实现：`n_total_attempted` 追踪所有 (indicator, horizon, regime) 组合数（含因样本不足/常量值被过滤的），传入 `benjamini_hochberg_fdr()` 做准确校正。

### Rolling IC IR 校正

当 `step < window_size` 时（默认 step=1），相邻窗口共享大量数据，IR 被严重高估。
校正公式：`std_corrected = std_ic × sqrt(n_windows / effective_n_windows)`

### 规则挖掘三层检验

| 检验 | 方法 | 用途 |
|------|------|------|
| Permutation Test | 打乱标签重训树 200 次，构建 null 分布 | 排除训练集 data snooping |
| CV Consistency | 规则条件归一化后跨 fold 匹配，计算出现频率 | 验证规则稳定性 |
| Binomial Test | 测试集 hit_rate 的精确二项检验 (H0: p=0.5) | 验证泛化能力 |

---

## 多 TF 挖掘准则

**必须多 TF 挖掘**，不得在单 TF 上得出结论：

```bash
python -m src.ops.cli.mining_runner --tf M15,M30,H1 --compare
```

### 信号分类

| 类型 | 定义 | 可信度 |
|------|------|--------|
| **Robust** | 2+ TF 显著 + 方向一致 | 高 → 可纳入策略开发 |
| **Divergent** | 2+ TF 显著但方向翻转 | 中 → 各 TF 独立评估 |
| **TF-specific** | 仅 1 TF 显著 | 低 → 可能是统计噪音 |

### 最佳 TF 推荐

```
score = |IC| × (1 + IR_bonus) × perm_bonus × sample_bonus

IR_bonus = max(0, IR × 0.3)
perm_bonus = 1.2 if perm_p < 0.05 else 1.0
sample_bonus = min(1.0, n_samples / 200)
```

---

## 配置参考

`config/research.ini`：

```ini
[research]
forward_horizons = 1,3,5,10
warmup_bars = 200
train_ratio = 0.70
round_trip_cost_pct = 0.04

[overfitting]
correction_method = bh_fdr        # bonferroni | bh_fdr
min_samples = 30
cv_folds = 5
cv_consistency_threshold = 0.60
cv_mode = expanding               # expanding | sliding

[threshold_sweep]
sweep_points = 50
target_metric = expectancy         # hit_rate | mean_return | sharpe | expectancy
# expectancy = hit_rate × avg_win - (1-hit_rate) × avg_loss
per_regime = true
n_permutations = 200               # block size 由 auto_block_size() 自适应确定
permutation_significance = 0.05

[predictive_power]
significance_level = 0.05
per_regime = true
rolling_ic_enabled = true
rolling_ic_window = 60             # IR 已校正重叠窗口偏差
min_ir_threshold = 0.5
permutation_test_enabled = true    # p-value 优先用于 BH-FDR 校正
n_permutations = 1000

[rule_mining]
max_depth = 3
min_samples_leaf = 30
min_hit_rate = 0.55
min_test_hit_rate = 0.52
max_rules = 20
dimensionless_only = true
n_permutations = 200               # 打乱标签后重训树
permutation_significance = 0.05
cv_folds = 5                       # 跨 fold 规则一致性
cv_consistency_threshold = 0.40

[feature_engineering]
enabled = true
features =                         # 空=全部内置特征

[feature_providers]
enabled_providers = temporal,microstructure,cross_tf,regime_transition,session_event,intrabar
                                   # 逗号分隔；空=全部启用
```

---

## 关键文件

| 文件 | 用途 |
|------|------|
| `src/research/orchestration/runner.py` | MiningRunner 编排器 |
| `src/research/core/data_matrix.py` | DataMatrix 构建 |
| `src/research/core/statistics.py` | 纯统计原语（ACF/block shuffle/效力分析/binomial/Newey-West） |
| `src/research/analyzers/predictive_power.py` | IC + Rolling IC + 排列检验 + BH-FDR + 效力分析 |
| `src/research/analyzers/threshold.py` | 阈值扫描 + expectancy + 排列检验 + 信号去重 |
| `src/research/analyzers/rule_mining.py` | 决策树规则挖掘 + 排列检验 + CV 一致性 + binomial 检验 |
| `src/research/features/hub.py` | FeatureHub：特征编排入口，聚合 6 个 Provider 的输出 |
| `src/research/features/{temporal,microstructure,...}/` | Feature Providers：6 个模块化特征生产者 |
| `src/research/core/overfitting.py` | BH-FDR + 排列检验 + CV 工具（expanding/sliding） |
| `src/research/analyzers/multi_tf_aggregator.py` | 跨 TF 一致性分析（robust / divergent / tf_specific 分类） |
| `src/research/core/config.py` | 配置加载（含 RuleMiningConfig） |
| `src/research/core/contracts.py` | 结果数据模型与候选工件契约 |
| `src/ops/cli/mining_runner.py` | CLI 工具 |
| `config/research.ini` | 配置文件 |

### 当前目录边界

- `src/research/core/`：公共基础层，承载配置、DataMatrix、统计工具、跨 TF 分析与研究契约。
- `src/research/analyzers/`：共享证据引擎，只负责统计分析，不区分指标路径或策略路径。
- `src/research/features/`：FeatureHub + 6 Feature Providers + feature candidate + indicator promotion 路径。
- `src/research/strategies/`：strategy candidate 发现路径。
- `src/research/orchestration/`：MiningRunner 编排入口。

---

## Feature Providers（模块化特征层）

原 `FeatureEngineer`（单文件 God class，~1250 行，~21 个特征）已重构为 **FeatureHub + 6 个独立 Provider**，总特征数从 ~31 扩展至 ~85，全部 numpy 向量化。

### FeatureHub

`src/research/features/hub.py` — 唯一入口，替代原 `FeatureEngineer.enrich()`。

- 聚合所有启用的 Provider 输出，注入 `DataMatrix`
- 通过 `FeatureProviderProtocol` 接口与各 Provider 解耦
- 支持按 `[feature_providers] enabled_providers` 配置启用子集

### 6 个 Provider 一览

| Provider | 路径 | 特征数 | 覆盖维度 |
|----------|------|--------|---------|
| `TemporalProvider` | `features/temporal/` | ~33 | 时间周期、动量演化、趋势持续性 |
| `MicrostructureProvider` | `features/microstructure/` | ~21 | K 线形态、蜡烛体比率、价格位置 |
| `CrossTFProvider` | `features/cross_tf/` | ~8 | 跨周期趋势一致性、HTF 对齐 |
| `RegimeTransitionProvider` | `features/regime_transition/` | ~11 | Regime 切换速度、稳定性、熵 |
| `SessionEventProvider` | `features/session_event/` | ~7 | 亚欧美时段、开盘区间突破 |
| `IntrabarProvider` | `features/intrabar/` | ~5 | 盘中 bar 进度、成交量分布 |

### CLI 用法

```bash
# 默认：全部 6 个 Provider
python -m src.ops.cli.mining_runner --tf M15,M30,H1 --compare

# 指定部分 Provider（用于快速调试或对比实验）
python -m src.ops.cli.mining_runner --tf H1 --providers temporal,microstructure
```

### 分组 FDR 校正（by_provider）

BH-FDR 校正现支持按 Provider 分组：每个 Provider 内部独立校正，避免跨维度特征数量稀释显著性阈值。可在 `[overfitting]` 中配置：

```ini
[overfitting]
fdr_group_by = provider   # none | provider（默认 none=全局统一校正）
```

### 边界约束

- `FeatureHub` 不承载特征计算逻辑，只负责编排和聚合
- 每个 Provider 只依赖 `DataMatrix` 输入，不访问其他 Provider 的输出（无隐式跨 Provider 依赖）
- Provider 新增不需要修改 `FeatureHub` 核心——只需实现 `FeatureProviderProtocol` 并在配置中启用

### 模块职责总览

`research` 当前只负责“发现候选”，不直接生成正式指标代码，也不直接生成可上线策略。其正式职责链路如下：

```text
CLI / API
  -> MiningRunner
  -> build_data_matrix
  -> feature engineering
  -> analyzers
  -> MiningResult
  -> cross-TF analysis
  -> 分叉

分支 A：FeatureCandidateSpec
  -> IndicatorPromotionDecision
  -> 手工实现正式 indicator
  -> indicators.json 注册
  -> backtest / WF / strategy consumer 验证

分支 B：StrategyCandidateSpec
  -> 手工实现 structured strategy
  -> backtest / execution_feasibility / WF
  -> ValidationDecision
  -> strategy_deployment
  -> demo_validation / active_guarded / active   # ADR-010 后命名
```

各层职责如下：

- `src/research/orchestration/`：只负责编排 research run，不承载统计实现，也不承载晋升决策。
- `src/research/core/`：统一提供配置、DataMatrix、跨 TF 分析、统计工具和正式契约，是两条晋升路径的共享地基。
- `src/research/analyzers/`：统一产出统计证据，只回答“有没有统计关系”，不回答“是否进入 live”。
- `src/research/features/`：把 research feature 收口成 `FeatureCandidateSpec`，并产出 `IndicatorPromotionDecision / FeaturePromotionReport`。
- `src/research/strategies/`：把研究发现收口成 `StrategyCandidateSpec`，供后续 structured strategy 实现和回测验证使用。

边界约束：

- `research` 不直接决定 live enablement。
- `analyzers` 不按“指标版/策略版”复制两套实现。
- `FeatureCandidateSpec` 与 `StrategyCandidateSpec` 都是候选工件，不是运行时策略本体。

### 关键文件职责表

| 文件 | 输入 | 输出 | 职责 | 不负责 |
|------|------|------|------|--------|
| `src/research/orchestration/runner.py` | symbol、timeframe、时间窗、ResearchConfig | `MiningResult` | 编排整次 research run，组织 DataMatrix、feature engineering 和 analyzers | 不做晋升决策，不做策略实现 |
| `src/research/core/config.py` | `config/research.ini` | `ResearchConfig` 及其子配置 | 统一加载 research 配置 | 不承载业务结论 |
| `src/research/core/data_matrix.py` | OHLC、共享指标、regime/soft regime | `DataMatrix` | 构建对齐后的研究矩阵，统一研究输入事实源 | 不做统计分析 |
| `src/research/core/contracts.py` | 各阶段结构化数据 | `MiningResult`、`FeatureCandidateSpec`、`StrategyCandidateSpec` 等契约 | 定义 research 正式工件和结果模型 | 不做计算 |
| `src/research/core/statistics.py` | 数值序列 | 各类统计原语结果 | 提供共享统计工具，避免 analyzer 重复实现 | 不关心策略/指标语义 |
| `src/research/core/overfitting.py` | 序列、fold 配置、显著性参数 | CV / FDR / 显著性辅助结果 | 提供稳健性和过拟合控制工具 | 不直接产出候选工件 |
| `src/research/analyzers/multi_tf_aggregator.py` | 多个 TF 的 `MiningResult` | `CrossTFAnalysis` | 统一判断 `robust / tf_specific / divergent` | 不直接决定策略上线 |
| `src/research/analyzers/predictive_power.py` | `DataMatrix`、配置 | `IndicatorPredictiveResult` 列表 | 评估特征/指标对未来收益的预测力 | 不做候选晋升 |
| `src/research/analyzers/threshold.py` | `DataMatrix`、配置 | `ThresholdSweepResult` 列表 | 扫描阈值，评估 expectancy 和一致性 | 不做候选晋升 |
| `src/research/analyzers/rule_mining.py` | `DataMatrix`、配置 | 规则挖掘结果 | 从条件组合中提取 IF-THEN 规则证据 | 不做策略实现 |
| `src/research/features/hub.py` | `DataMatrix`、Provider 列表 | enriched `DataMatrix` | FeatureHub：聚合 6 个 Provider 输出并注入 DataMatrix | 不注册正式 indicator |
| `src/research/features/{temporal,...}/` | `DataMatrix` | 各维度特征列 | 6 个 Feature Provider：各自负责一个特征维度的计算 | 不访问其他 Provider 输出 |
| `src/research/features/candidates.py` | 多 TF `MiningResult` | `FeatureCandidateDiscoveryResult` / `FeatureCandidateSpec` | 发现 feature candidate，并给出 `IndicatorPromotionDecision` | 不自动写正式 indicator 代码 |
| `src/research/features/promotion.py` | `FeatureCandidateSpec`、下游验证摘要 | `FeaturePromotionReport` | 串联 feature -> indicator -> strategy 的 lineage | 不执行回测 |
| `src/research/strategies/candidates.py` | 多 TF `MiningResult` | `CandidateDiscoveryResult` / `StrategyCandidateSpec` | 发现 strategy candidate，附带 `why / when / where` 骨架与 validation gates | 不直接生成 structured strategy 代码 |
| `src/ops/cli/mining_runner.py` | 命令行参数 | stdout / JSON 工件 | 提供 CLI 入口，组织单 TF 和多 TF research 输出 | 不改变 research 领域语义 |
| `src/api/research_routes/routes.py` | HTTP 请求 | research job / API 响应 | 提供 research API 入口和后台任务提交 | 不实现 research 算法 |
