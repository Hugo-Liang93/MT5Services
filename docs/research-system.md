# Research 信号挖掘系统

> 纯数据驱动的信号发现引擎。职责边界：**发现，不验证**——不涉及现有策略评估。
> 文档类型：研究子系统说明，不描述实时交易服务的启动链路、日志路径或健康探针。
> 如果目标是判断当前服务是否能启动、链路是否贯通，请回到 `docs/architecture.md` 与 `docs/design/full-runtime-dataflow.md`。

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
│  ⑥ MiningResult 输出 + 持久化                                │
└─────────────────────────────────────────────────────────────┘
```

---

## 统计工具层（statistics.py）

所有分析器共享的纯统计原语，集中在 `src/research/statistics.py`，单一职责：可复用的统计计算，不涉及交易逻辑或 DataMatrix 结构。

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

**设计原则**：各分析器不应各自实现 block_shuffle、ACF 等统计工具——统一从 `statistics.py` 导入。

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
Layer 1: 正式指标 (28 个, indicators.json)
  · 注册即持久化（OHLC.indicators JSONB）
  · 实盘/回测/研究全系统共享
  · 趋势/动量/波动/量能/价格行为/复合

Layer 2: 研究特征 (3 个, feature_engineer.py)
  · 仅 DataMatrix 生命周期内存在
  · 依赖运行时状态（regime 序列、soft_regime 概率）
  · momentum_consensus / regime_entropy / bars_in_regime

Layer 3: 分析器输出 → MiningResult → top_findings
```

**判定准则**：
- 实盘策略能用 → Layer 1 指标
- 依赖跨指标组合 + 运行时状态 → Layer 2 研究特征
- 通过挖掘验证有效 → 可晋升为 Layer 1

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
| `src/research/runner.py` | MiningRunner 编排器 |
| `src/research/data_matrix.py` | DataMatrix 构建 |
| `src/research/statistics.py` | 纯统计原语（ACF/block shuffle/效力分析/binomial/Newey-West） |
| `src/research/analyzers/predictive_power.py` | IC + Rolling IC + 排列检验 + BH-FDR + 效力分析 |
| `src/research/analyzers/threshold.py` | 阈值扫描 + expectancy + 排列检验 + 信号去重 |
| `src/research/analyzers/rule_mining.py` | 决策树规则挖掘 + 排列检验 + CV 一致性 + binomial 检验 |
| `src/research/feature_engineer.py` | 研究特征工程框架 |
| `src/research/overfitting.py` | BH-FDR + 排列检验 + CV 工具（expanding/sliding） |
| `src/research/cross_tf_analyzer.py` | 跨 TF 一致性分析 |
| `src/research/config.py` | 配置加载（含 RuleMiningConfig） |
| `src/research/models.py` | 结果数据模型 |
| `src/ops/cli/mining_runner.py` | CLI 工具 |
| `config/research.ini` | 配置文件 |
