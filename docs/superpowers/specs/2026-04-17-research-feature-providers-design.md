# Research Feature Providers 设计方案

> 增强挖掘模块的特征感知能力：时序模式、市场微结构、跨 TF 上下文、Regime 转换。
> 模块化 Provider 架构，配置化启用，分组过拟合防护。

**日期：** 2026-04-17  
**状态：** Approved

---

## 1. 背景与动机

当前挖掘模块（Research）的特征空间仅有 28 个原始指标 + 3 个派生特征。分析器对每根 bar 做独立快照评估，无法感知：

- 指标的变化方向与节奏（时序）
- bar 之间的量价关系与持续性（微结构）
- 不同时间框架之间的约束关系（跨 TF）
- 行情状态切换的时机与路径（Regime 转换）

这导致挖掘只能发现"指标在某阈值时有预测力"这类静态规则，无法发现动态的、有上下文的交易机会。

**目标：** 将特征空间从 ~31 扩展到 ~95，覆盖 4 个认知维度，让挖掘模块能发现更丰富的市场行为模式。

---

## 2. 架构设计

### 2.1 整体结构

```
src/research/features/
├── hub.py                    # FeatureHub — 唯一入口
├── protocol.py               # FeatureProvider 协议 + ProviderDataRequirement + FeatureRole
├── temporal/
│   ├── __init__.py
│   └── provider.py           # TemporalFeatureProvider
├── microstructure/
│   ├── __init__.py
│   └── provider.py           # MicrostructureFeatureProvider
├── cross_tf/
│   ├── __init__.py
│   └── provider.py           # CrossTFFeatureProvider
├── regime_transition/
│   ├── __init__.py
│   └── provider.py           # RegimeTransitionFeatureProvider
├── candidates.py             # 保留（特征候选发现）
└── promotion.py              # 保留（特征晋升流程）
```

**删除：** `engineer.py` — 职责被 FeatureHub + Providers 完整替代。现有 3 个派生特征归属：
- `momentum_consensus` → 已晋升为正式指标（`indicators.json`），不再是研究特征
- `regime_entropy` → 迁入 `RegimeTransitionProvider`
- `bars_in_regime` → 迁入 `RegimeTransitionProvider`

### 2.2 核心协议

```python
class FeatureRole(Enum):
    WHY = "why"        # 方向确认
    WHEN = "when"      # 入场时机
    WHERE = "where"    # 结构位
    VOLUME = "volume"  # 量能

@dataclass
class ProviderDataRequirement:
    """Provider 声明的额外数据需求"""
    parent_tf_mapping: dict[str, str]  # {挖掘TF: 父TF}，如 {"M30": "H4"}
    parent_indicators: list[str]        # 需要父 TF 的哪些指标

class FeatureProvider(Protocol):
    @property
    def name(self) -> str:
        """模块名，如 'temporal'"""
        ...

    @property
    def feature_count(self) -> int:
        """本模块提供的特征数量"""
        ...

    def required_columns(self) -> list[str]:
        """声明需要 DataMatrix 中的哪些列"""
        ...

    def required_extra_data(self) -> ProviderDataRequirement | None:
        """声明额外数据需求。None 表示只需标准 DataMatrix"""
        ...

    def role_mapping(self) -> dict[str, FeatureRole]:
        """声明每个特征的策略角色映射，供候选生成使用"""
        ...

    def compute(
        self,
        matrix: DataMatrix,
        extra_data: dict[str, Any] | None = None,
    ) -> dict[str, np.ndarray]:
        """批量计算全部特征。数组长度 == matrix.n_bars"""
        ...
```

### 2.3 FeatureHub

```python
class FeatureHub:
    """研究特征计算的唯一入口。"""

    def __init__(self, config: ResearchConfig):
        self._providers: dict[str, FeatureProvider] = {}
        self._register_enabled_providers(config)

    def _register_enabled_providers(self, config: ResearchConfig) -> None:
        provider_classes = {
            "temporal": TemporalFeatureProvider,
            "microstructure": MicrostructureFeatureProvider,
            "cross_tf": CrossTFFeatureProvider,
            "regime_transition": RegimeTransitionFeatureProvider,
        }
        for name, cls in provider_classes.items():
            if config.is_provider_enabled(name):
                self._providers[name] = cls(config)

    def required_extra_data(self) -> list[ProviderDataRequirement]:
        """聚合所有 Provider 的额外数据需求，供 Runner 准备"""
        reqs = []
        for provider in self._providers.values():
            req = provider.required_extra_data()
            if req is not None:
                reqs.append(req)
        return reqs

    def compute_all(
        self,
        matrix: DataMatrix,
        extra_data: dict[str, Any] | None = None,
    ) -> FeatureComputeResult:
        """批量计算全部启用模块的特征，注入 matrix.indicator_series"""
        result = FeatureComputeResult()
        for name, provider in self._providers.items():
            t0 = time.perf_counter()
            features = provider.compute(matrix, extra_data)
            elapsed = time.perf_counter() - t0
            for feat_name, values in features.items():
                matrix.indicator_series[feat_name] = values
            result.add(name, len(features), elapsed)
        return result

    def feature_names_by_provider(self) -> dict[str, list[str]]:
        """返回 {provider_name: [feature_names]}，供分组 FDR"""
        ...

    def role_mapping_all(self) -> dict[str, FeatureRole]:
        """聚合全部 Provider 的角色映射，供候选生成"""
        ...

    def describe(self) -> dict[str, Any]:
        """各模块状态摘要"""
        ...
```

