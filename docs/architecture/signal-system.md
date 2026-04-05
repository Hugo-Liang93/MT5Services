# 信号系统设计文档

> 面向策略开发者的深度参考。涵盖信号系统的设计原理、完整流程、扩展规范。

---

## 1. 模块结构

```
src/signals/
├── service.py                 # SignalModule (策略注册 + evaluate())
├── models.py                  # SignalEvent / SignalContext / SignalDecision
├── confidence.py              # 置信度管线纯函数
├── orchestration/
│   ├── runtime.py             # SignalRuntime 协调器 (生命周期 + 队列)
│   ├── runtime_evaluator.py   # 策略评估 + confidence 调整 + 信号发布
│   ├── runtime_processing.py  # 事件出队 + filter/regime + 单事件处理
│   ├── runtime_recovery.py    # 运行态恢复 (confirmed/preview 状态还原)
│   ├── policy.py              # SignalPolicy + VotingGroupConfig
│   ├── voting.py              # StrategyVotingEngine
│   ├── htf_resolver.py        # HTF 配置解析与对齐乘数 (纯函数)
│   ├── state_machine.py       # 状态机转换 (纯逻辑)
│   ├── vote_processor.py      # 投票处理 (纯函数)
│   └── affinity.py            # Regime 亲和度 + 快速拒绝 (纯函数)
├── strategies/
│   ├── base.py                # SignalStrategy Protocol + TimeframeScaler + get_tf_param()
│   ├── catalog.py             # build_named_strategy_catalog() (41+ 策略统一构建)
│   ├── registry.py            # StrategyRegistry + build_composite_strategies()
│   ├── trend.py               # 趋势策略 (7个)
│   ├── mean_reversion.py      # 均值回归策略 (6个)
│   ├── breakout.py            # 突破/波动率策略 (6个)
│   ├── session.py             # 时段动量策略
│   ├── price_action.py        # 价格行为策略
│   ├── multi_tf_entry.py      # HTFTrendPullback / DualTFMomentum (跨TF策略)
│   ├── m5_scalp.py            # M5ScalpRSI / M5MomentumBurst
│   ├── trendline.py           # TrendlineThreeTouch
│   ├── composite.py           # CompositeStrategy 基类
│   ├── adapters.py            # UnifiedIndicatorSourceAdapter
│   ├── htf_cache.py           # HTFStateCache
│   └── tf_params.py           # TFParamResolver
├── evaluation/
│   ├── regime.py              # MarketRegimeDetector + SoftRegimeResult
│   ├── calibrator.py          # ConfidenceCalibrator (历史胜率校准)
│   ├── performance.py         # StrategyPerformanceTracker (日内绩效)
│   └── indicators_helpers.py  # 指标提取工具函数
├── execution/
│   └── filters.py             # SignalFilterChain
├── tracking/
│   └── repository.py          # SignalRepository (持久化)
├── analytics/
│   ├── diagnostics.py         # DiagnosticsEngine
│   ├── interfaces.py          # DiagnosticsEngine Protocol
│   └── plugins.py             # AnalyticsPluginRegistry
└── contracts/
    └── sessions.py            # 交易时段常量
```

---

## 2. 评估流程

### 2.1 触发路径

```
IndicatorManager 发布快照
    ↓
SignalRuntime._on_snapshot(symbol, timeframe, bar_time, indicators, scope)
    ├─ scope="confirmed" → _confirmed_events.put()   (不可丢弃)
    └─ scope="intrabar"  → _intrabar_events.put()    (可丢弃)
```

### 2.2 主循环

```
1. 优先取 confirmed 队列, 空则等 intrabar
2. SignalFilterChain 全局过滤 (时段/点差/经济事件/波动率)
3. Regime 检测 (每快照仅一次)
4. _evaluate_strategies()
5. _process_voting()
```

### 2.3 策略评估 (`_evaluate_strategies`)

对每个注册策略依次执行：

```
① session 白名单      → skip if not allowed
② timeframe 白名单    → skip if not allowed
③ scope 匹配          → skip if scope ∉ preferred_scopes
④ 指标完整性          → skip if missing required_indicators
⑤ Affinity gate       → skip if affinity < 0.15 (不调用 evaluate)
⑥ Snapshot 去重       → skip if same bar_time + signature
⑦ service.evaluate()  → raw_confidence × affinity → calibrator → final
⑧ 状态机转换          → confirmed / intrabar 路径
⑨ 持久化 + 事件发布   → 仅在状态转换时
```

---

## 3. 策略开发规范

### 3.1 四个必填属性

```python
class MyStrategy:
    name = "my_strategy"                          # 全局唯一
    required_indicators = ("adx14", "rsi14")      # 对应 indicators.json
    preferred_scopes = ("confirmed",)             # "confirmed" 和/或 "intrabar"
    regime_affinity = {
        RegimeType.TRENDING:  1.00,
        RegimeType.RANGING:   0.20,
        RegimeType.BREAKOUT:  0.50,
        RegimeType.UNCERTAIN: 0.50,
    }
```

