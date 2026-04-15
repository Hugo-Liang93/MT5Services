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
  * 见 `src/research/core/barrier.py` + `tests/research/test_barrier.py`
- **F-12b** ✅ **已闭环**（commit `c0c0387`）：策略可声明 `ExitSpec(mode=BARRIER, ...)` 与挖掘 exit 语义对齐
  * 见 `src/trading/positions/exit_rules.py` `evaluate_barrier_exit()`
- **F-12c** ⏸️ **推迟至 P3**：Plan B 完成后诊断价值被实质覆盖

### 下次挖掘推荐流程

1. `MiningRunner.run()` 现在会自动填充 `DataMatrix.barrier_returns_*`
2. 产出 Top Rules 时**同时报告**朴素 forward_return 胜率 + barrier_returns 胜率（选 top-3 RR 组合）
3. Top candidate 选择时挑"两者都高"的规则 → 天然避开本次 C-1/I-1 这类 exit 不兼容陷阱
4. 候选编码策略时在 `_exit_spec()` 声明挖掘产出的最优 barrier 参数，回测自动走 barrier 路径

---

## 系统定位

```
Research (发现假设)
    ↓ 人工编码为策略
Backtest (历史验证)
    ↓ WF + Recommendation
Paper Trading (实时影子验证)
    ↓ 人工确认
Live Trading
```

Research 是整条链路的源头——从历史数据中挖掘"指标值 → 未来收益"的统计关系，输出可操作的发现供策略开发使用。

当前 research/mining 默认只产出 `FeatureCandidateSpec` 与 `StrategyCandidateSpec` 这类单策略候选工件，不生成 voting group / ensemble / `strategy="consensus"` 运行时合同。若未来需要组合策略，必须单独定义新工件类型，而不是复用当前单策略主线。

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
  -> paper_only / active_guarded / active
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
- `paper_only`：只允许 paper/shadow。
- `active_guarded`：允许 live，但必须保持永久护栏。
- `active`：仅 `robust` 策略允许进入。

`active_guarded` 的硬约束如下：

- 必须声明 `locked_timeframes`
- 必须声明 `locked_sessions`
- `min_final_confidence` 至少高于同 TF 基线 `+0.05`，且不低于 `0.50`
- `max_live_positions = 1`
- `require_pending_entry = true`
- `paper_shadow_required = true`

运行时不再允许用 “`regime_affinity.* = 0`” 这种隐式冻结方式代替部署状态；冻结、候选、paper-only、guarded 都必须通过 `strategy_deployment.<strategy>` 正式声明。

### 首个真实 indicator promotion 结果

本轮已经交付第一条真实的 `feature -> indicator -> strategy consumer -> validation` 证据链：

- research feature：`derived.momentum_consensus`
- promoted shared indicator：`momentum_consensus14`
- downstream strategy consumer：`structured_trend_h4_momentum`
- deployment contract：`paper_only + tf_specific + locked_timeframes=H1 + locked_sessions=london,new_york`

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

因此，indicator promotion 已成立，但 downstream strategy 仍只处于 `paper_only / refit` 阶段，不能把 promoted indicator 的存在误解成“策略已经可上线”。

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

Layer 2: 研究特征 (3 个, features/engineer.py)
  · 仅 DataMatrix 生命周期内存在
  · 可以依赖 Layer 1 指标，也可以依赖 regime / soft_regime 等研究态上下文
  · momentum_consensus / regime_entropy / bars_in_regime

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
| `src/research/features/engineer.py` | 研究特征工程框架 |
| `src/research/core/overfitting.py` | BH-FDR + 排列检验 + CV 工具（expanding/sliding） |
| `src/research/core/cross_tf.py` | 跨 TF 一致性分析 |
| `src/research/core/config.py` | 配置加载（含 RuleMiningConfig） |
| `src/research/core/contracts.py` | 结果数据模型与候选工件契约 |
| `src/ops/cli/mining_runner.py` | CLI 工具 |
| `config/research.ini` | 配置文件 |

### 当前目录边界

- `src/research/core/`：公共基础层，承载配置、DataMatrix、统计工具、跨 TF 分析与研究契约。
- `src/research/analyzers/`：共享证据引擎，只负责统计分析，不区分指标路径或策略路径。
- `src/research/features/`：research feature、feature candidate、indicator promotion 路径。
- `src/research/strategies/`：strategy candidate 发现路径。
- `src/research/orchestration/`：MiningRunner 编排入口。

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
  -> paper_only / active_guarded / active
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
| `src/research/core/cross_tf.py` | 多个 TF 的 `MiningResult` | `CrossTFAnalysis` | 统一判断 `robust / tf_specific / divergent` | 不直接决定策略上线 |
| `src/research/analyzers/predictive_power.py` | `DataMatrix`、配置 | `IndicatorPredictiveResult` 列表 | 评估特征/指标对未来收益的预测力 | 不做候选晋升 |
| `src/research/analyzers/threshold.py` | `DataMatrix`、配置 | `ThresholdSweepResult` 列表 | 扫描阈值，评估 expectancy 和一致性 | 不做候选晋升 |
| `src/research/analyzers/rule_mining.py` | `DataMatrix`、配置 | 规则挖掘结果 | 从条件组合中提取 IF-THEN 规则证据 | 不做策略实现 |
| `src/research/features/engineer.py` | `DataMatrix`、feature definitions | enriched `DataMatrix`、feature inventory | 管理 research feature 定义和批量注入 | 不注册正式 indicator |
| `src/research/features/candidates.py` | 多 TF `MiningResult` | `FeatureCandidateDiscoveryResult` / `FeatureCandidateSpec` | 发现 feature candidate，并给出 `IndicatorPromotionDecision` | 不自动写正式 indicator 代码 |
| `src/research/features/promotion.py` | `FeatureCandidateSpec`、下游验证摘要 | `FeaturePromotionReport` | 串联 feature -> indicator -> strategy 的 lineage | 不执行回测 |
| `src/research/strategies/candidates.py` | 多 TF `MiningResult` | `CandidateDiscoveryResult` / `StrategyCandidateSpec` | 发现 strategy candidate，附带 `why / when / where` 骨架与 validation gates | 不直接生成 structured strategy 代码 |
| `src/ops/cli/mining_runner.py` | 命令行参数 | stdout / JSON 工件 | 提供 CLI 入口，组织单 TF 和多 TF research 输出 | 不改变 research 领域语义 |
| `src/api/research_routes/routes.py` | HTTP 请求 | research job / API 响应 | 提供 research API 入口和后台任务提交 | 不实现 research 算法 |