### 2.4 数据流

```
MiningRunner
  ├── 构建 DataMatrix（不变）
  ├── extra_reqs = FeatureHub.required_extra_data()
  ├── 如有需求 → _prepare_extra_data(symbol, tf, extra_reqs)
  │     └── 加载父 TF 历史 bars + 计算父 TF 指标
  ├── FeatureHub.compute_all(matrix, extra_data)
  │     ├── TemporalProvider.compute(matrix)
  │     ├── MicrostructureProvider.compute(matrix)
  │     ├── CrossTFProvider.compute(matrix, extra_data)
  │     └── RegimeTransitionProvider.compute(matrix)
  │     → 全部注入 matrix.indicator_series
  ├── provider_groups = FeatureHub.feature_names_by_provider()
  ├── 分析器循环：analyzer.analyze(matrix, provider_groups=provider_groups)
  └── 输出 MiningResult
```

---

## 3. 各 Provider 特征清单

### 3.1 TemporalFeatureProvider（时序特征）

对可配置的目标指标 × 可配置的窗口生成特征。

**默认配置：**
- 核心指标（全量时序）：`rsi14, adx14`
- 辅助指标（仅 delta_5）：`macd_histogram, cci20, roc10, stoch_k`
- 默认窗口：`3, 5, 10`

| 特征名模式 | 公式 | 角色 |
|-----------|------|------|
| `temporal_{ind}_delta_{w}` | `ind[i] - ind[i-w]` | WHY |
| `temporal_{ind}_accel_{w}` | `delta[i] - delta[i-w]` | WHY |
| `temporal_{ind}_slope_{w}` | 过去 w bar 线性回归斜率 | WHY |
| `temporal_{ind}_zscore_{w}` | `(ind[i] - mean(w)) / std(w)` | WHEN |
| `temporal_{ind}_bars_since_{level}` | 距上次穿越关键水平的 bar 数 | WHEN |

**关键水平定义：**
- RSI: 30, 50, 70
- ADX: 20, 25
- StochK: 20, 80
- MACD histogram: 0
- CCI: -100, 0, 100

**默认特征数：~34**
- 2 核心 × 3 窗口 × 4 类型 = 24
- 4 辅助 × 1 窗口 × 1 类型 = 4
- bars_since_cross: 2 核心 × 3 水平 = 6

### 3.2 MicrostructureFeatureProvider（微结构特征）

基于 OHLCV 原始数据，默认 lookback = 5。

| 特征名 | 公式 | 角色 |
|--------|------|------|
| `micro_consecutive_up` | 连续 close > close[-1] 计数 | WHY |
| `micro_consecutive_down` | 连续 close < close[-1] 计数 | WHY |
| `micro_vol_price_accord_{w}` | Σ sign(close_delta) × sign(vol_delta) / w | VOLUME |
| `micro_volatility_ratio_{w}` | ATR[0] / mean(ATR[i-w:i]) | WHEN |
| `micro_bb_width_change_{w}` | bb_width[0] / bb_width[-w] | WHEN |
| `micro_avg_body_ratio_{w}` | 过去 w bar body_ratio 均值 | WHEN |
| `micro_upper_shadow_ratio_{w}` | 过去 w bar 上影线占比均值 | WHERE |
| `micro_lower_shadow_ratio_{w}` | 过去 w bar 下影线占比均值 | WHERE |
| `micro_hh_count_{w}` | 过去 w bar 中 HH 次数 | WHY |
| `micro_ll_count_{w}` | 过去 w bar 中 LL 次数 | WHY |
| `micro_gap_ratio` | (open[0] - close[-1]) / ATR | WHEN |
| `micro_range_position_{w}` | (close - min_low) / (max_high - min_low) | WHERE |
| `micro_volume_surge` | volume[0] / mean(volume[-w:]) | VOLUME |