注册时 `SignalModule._validate_strategy_attrs()` 自动校验。

### 3.2 evaluate() 接口

```python
def evaluate(self, context: SignalContext) -> SignalDecision:
    # context.indicators  — 已过滤的指标快照
    # context.metadata    — 含 _regime, bar_time, scope, market_structure
    # context.htf_indicators — HTF 数据 (由 signal.ini 配置驱动)
    return SignalDecision(direction="buy", confidence=0.72, reason="...", metadata={})
```

- `confidence` 反映规则信号强度，Regime 由外层 affinity 处理
- `direction="hold"` 表示无信号
- 不要在策略内硬截断 Regime

### 3.3 Regime 亲和度参考

| 策略类型 | TRENDING | RANGING | BREAKOUT | UNCERTAIN |
|---------|---------|---------|---------|---------|
| 趋势跟踪 | 1.00 | 0.10–0.30 | 0.40–0.60 | 0.50 |
| 均值回归 | 0.20–0.30 | 1.00 | 0.30–0.40 | 0.60 |
| 突破/波动率 | 0.30–0.90 | 0.15–0.55 | 1.00 | 0.45–0.65 |

### 3.4 新增步骤

1. 在对应策略文件中实现类 (trend.py / mean_reversion.py / breakout.py / ...)
2. 在 `src/signals/strategies/__init__.py` 中导出
3. 在 `src/signals/service.py` 默认策略列表中注册
4. 确认 `config/indicators.json` 包含所需指标
5. 在 `tests/signals/` 中添加单元测试 (覆盖四种 Regime)
6. (可选) 在 `config/signal.ini` 配置参数

### 3.5 Intrabar 决策

```
新策略依赖"盘中实时状态"?
  (超买超卖/通道触边/实时极值)
  ├─ YES → preferred_scopes = ("intrabar", "confirmed")
  └─ NO  → preferred_scopes = ("confirmed",)
```

Intrabar 指标集合由策略 `preferred_scopes` + `required_indicators` 在启动时**自动推导**，无需手动配置。

---

## 4. Regime 系统

### 4.1 硬分类 (`detect()`)

```
优先级:
  1. Keltner-Bollinger Squeeze (BB 完全在 KC 内) → BREAKOUT
  2. ADX ≥ 23                                    → TRENDING
  3. ADX < 18 且 BB 宽度 < 0.8%                  → BREAKOUT (蓄力)
  4. ADX < 18                                    → RANGING
  5. 18 ≤ ADX < 23                               → UNCERTAIN
  6. 无数据                                       → UNCERTAIN (兜底)
```

### 4.2 概率化分类 (`detect_soft()`)

```python
SoftRegimeResult:
    primary: RegimeType                     # 主分类 (向后兼容)
    probabilities: dict[RegimeType, float]   # 概率分布, 总和=1.0

# effective_affinity = Σ(prob[r] × affinity[r])
```

---

## 5. 置信度管线

```
raw_confidence (策略输出)
    × effective_affinity                    (Regime 结构过滤)
    × session_performance_multiplier        (日内实时状态)
    → ConfidenceCalibrator                  (长期历史校准)
    → max(confidence_floor, result)         (底线保护)
    × intrabar_confidence_factor            (scope=intrabar 时 ×0.85)
    × htf_alignment_multiplier              (对齐 ×1.10 / 冲突 ×0.70)
    = final_confidence
```

分阶段校准：`<50笔` 不校准, `50-100` alpha=0.10, `100+` alpha=0.15。

---

## 5.5 StrategyPerformanceTracker — 日内绩效追踪

**模块**：`src/signals/evaluation/performance.py`

纯内存的实时反馈层，置信度管线中的 `session_performance_multiplier` 来源。

### 重启恢复

`PerformanceTracker` 启动时通过 `warm_up_from_db()` 从 DB 恢复当天状态，避免重启后学习归零：

```
AppRuntime.start()
    → _start_performance_tracker()
        → signal_repo.fetch_recent_outcomes(hours=24)   # UNION ALL: signal_outcomes + trade_outcomes
        → perf.warm_up_from_db(rows)                    # 按 recorded_at 升序重放
        → 恢复后: wins/losses/streak/PnL 与重启前一致
```

非致命：DB 不可用时 catch Exception → debug log，不阻塞启动。

### PnL 熔断器

独立于 `TradeExecutor` 的技术熔断器，计实际亏损次数（非 API 失败）：

