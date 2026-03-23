# MT5Services — 信号系统设计文档

> 面向策略开发者的深度参考。涵盖信号系统的设计原理、完整流程、扩展规范。

---

## 目录

1. [设计目标](#1-设计目标)
2. [模块地图](#2-模块地图)
3. [信号评估流程](#3-信号评估流程)
4. [策略开发规范](#4-策略开发规范)
5. [Regime 系统](#5-regime-系统)
6. [置信度修正流水线](#6-置信度修正流水线)
7. [状态机详解](#7-状态机详解)
8. [Voting Engine](#8-voting-engine)
9. [CompositeSignalStrategy](#9-compositesignalstrategy)
10. [诊断与质量报告](#10-诊断与质量报告)
11. [常见问题](#11-常见问题)

---

## 1. 设计目标

信号系统的设计遵循以下核心原则：

| 原则 | 实现方式 |
|------|---------|
| 策略自描述 | 每个策略类声明 `name / required_indicators / preferred_scopes / regime_affinity` |
| 市场状态感知 | Regime 分类 × affinity 乘数，不同市场状态下自动调节策略权重 |
| 高频事件不饿死确认事件 | 双队列架构：confirmed（K线收盘）优先于 intrabar |
| 多策略共识 | VotingEngine 对同一快照的所有策略决策加权投票 |
| 可观测 | 每个信号携带完整溯源链：`raw_confidence → affinity → calibrated` |

---

## 2. 模块地图

```
src/signals/
│
├── service.py                 # SignalModule（策略注册中心 + evaluate() 入口）
│
├── orchestration/             # 运行时编排
│   ├── runtime.py             # SignalRuntime（事件主循环，双队列）
│   ├── policy.py              # SignalPolicy（运行时参数）+ VotingGroupConfig
│   ├── voting.py              # StrategyVotingEngine（加权投票）
│   ├── htf_resolver.py        # HTF 配置解析、指标查询、对齐乘数计算
│   └── state_machine.py       # 状态机转换（preview/armed/confirmed 纯逻辑）
│
├── strategies/                # 策略实现
│   ├── base.py                # SignalStrategy Protocol + 辅助函数
│   ├── trend.py               # SmaTrend, MacdMomentum, Supertrend, EmaRibbon, HmaCross, RocMomentum
│   ├── mean_reversion.py      # RsiReversion, StochRsi, WilliamsR, CciReversion
│   ├── breakout.py            # BollingerBreakout, KeltnerBollingerSqueeze, DonchianBreakout, MultiTimeframeConfirm
│   ├── composite.py           # CompositeSignalStrategy + build_*() 工厂函数
│   ├── adapters.py            # IndicatorSource（适配 UnifiedIndicatorManager）
│   ├── htf_cache.py           # HTFStateCache（高时间框架状态缓存）
│   └── registry.py            # StrategyRegistry（按名称查找策略实例）
│
├── evaluation/
│   ├── regime.py              # MarketRegimeDetector + RegimeType + RegimeTracker
│   ├── calibrator.py          # ConfidenceCalibrator（历史胜率混合校准）
│   ├── performance.py         # StrategyPerformanceTracker（日内策略绩效追踪）
│   └── indicators_helpers.py  # 指标提取工具函数
│
├── execution/
│   └── filters.py             # SignalFilterChain（时段/点差/经济事件过滤）
│
├── tracking/
│   └── repository.py          # SignalRepository（持久化 + 查询）
│
├── analytics/
│   ├── diagnostics.py         # DiagnosticsEngine（冲突检测、质量报告）
│   └── plugins.py             # 分析插件
│
└── contracts/
    └── sessions.py            # 交易时段常量（SESSION_LONDON 等）
```

---

## 3. 信号评估流程

### 3.1 触发路径

```
IndicatorManager 发布快照
    │
    ▼
SignalRuntime._on_snapshot(symbol, timeframe, bar_time, indicators, scope)
    │
    ├─ scope="confirmed" → _confirmed_events.put()   # bar-close，不可丢弃
    └─ scope="intrabar"  → _intrabar_events.put()    # 盘中实时，可丢弃
```

### 3.2 主循环（`process_next_event`）

```
1. 优先取 confirmed 队列（get_nowait），再等 intrabar（blocking）

2. 全局过滤（SignalFilterChain）
   → 时段不符 / 点差过大 → 跳过（计数但不评估）

3. Regime 检测（每次快照仅调用一次）
   → regime + stability_multiplier → 写入 regime_metadata["_regime"]

4. _evaluate_strategies()（见 3.3）

5. _process_voting()（见 8.2）
```

### 3.3 策略评估（`_evaluate_strategies`）

对该 `(symbol, timeframe)` 下的所有注册策略依次执行：

```
for strategy in strategies[(symbol, timeframe)]:

    # ① session 白名单检查
    if strategy 不在 allowed_sessions → skip

    # ② timeframe 白名单检查
    if strategy 不在 allowed_timeframes → skip

    # ③ scope 匹配检查
    if scope 不在 strategy.preferred_scopes → skip

    # ④ 指标完整性检查
    if any required_indicator not in indicators → skip

    # ⑤ Pre-flight affinity gate（0.15 默认）
    if regime_affinity[regime] < min_affinity_skip → skip（不调用 evaluate()）

    # ⑥ Snapshot 去重（相同 bar_time + signature 不重复评估）
    if _should_evaluate_snapshot() == False → skip

    # ⑦ 策略评估
    decision = service.evaluate(strategy, scoped_indicators, regime_metadata)
    # 内部：raw_conf × affinity → ConfidenceCalibrator → final_conf

    # ⑧ 状态机转换
    if scope == "confirmed":
        transition_metadata = _transition_confirmed(...)
    else:
        transition_metadata = _transition_preview(...)

    # ⑨ 持久化 + 事件发布（仅在状态发生转换时）
    if transition_metadata:
        service.persist_decision(...)
        _publish_signal_event(...)
```

---

## 4. 策略开发规范

### 4.1 必填四属性

注册时 `SignalModule._validate_strategy_attrs()` 自动校验：

```python
class MyStrategy:
    # ① 全局唯一名称（用于日志、API、信号记录）
    name: str = "my_strategy"

    # ② 依赖指标（必须在 config/indicators.json 中存在）
    required_indicators: tuple[str, ...] = ("adx14", "rsi14")

    # ③ 接收快照的 scope
    #    "confirmed" = bar 收盘（趋势/突破类）
    #    "intrabar"  = 盘中实时（均值回归类）
    preferred_scopes: tuple[str, ...] = ("confirmed",)

    # ④ Regime 亲和度（四个 RegimeType 均须覆盖）
    regime_affinity: dict[RegimeType, float] = {
        RegimeType.TRENDING:  1.00,
        RegimeType.RANGING:   0.20,
        RegimeType.BREAKOUT:  0.50,
        RegimeType.UNCERTAIN: 0.50,
    }
```

### 4.2 evaluate() 接口

```python
def evaluate(self, context: SignalContext) -> SignalDecision:
    """
    context.symbol      - 品种（如 "XAUUSD"）
    context.timeframe   - 时间框架（如 "H1"）
    context.indicators  - 已经过 required_indicators 收窄的指标快照
    context.metadata    - 包含 _regime, bar_time, scope 等运行时信息

    返回 SignalDecision：
        action: "buy" | "sell" | "hold"
        confidence: float (0.0–1.0)
        reason: str
        metadata: dict  # 策略内部调试信息
    """
```

**注意**：
- `confidence` 应反映策略规则的"信号强度"，不需要考虑 Regime（由外层乘数处理）
- `action="hold"` 表示无信号，`confidence` 无意义（不被使用）
- 不要在策略内部访问 `context.metadata["_regime"]` 做硬截断——这由 affinity 机制自然处理

### 4.3 Regime 亲和度设计指南

| 策略类型 | TRENDING | RANGING | BREAKOUT | UNCERTAIN |
|---------|---------|---------|---------|---------|
| 趋势跟踪（MA Cross、Supertrend、EMA Ribbon）| 1.00 | 0.10–0.30 | 0.40–0.60 | 0.50 |
| 均值回归（RSI、StochRSI、Williams R）| 0.20–0.30 | 1.00 | 0.30–0.40 | 0.60 |
| 突破/波动率（Bollinger、Donchian、Keltner）| 0.30–0.90 | 0.15–0.55 | 1.00 | 0.45–0.65 |

**核心语义**：
- `1.00` = 该行情下完全可信，置信度不衰减
- `0.50` = 置信度减半（原 0.70 → 0.35，低于默认阈值 0.55，信号被静默）
- `0.10` = 几乎完全压制

### 4.4 新增策略完整步骤

```
1. 实现策略类（含四属性 + evaluate()）
   ├─ 趋势跟踪  → src/signals/strategies/trend.py
   ├─ 均值回归  → src/signals/strategies/mean_reversion.py
   ├─ 突破      → src/signals/strategies/breakout.py
   └─ 复合      → src/signals/strategies/composite.py

2. 在 src/signals/strategies/__init__.py 中导出

3. 在 src/signals/service.py 默认策略列表中追加实例

4. 确认 config/indicators.json 包含 required_indicators 中的指标
   （如果指标不存在，需同步在 src/indicators/core/ 中实现并添加 JSON 条目）

5. 在 tests/signals/ 中编写单元测试：
   ├─ 四种 Regime 下的输出验证
   ├─ hold 路径验证
   └─ 边界条件验证

6. （可选）在 config/signal.ini 的 [strategy_timeframes] / [strategy_sessions] 中
   配置时间框架/时段白名单
```

---

## 5. Regime 系统

### 5.1 分类逻辑

`MarketRegimeDetector.detect(indicators)` 优先级从高到低：

```python
# 1. Keltner-Bollinger Squeeze（BB完全在KC内）
if bb_upper <= kc_upper and bb_lower >= kc_lower:
    return BREAKOUT

# 2. ADX 趋势强度
if adx >= 25:
    return TRENDING

# 3. 低 ADX + 极窄 BB（蓄力盘整）
if adx < 20 and bb_width < 0.005:
    return BREAKOUT

# 4. 震荡
if adx < 20:
    return RANGING

# 5. 过渡区
if 20 <= adx < 25:
    return UNCERTAIN

# 6. 兜底
return UNCERTAIN
```

### 5.2 RegimeTracker（稳定性跟踪）

每个 `(symbol, timeframe)` 有独立的 `RegimeTracker`：

- 跟踪最近 N 个 bar 的 Regime 序列
- `stability_multiplier()` 返回 0.0–1.0，表示当前 Regime 的稳定程度
- 在 `_emit_vote_signal()` 中，共识信号置信度 × `regime_stability`
- 目的：刚发生 Regime 切换时，共识信号置信度降低，避免虚假突破

---

## 6. 置信度修正流水线

```
strategy.evaluate() → raw_confidence（规则层输出，0.0–1.0）
         │
         ▼
× regime_affinity[detected_regime]
         │
         ▼
  post_affinity_confidence
         │
         ▼ （如果 ConfidenceCalibrator 已启用）
calibrator.calibrate(strategy, action, raw_confidence=post_affinity, regime=regime)
    ├─ 查询该 (strategy, action, regime) 的历史胜率
    ├─ 混合校准：calibrated = α × win_rate + (1-α) × post_affinity
    └─ 返回 calibrated_confidence
         │
         ▼
  final_confidence（写入 SignalDecision.confidence）
```

信号 metadata 中携带完整溯源：

```json
{
    "raw_confidence": 0.72,
    "post_affinity_confidence": 0.36,
    "regime": "ranging",
    "regime_affinity": 0.50,
    "calibrated": false
}
```

启用 soft regime 后，metadata 还会附带：

```json
{
    "regime_source": "soft",
    "regime_probabilities": {
        "trending": 0.40,
        "ranging": 0.10,
        "breakout": 0.20,
        "uncertain": 0.30
    },
    "dominant_regime_probability": 0.30
}
```

此时有效 affinity 为：

```python
effective_affinity = sum(
    regime_probabilities[r] * strategy.regime_affinity[r]
    for r in RegimeType
)
```

---

## 7. 状态机详解

### 7.1 Intrabar 路径

每个 `(symbol, timeframe, strategy)` 的 preview 状态由 `_transition_preview()` 管理：

```
状态变量：
  preview_state   = "idle" | "preview_buy" | "preview_sell" | "armed_buy" | "armed_sell"
  preview_action  = None | "buy" | "sell"
  preview_since   = datetime (方向稳定开始时间)
  preview_bar_time= datetime (当前 bar 开始时间)

触发条件：
  actionable = (action in buy/sell)
             AND (confidence >= min_preview_confidence=0.55)
             AND (bar_progress >= min_preview_bar_progress=0.2)

转换规则：
  ① not actionable AND previous != idle
      → idle + emit "cancelled"

  ② actionable AND action changed
      → preview_{action} + emit "preview_{action}"

  ③ actionable AND same action AND stable < 15s
      → 静默（不发出事件）

  ④ actionable AND same action AND stable ≥ 15s
      → armed_{action} + emit "armed_{action}"
```

**bar_progress** 语义：`elapsed_seconds / timeframe_seconds`，防止 bar 刚开始时的噪声信号。

### 7.2 Confirmed 路径

`_transition_confirmed()` 在每根 bar close 时执行：

```
① action = "hold" AND previous_confirmed_state != "idle"
    → confirmed_state = "idle"
    → emit "confirmed_cancelled"（通知上层持仓已转向 hold）

② action = "hold" AND previous = "idle"
    → confirmed_state = "idle"（静默，不发出事件）

③ action = "buy" | "sell"
    → confirmed_state = "confirmed_{action}"
    → emit "confirmed_{action}"
```

`preview_state_at_close` 记录 bar 收盘前瞬间的 preview 状态，供 `TradeExecutor` 的 `require_armed` 检查使用（已 armed 才允许自动下单）。

Voting 前还会先做一次同 bar 同策略融合：
- `confirmed` 优先于 `intrabar`
- 同 scope 下保留最后一次结果
- 每个策略每根 bar 最多贡献一次 vote

### 7.3 冷却机制

`_should_emit()` 防止相同状态在短时间内重复发出事件：

- `cooldown_seconds`（preview 路径，默认 30s）：相同 `(signal_state, bar_time)` 至少间隔 30s 才重复发出
- confirmed 路径：相同 `(signal_state, bar_time)` 只发出一次（cooldown=0）

### 7.4 状态恢复（服务重启）

`_restore_state()` 在 `start()` 时从 `SignalRepository` 读取最近信号记录，恢复各目标的状态：

- `confirmed_buy/sell` → 恢复 confirmed 状态
- `preview/armed` → 仅在同一 bar 内有效（跨 bar 不恢复）
- `confirmed_cancelled` → 重置为 idle

---

## 8. Voting Engine

### 8.1 设计动机

单一策略信号噪声大。1 个策略 buy 和 9 个策略 buy 在 SignalRuntime 看来没有区别。

`StrategyVotingEngine` 在所有策略评估完成后，对同一快照进行加权投票：
- 高度共识 → 更高置信度的 `consensus` 信号
- 多空分歧 → 降低置信度或不产生信号

### 8.2 算法

```python
buy_score  = Σ(confidence[i] for decision[i].action == "buy") / Σ all_confidence
sell_score = Σ(confidence[i] for decision[i].action == "sell") / Σ all_confidence

if buy_score >= consensus_threshold:
    action = "buy"
    score  = buy_score
elif sell_score >= consensus_threshold:
    action = "sell"
    score  = sell_score
else:
    return None  # 无共识

disagreement = min(buy_score, sell_score) / 0.5 × disagreement_penalty
confidence   = score × (1.0 - disagreement)
```

### 8.3 两种模式

**单 consensus 模式（向后兼容）**：

```python
policy = SignalPolicy(voting_enabled=True, voting_groups=[])
# → 所有非复合策略参与投票（CompositeSignalStrategy 默认排除，避免重复计票）
# → 产生 strategy="consensus" 信号
```

**多 voting group 模式**：

```python
policy = SignalPolicy(
    voting_groups=[
        VotingGroupConfig(
            name="trend_vote",
            strategies=frozenset(["supertrend", "ema_ribbon", "macd_momentum", "hma_cross", "session_momentum"]),
            consensus_threshold=0.45,
            min_quorum=2,
        ),
        VotingGroupConfig(
            name="reversion_vote",
            strategies=frozenset(["rsi_reversion", "stoch_rsi", "williams_r", "cci_reversion", "price_action_reversal"]),
            consensus_threshold=0.40,
        ),
        VotingGroupConfig(
            name="breakout_vote",
            strategies=frozenset(["bollinger_breakout", "donchian_breakout", "keltner_bb_squeeze", "squeeze_release", "fake_breakout"]),
            consensus_threshold=0.45,
            min_quorum=2,
        ),
    ]
)
# → 多组模式启用时，全局 consensus 自动禁用
# → 产生 strategy="trend_vote" / "reversion_vote" / "breakout_vote" 信号
```

### 8.4 Regime 稳定性乘数

投票结果的置信度还会乘以当前 Regime 的稳定性乘数：

```python
adjusted_confidence = min(1.0, vote_confidence × regime_stability)
```

---

## 9. CompositeSignalStrategy

### 9.1 与 VotingEngine 的区别

| 维度 | VotingEngine | CompositeSignalStrategy |
|------|-------------|------------------------|
| 作用范围 | 全局（所有策略） | 局部（2-4 个相关指标） |
| 输出名称 | "consensus" 或 group.name | 自定义名称 |
| 参与投票 | 不参与（避免重复计票） | 作为普通策略参与 VotingEngine |

### 9.2 组合模式

| 模式 | 语义 |
|------|------|
| `all_agree` | 所有子策略方向一致才发信号（高精度低频率） |
| `majority` | 超半数方向一致（平衡精度与频率） |
| `weighted_sum` | 按置信度加权计票（子策略置信度差异大时更公平） |

### 9.3 内置复合策略

```python
# build_trend_triple_confirm()
CompositeSignalStrategy(
    name="trend_triple_confirm",
    sub_strategies=[SmaTrendStrategy(), MacdMomentumStrategy(), SupertrendStrategy()],
    combine_mode="all_agree",
    regime_affinity={TRENDING: 1.00, RANGING: 0.10, BREAKOUT: 0.50, UNCERTAIN: 0.40},
)

# build_breakout_double_confirm()
CompositeSignalStrategy(
    name="breakout_double_confirm",
    sub_strategies=[BollingerBreakoutStrategy(), KeltnerBollingerSqueezeStrategy()],
    combine_mode="majority",
    regime_affinity={TRENDING: 0.35, RANGING: 0.20, BREAKOUT: 1.00, UNCERTAIN: 0.55},
)

# config/composites.json
CompositeSignalStrategy(
    name="breakout_release_confirm",
    sub_strategies=[DonchianBreakoutStrategy(), SqueezeReleaseFollow()],
    combine_mode="all_agree",
    regime_affinity={TRENDING: 0.35, RANGING: 0.15, BREAKOUT: 1.00, UNCERTAIN: 0.50},
)

CompositeSignalStrategy(
    name="reversal_rejection_confirm",
    sub_strategies=[FakeBreakoutDetector(), PriceActionReversal()],
    combine_mode="all_agree",
    regime_affinity={TRENDING: 0.10, RANGING: 1.00, BREAKOUT: 0.45, UNCERTAIN: 0.60},
)
```

---

## 10. 诊断与质量报告

`SignalModule` 提供多种诊断接口（均通过 `/signals` API 暴露）：

### 10.1 策略诊断（`strategy_diagnostics`）

识别策略间冲突、频繁 hold、低置信度等问题：

```json
{
    "strategy_breakdown": [
        {
            "strategy": "sma_trend",
            "buy_count": 12,
            "sell_count": 8,
            "hold_count": 180,
            "conflict_rate": 0.10,
            "avg_confidence": 0.62,
            "expectancy": 0.34,
            "payoff_ratio": 1.8
        }
    ],
    "warnings": ["high_hold_rate: sma_trend hold_rate=0.90"],
    "recommendations": []
}
```

### 10.2 日质量报告（`daily_quality_report`）

按时间维度聚合，识别当日信号质量趋势。

### 10.3 Regime 报告（`regime_report`）

```json
{
    "regime": "trending",
    "adx": 28.5,
    "bb_width": 0.012,
    "squeeze": false,
    "symbol": "XAUUSD",
    "timeframe": "H1",
    "stability": {
        "current_regime": "trending",
        "stability_score": 0.85,
        "transitions": 2
    }
}
```

### 10.4 Expectancy 分析（`strategy_expectancy`）

```
Expectancy = win_rate × avg_win - (1 - win_rate) × avg_loss
```

负 Expectancy 的策略会触发 `recommendations` 告警。

---

## 11. 常见问题

### Q: 策略注册时报 AttributeError？

检查四个必填属性是否都在**类级别**声明（不是实例属性）：
```python
class MyStrategy:
    name = "xxx"                    # 类属性，不是 self.name = "xxx"
    required_indicators = ("a",)
    preferred_scopes = ("confirmed",)
    regime_affinity = {RegimeType.TRENDING: 1.0, ...}  # 四个 key 都要有
```

### Q: 策略总是返回 hold，信号从不触发？

1. 检查 `required_indicators` 中的指标是否在 `config/indicators.json` 中存在且 `enabled=true`
2. 检查 `preferred_scopes` 是否与当前运行的 scope 一致（`enable_intrabar=False` 时 intrabar scope 不触发）
3. 检查 `regime_affinity` 是否太低（× 之后 < 0.55 会被过滤）
4. 检查 `config/signal.ini` 的 `[strategy_timeframes]` 是否白名单限制了时间框架

### Q: consensus 信号怎么获取？

```python
# API
GET /signals/recent?strategy=consensus&scope=confirmed

# 代码
signal_module.recent_consensus_signals(symbol="XAUUSD", timeframe="H1")
```

### Q: 如何启用多组 voting group？

在 `src/api/factories/signals.py` 中构建 `SignalPolicy` 时配置 `voting_groups`（见 8.3）。

### Q: 置信度校准何时启用？

`ConfidenceCalibrator` 在运行时默认启用轻量混合校准，当前参数为 `alpha=0.15`、`min_samples=50`、`recency_hours=8`。需要积累足够的交易历史（通过 `SignalQualityTracker`）后才会生效；样本不足时会自动退化为不校准。近期胜率弱于基准时，运行时会禁止 boost，只保留压制能力。

### Q: HTFStateCache 是什么？

`HTFStateCache`（`src/signals/strategies/htf_cache.py`）缓存高时间框架（如 H4、D1）的指标快照，供低时间框架策略（如 M15）访问更大周期的趋势状态，实现跨时间框架分析而无需重复计算。

### Q: MarketStructureAnalyzer 如何在策略中访问？

策略通过 `context.metadata.get("market_structure")` 访问：

```python
def evaluate(self, context: SignalContext) -> SignalDecision:
    structure = context.metadata.get("market_structure") or {}
    nearest_support = structure.get("nearest_support")
    # ...
```

---

## 当前运行补充

- `SignalFilterChain` now supports `SessionTransitionFilter`, so a configured cooldown can block signal evaluation during the London -> New York handoff.
- `SignalRuntime` reuses confirmed market-structure context for intrabar processing and tracks cache size in `runtime.status()`.
- `market_structure_m1_lookback_bars` allows M1 analysis to use a shorter lookback than higher timeframes.
- `TradeExecutor` enforces `max_concurrent_positions_per_symbol` and applies timeframe-specific ATR stop/target defaults.
- `PositionManager` can close all tracked positions at a configured UTC cutoff via `end_of_day_close_*` settings.
- `PreTradeRiskService` now blocks on `daily_loss_limit_pct`, and the trade APIs surface that case with the `daily_loss_limit` error code.
- `GET /signals/monitoring/quality/{symbol}/{timeframe}` combines regime diagnostics with confirmed-signal quality metrics for runtime monitoring.