**默认特征数：~13**

### 3.3 CrossTFFeatureProvider（跨 TF 上下文）

需额外输入父 TF 指标序列。默认关闭。

**父 TF 默认映射：** M15→H1, M30→H4, H1→H4, H4→D1

**数据对齐方式：** 父 TF 值 forward-fill 到子 TF bar（标准多 TF 对齐，无未来信息泄露）。

| 特征名 | 公式 | 角色 |
|--------|------|------|
| `ctf_parent_trend_dir` | 父 TF supertrend_direction (+1/-1) | WHY |
| `ctf_parent_rsi` | 父 TF RSI14 值 | WHY |
| `ctf_parent_adx` | 父 TF ADX14 值 | WHY |
| `ctf_tf_trend_align` | sign(子TF趋势) × sign(父TF趋势) | WHY |
| `ctf_dist_to_parent_ema` | (close - parent_EMA50) / ATR | WHERE |
| `ctf_parent_rsi_delta_5` | 父 TF RSI 的 5-bar delta | WHY |
| `ctf_parent_adx_delta_5` | 父 TF ADX 的 5-bar delta | WHY |
| `ctf_parent_bb_pos` | 父 TF BB position | WHERE |

**默认特征数：~8**

### 3.4 RegimeTransitionFeatureProvider（Regime 转换特征）

基于 DataMatrix 中已有的 hard_regimes 和 soft_regimes。默认 history_window = 50。

| 特征名 | 公式 | 角色 |
|--------|------|------|
| `regime_bars_since_change` | 距上次 hard_regime 变化 bar 数 | WHEN |
| `regime_prev_regime` | 上一段 regime 编码 (0-3) | WHY |
| `regime_transitions_{w}` | 过去 w bar 内切换次数 | WHEN |
| `regime_trending_prob_delta_{w}` | soft trending 概率 w-bar delta | WHY |
| `regime_ranging_prob_delta_{w}` | soft ranging 概率 w-bar delta | WHY |
| `regime_breakout_prob_delta_{w}` | soft breakout 概率 w-bar delta | WHY |
| `regime_duration_vs_avg` | 当前持续 / 历史平均持续 | WHEN |
| `regime_entropy_delta_{w}` | entropy w-bar delta | WHEN |
| `regime_dominant_strength` | max(soft_probs) | WHY |

**默认特征数：~9**（含原 regime_entropy 和 bars_in_regime 的职责）

### 3.5 汇总

| Provider | 默认特征数 | 计算复杂度 | 额外数据 | 默认启用 |
|----------|-----------|-----------|---------|---------|
| temporal | ~34 | 中 | 无 | 是 |
| microstructure | ~13 | 低 | 无 | 是 |
| cross_tf | ~8 | 低 | 父 TF bars | 否 |
| regime_transition | ~9 | 低 | 无 | 是 |
| **合计** | **~64** | — | — | — |

加 28 原始指标 = **默认 ~84 特征，全量 ~92 特征**。

---

## 4. 配置

### 4.1 research.ini 新增段

```ini
[feature_providers]
temporal = true
microstructure = true
cross_tf = false
regime_transition = true

[feature_providers.temporal]
core_indicators = rsi14,adx14
aux_indicators = macd_histogram,cci20,roc10,stoch_k
windows = 3,5,10
cross_levels_rsi = 30,50,70
cross_levels_adx = 20,25

[feature_providers.microstructure]
lookback = 5

[feature_providers.cross_tf]
parent_tf_map = M15:H1,M30:H4,H1:H4,H4:D1
parent_indicators = supertrend_direction,rsi14,adx14,ema50,bb_position

[feature_providers.regime_transition]
history_window = 50
prob_delta_window = 5
```

### 4.2 CLI 参数扩展

```bash
# 覆盖 ini 启用配置
python -m src.ops.cli.mining_runner --tf M15,M30,H1 --providers temporal,microstructure --compare

# 全量
python -m src.ops.cli.mining_runner --tf M30,H1 --providers all --compare

# 默认（按 ini 配置）
python -m src.ops.cli.mining_runner --tf M30,H1 --compare
```

---

## 5. 过拟合防护适配

### 5.1 分组 FDR

特征数扩大 3 倍，全局 BH-FDR 会过度压低 alpha。改为按 Provider 分组：

```python
# 按 provider 分组独立做 FDR
for provider_name, feature_names in provider_groups.items():
    pvalues = [r.perm_pvalue for r in results if r.feature in feature_names]
    adjusted = bh_fdr(pvalues)
```