```
record_outcome(source="trade", won=False)
    → _global_trade_loss_streak += 1
    → 达到 pnl_circuit_max_consecutive_losses (默认 5)
        → _pnl_circuit_paused = True
        → 记录 _pnl_circuit_opened_at

is_trading_paused()           ← TradeExecutor._handle_confirmed() 中调用
    → True: 跳过下单 (notify_skip: "pnl_circuit_paused")
    → 超过 cooldown_minutes (默认 120min) 自动复位
    → 任意一笔盈利 (won=True) 重置连败计数器
```

**配置**（`signal.ini [pnl_circuit_breaker]`）：

| 参数 | 默认 | 说明 |
|------|------|------|
| `enabled` | `true` | 总开关 |
| `max_consecutive_losses` | `5` | 触发阈值 |
| `cooldown_minutes` | `120` | 自动恢复等待时间 |

### 两个熔断器对比

| 维度 | TradeExecutor 技术熔断 | PnL 熔断（PerformanceTracker） |
|------|----------------------|-------------------------------|
| 计数对象 | MT5 API 失败次数 | 实际平仓亏损次数 |
| 状态位置 | `TradeExecutor._circuit_open` | `PerformanceTracker._pnl_circuit_paused` |
| 阈值 | `max_consecutive_failures = 3` | `max_consecutive_losses = 5` |
| 自动恢复 | `circuit_auto_reset_minutes = 30` | `cooldown_minutes = 120` |
| 重启恢复 | 不恢复（API 错误不持久化） | 从 DB 恢复（trade_outcomes 表）|
| INI section | `[circuit_breaker]` | `[pnl_circuit_breaker]` |

---

## 6. 状态机

每个 `(symbol, timeframe, strategy)` 独立维护状态：

**Intrabar 路径**:
```
idle → preview_buy/sell (方向改变 + conf≥0.55 + bar_progress≥0.2)
     → armed_buy/sell   (方向稳定 ≥ 15s)
     → idle             (置信度降低 → cancelled)
```

**Confirmed 路径**:
```
任意 → confirmed_buy/sell      (bar close 时方向为 buy/sell)
     → confirmed_cancelled     (上一根有信号, 本根转 hold)
     → idle                    (无信号)
```

---

## 7. Voting Engine

### 两种模式

**单 consensus**: `SignalPolicy(voting_enabled=True, voting_groups=[])` → 所有策略投票 → `strategy="consensus"`

**多 voting group**: 每组独立投票 → 产生 group.name 信号 → 全局 consensus 自动禁用

### 算法

```
buy_score  = Σ conf(buy)  / Σ conf(all)
sell_score = Σ conf(sell) / Σ conf(all)

score ≥ consensus_threshold (0.40) → emit signal
confidence = score × (1.0 - disagreement_factor) × regime_stability
```

---

## 8. 复合策略

| 策略 | 子策略 | Scope | 模式 |
|------|--------|-------|------|
| `trend_triple_confirm` | supertrend + macd_momentum + sma_trend | confirmed | majority |
| `breakout_double_confirm` | donchian_breakout + keltner_bb_squeeze + squeeze_release | confirmed | all_agree |
| `reversion_double_confirm` | rsi_reversion + stoch_rsi | intrabar + confirmed | all_agree |
| `breakout_release_confirm` | donchian_breakout + squeeze_release | confirmed | all_agree |
| `reversal_rejection_confirm` | fake_breakout + price_action_reversal | confirmed | all_agree |

组合模式: `all_agree` (全部一致) / `majority` (过半) / `weighted_sum` (加权)。

复合策略由 `config/composites.json` 声明式配置，通过 `build_composite_strategies()` 动态构建。不参与 VotingEngine 投票 (避免重复计票)。

---

## 9. HTF 指标注入

通过 `signal.ini [strategy_htf]` 配置，策略代码不声明 HTF 属性：

```ini
[strategy_htf]
supertrend.supertrend14 = H1
sma_trend.sma20 = D1
```

策略中按需消费:

```python
h1 = context.htf_indicators.get("H1", {})
h1_adx = h1.get("adx14", {}).get("adx")
```

未配置时为空 dict，安全跳过。HTF 指标不需额外计算 — IndicatorManager 已为所有配置的 `(symbol, timeframe)` 计算全量指标。

---

## 10. Signal Listener 架构

```
SignalRuntime._publish_signal_event(event)
    ├─→ TradeExecutor.on_signal_event()         (自动下单)
    ├─→ SignalQualityTracker.on_signal_event()   (信号质量追踪)
    └─→ HTFStateCache.on_signal_event()          (HTF 方向缓存)
```

### 信号质量 vs 交易结果

| 维度 | SignalQualityTracker | TradeOutcomeTracker |
|------|---------------------|---------------------|
| 衡量 | 信号预测质量 (N bars 后方向) | 实际交易盈亏 |
| 评估对象 | 所有 confirmed 信号 | 仅实际执行的交易 |
| 写入表 | `signal_outcomes` | `trade_outcomes` |
| 消费者 | ConfidenceCalibrator (长期) | PerformanceTracker (日内) |
