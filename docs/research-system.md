# Research 信号挖掘系统

> 纯数据驱动的信号发现引擎。职责边界：**发现，不验证**——不涉及现有策略评估。

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
  · Rolling IC + Information Ratio (IR > 0.5 = 时序稳定)
  · Block Permutation 排列检验 (1000 次)
  · BH-FDR 批量显著性校正

输出示例:
  rsi14.rsi [ranging] IC=+0.30, IR=0.98, perm_p=0.01, 10-bar
  → "RSI 在 ranging 市场对 10 bar 后的收益有稳定预测力"
```

**关键指标**：
- `IC` (Information Coefficient)：指标值与未来收益的秩相关，|IC| > 0.05 有意义
- `IR` (Information Ratio)：IC 的稳定性 = mean(IC) / std(IC)，IR > 0.5 = 稳定
- `perm_p`：排列检验 p-value，< 0.05 排除 data snooping

### 2. Threshold Sweep — "最佳阈值是多少？"

**定量阶段**：知道某指标有预测力后，找到最优的买入/卖出阈值。

```
输入: 单个 indicator.field × horizon × regime
方法:
  · 50 点网格扫描（5th~95th 分位数范围）
  · 每个阈值计算: 命中率 / 平均收益 / Sharpe
  · 信号聚集去重（连续触发只计第一次）
  · 5-fold 时序 CV 一致性检验
  · Block Permutation 排列检验 (200 次)
  · 显著性 = perm_p ≤ 0.05 AND CV ≥ 0.60
  · 测试集外验证

输出示例:
  rsi14.rsi [ranging] BUY <= 35.20  hit=58.3%  CV=78%  perm_p=0.02  SIG
  → "RSI 跌破 35.2 时买入，在 ranging 市场命中率 58.3%，统计显著"
```

**关键指标**：
- `CV`：交叉验证一致性，≥ 0.60 才算稳健，< 0.60 标记 FRAGILE
- `perm_p`：最优阈值的排列检验，排除 50×2=100 次检验的 data snooping
- `SIG`：perm_p ≤ 0.05 AND CV ≥ 0.60 双重门控通过

### 3. Rule Mining — "什么组合最赚？"

**组合阶段**：用决策树发现多条件交互效应。

```
输入: 全部无量纲指标字段（含 8 个新指标 + 3 个研究特征）
方法:
  · DecisionTreeClassifier (max_depth=3, sample_weight=|return|)
  · 仅使用无量纲字段（_DIMENSIONLESS_FIELDS 过滤绝对价格）
  · DFS 遍历叶节点 → 提取盈利路径
  · 每个条件自动映射到 Why/When/Where 角色
  · Train hit_rate ≥ 55% + Test hit_rate ≥ 52% 才保留

输出示例:
  IF adx14.adx > 28.5 AND rsi14.rsi <= 35.2 THEN buy
    Why:   adx14.adx > 28.5    (方向确认)
    When:  rsi14.rsi <= 35.2   (入场时机)
    train=62.9%/n=35  test=57.1%/n=15
  → 直接对应 StructuredStrategyBase 的 _why() + _when()
```

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

## 过拟合防护（6 层）

| 层 | 防护 | 实现 |
|----|------|------|
| L1 | Train/Test 分割 | 70/30 + gap = max(horizons) bars |
| L2 | BH-FDR 多重校正 | 批量校正 FDR ≤ 5%，替代 Bonferroni |
| L3 | 时序交叉验证 | 5-fold 滚动窗口（无未来泄露） |
| L4 | 最小样本量 | n ≥ 30 per bucket |
| L5 | 效应量门槛 | \|IC\| ≥ 0.05 or hit_dev ≥ 3% |
| L6 | Block Permutation | IC: 1000 次 / 阈值: 200 次（保留自相关） |

---

## 多 TF 挖掘准则

**必须多 TF 挖掘**，不得在单 TF 上得出结论：

```bash
python tools/mining_runner.py --tf M15,M30,H1 --compare
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

[threshold_sweep]
sweep_points = 50
per_regime = true
n_permutations = 200

[predictive_power]
rolling_ic_enabled = true
rolling_ic_window = 60
permutation_test_enabled = true
n_permutations = 1000

[feature_engineering]
enabled = true
```

---

## 关键文件

| 文件 | 用途 |
|------|------|
| `src/research/runner.py` | MiningRunner 编排器 |
| `src/research/data_matrix.py` | DataMatrix 构建 |
| `src/research/analyzers/predictive_power.py` | IC + Rolling IC + 排列检验 + BH-FDR |
| `src/research/analyzers/threshold.py` | 阈值扫描 + 排列检验 + 信号去重 |
| `src/research/analyzers/rule_mining.py` | 决策树规则挖掘 → Why/When/Where |
| `src/research/feature_engineer.py` | 研究特征工程框架 |
| `src/research/overfitting.py` | BH-FDR + 排列检验 + CV 工具 |
| `src/research/cross_tf_analyzer.py` | 跨 TF 一致性分析 |
| `src/research/config.py` | 配置加载 |
| `src/research/models.py` | 结果数据模型 |
| `tools/mining_runner.py` | CLI 工具 |
| `config/research.ini` | 配置文件 |