原始 28 个指标归为 `"base"` 组。

**配置项：**
```ini
[overfitting]
fdr_grouping = by_provider   # by_provider / global / none
```

### 5.2 其他防护层

排列检验、CV 一致性、最小样本、效应量门槛均不变（每个特征独立检验，与特征总数无关）。

---

## 6. 分析器扩展点

### 6.1 MiningAnalyzer 协议

```python
class MiningAnalyzer(Protocol):
    @property
    def name(self) -> str: ...

    def analyze(
        self,
        matrix: DataMatrix,
        provider_groups: dict[str, list[str]] | None = None,
    ) -> AnalyzerResult: ...

class AnalyzerResult(Protocol):
    def top_findings(self, n: int) -> list[Finding]: ...
    def summary(self) -> str: ...
```

### 6.2 MiningRunner 改为持有分析器列表

```python
class MiningRunner:
    def __init__(self, config):
        self._feature_hub = FeatureHub(config)
        self._analyzers: list[MiningAnalyzer] = [
            PredictivePowerAnalyzer(config),
            ThresholdSweepAnalyzer(config),
            RuleMiningAnalyzer(config),
        ]
```

现有三个分析器适配 `MiningAnalyzer` 协议（增加 `provider_groups` 参数）。未来新增分析器只需实现协议 + 注册到列表。

---

## 7. MiningResult 输出增强

### 7.1 新增字段

```python
@dataclass
class MiningResult:
    # 现有
    data_summary: DataSummary
    predictive_power: list[IndicatorPredictiveResult]
    threshold_sweeps: list[ThresholdSweepResult]
    mined_rules: list[MinedRule]
    top_findings: list[Finding]

    # 新增
    feature_compute_summary: FeatureComputeResult
    findings_by_provider: dict[str, list[Finding]]
    cross_provider_rules: list[MinedRule]
```

### 7.2 跨模块规则提取

规则条件涉及 2+ 个 Provider 的特征时，标记为 `cross_provider_rules`，单独展示。这类规则价值最高（跨维度组合信号）。

### 7.3 特征 → 策略角色映射

每个 Provider 通过 `role_mapping()` 声明自己特征的角色（WHY/WHEN/WHERE/VOLUME），供 `StrategyCandidateSpec` 生成器自动填充 Why/When/Where 骨架。

---

## 8. 性能预估

| 环节 | 耗时（H1, 4700 bar） | 说明 |
|------|----------------------|------|
| Temporal 计算 | ~200ms | numpy 向量化，含线性回归 |
| Microstructure 计算 | ~100ms | 纯 OHLCV 滚动 |
| CrossTF 数据加载 | ~500ms | DB 查询 + 指标计算（仅启用时） |
| CrossTF 对齐 | ~50ms | pandas merge_asof |
| RegimeTransition 计算 | ~50ms | regime 序列统计 |
| 新增总耗时 | ~400ms（无 CrossTF）/ ~900ms（含） | — |
| 分析器排列检验 | ×2.8 线性增长 | 特征数增加，各特征独立可并行 |

---

## 9. 变更清单

| 文件 | 操作 |
|------|------|
| `src/research/features/engineer.py` | **删除** |
| `src/research/features/hub.py` | **新建** |
| `src/research/features/protocol.py` | **新建** |
| `src/research/features/temporal/__init__.py` | **新建** |
| `src/research/features/temporal/provider.py` | **新建** |
| `src/research/features/microstructure/__init__.py` | **新建** |
| `src/research/features/microstructure/provider.py` | **新建** |
| `src/research/features/cross_tf/__init__.py` | **新建** |
| `src/research/features/cross_tf/provider.py` | **新建** |
| `src/research/features/regime_transition/__init__.py` | **新建** |
| `src/research/features/regime_transition/provider.py` | **新建** |
| `src/research/orchestration/runner.py` | **改造** — FeatureEngineer → FeatureHub |
| `src/research/core/config.py` | **扩展** — Provider 配置读取 |
| `src/research/analyzers/predictive_power.py` | **扩展** — 接受 provider_groups，分组 FDR |
| `src/research/analyzers/threshold.py` | **扩展** — 同上 |
| `src/research/analyzers/rule_mining.py` | **扩展** — 同上 |
| `src/research/strategies/candidates.py` | **扩展** — 新特征角色映射 |
| `config/research.ini` | **扩展** — `[feature_providers]` 段 |
| `src/ops/cli/mining_runner.py` | **扩展** — `--providers` 参数 |
| 测试文件 | **新建** — 每个 Provider 独立测试 + Hub 集成测试 |
