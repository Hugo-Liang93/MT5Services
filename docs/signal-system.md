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
│   ├── catalog.py             # build_named_strategy_catalog() (42+ 策略统一构建)
│   ├── registry.py            # StrategyRegistry + build_composite_strategies()
│   ├── trend.py               # 趋势策略 (7个)
│   ├── mean_reversion.py      # 均值回归策略 (6个)
│   ├── breakout.py            # 突破/波动率策略 (6个)
│   ├── volatility_structure.py # RangeBoxBreakout + BarMomentumSurge (波动率结构策略)
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

### 3.6 Intrabar 指标自动推导机制

```
intrabar 指标集合 = 所有满足以下条件的指标并集：
    "intrabar" ∈ strategy.preferred_scopes  AND  该指标 ∈ strategy.required_indicators

推导流程（src/app_runtime/factories/signals.py 启动时执行）：
  SignalModule.intrabar_required_indicators()
    → 遍历所有策略，收集 preferred_scopes 含 "intrabar" 的策略的 required_indicators 并集
    → 注入到 UnifiedIndicatorManager.set_intrabar_eligible_override()
    → indicator manager 的 intrabar pipeline 仅计算该集合中的指标
```

**当前自动推导结果（8 个）**：

| 指标 | 来源策略 | 盘中语义 |
|------|---------|---------|
| `rsi14` | rsi_reversion | 超买超卖是实时状态，盘中触极值即预警 |
| `stoch_rsi14` | stoch_rsi | 同上 |
| `williamsr14` | williams_r | 同上 |
| `cci20` | cci_reversion | 同上 |
| `boll20` | bollinger_breakout, keltner_bb_squeeze, breakout_double_confirm | 盘中触及/突破通道边界即可预警 |
| `keltner20` | keltner_bb_squeeze | BB/KC 挤压是实时状态 |
| `donchian20` | breakout_double_confirm | 当前 bar 只能扩大通道（不会收窄），盘中值单调可信 |
| `adx14` | breakout_double_confirm | ADX 变化缓慢，盘中值与收盘差距极小 |

其余 confirmed-only 指标（sma20, ema9/21/50/55, hma20, rsi5, macd, macd_fast, roc12, supertrend14, atr14, stoch14）仅在 confirmed 链路计算。

### 3.7 指标语义分析（intrabar 适用性判断依据）

| 指标类别 | 代表指标 | 盘中语义 | 适合 intrabar |
|---------|---------|---------|:------------:|
| 移动均线（MA/EMA/HMA） | sma20, ema9/50, hma20 | 当前 bar 未收盘时 close 是最新 tick 价，均线随 tick 频繁波动，无收盘意义 | **No** |
| 趋势跟踪（Supertrend） | supertrend14 | 基于 ATR 的价格通道，收盘前方向可频繁翻转，给出假信号 | **No** |
| 动量趋势（MACD/ROC） | macd, macd_fast, roc12 | 以 EMA 为基础，同样受未收盘价格噪声影响 | **No** |
| 振荡器（RSI/CCI/Williams/StochRSI） | rsi14, cci20, williamsr14, stoch_rsi14 | 超买超卖是**实时状态**，盘中触极值比收盘才知道更有价值 | **Yes** |
| 波动率通道（Bollinger/Keltner） | boll20, keltner20 | 价格盘中触及/突破通道边界本身就是信号，无需等待收盘 | **Yes** |
| 趋势通道（Donchian） | donchian20 | 当前 bar 只能**扩大**通道（不会收窄），盘中值单调可信 | **Yes** |
| 趋势强度（ADX） | adx14 | ADX 变化缓慢，盘中值与收盘值差距极小，可信 | **Yes** |
| 波动率基准（ATR） | atr14 | 消费方（sizing/fake_breakout）全在 confirmed 时执行；keltner20 内部自行计算 ATR | **No** |

### 3.8 策略 scope 经验判断表

| 策略类型 | preferred_scopes | 代表指标 | 原因 |
|---------|:---------------:|---------|------|
| 均线交叉（MA Cross） | confirmed | sma/ema/hma | 均线需要收盘价定型，盘中值噪声大 |
| 趋势跟踪（Supertrend/ROC） | confirmed | supertrend14/roc12 | 趋势方向收盘才稳定 |
| MACD 动量 | confirmed | macd | EMA 底层，收盘前频繁变动 |
| 价格行为（K 线形态） | confirmed | atr14 | 形态必须 K 线收盘才能确认完整 |
| 时段动量 | confirmed | atr14/supertrend14 | 基于已收盘 K 线统计规律 |
| **RSI/CCI/Williams/StochRSI** | **intrabar + confirmed** | rsi14/cci20/williamsr14/stoch_rsi14 | 超买超卖是实时状态，盘中触值即可预警 |
| **Bollinger 触边** | **intrabar + confirmed** | boll20 | 价格触及/突破通道边界不需要等收盘 |
| **Keltner 挤压** | **intrabar + confirmed** | boll20/keltner20 | BB 完全在 KC 内是实时状态 |
| Donchian 突破（需站稳） | confirmed | donchian20 | 需收盘确认站稳通道外，防假突破 |

### 3.9 TFParamResolver — Per-TF 策略参数

策略参数按时间框架独立配置。查找优先级：`[strategy_params.<TF>]` → `[strategy_params]` → 策略代码 default。

```ini
# 全局默认（所有 TF 兜底）
[strategy_params]
rsi_reversion__overbought = 78
supertrend__adx_threshold = 21

# M5 特化
[strategy_params.M5]
rsi_reversion__overbought = 72
rsi_reversion__oversold = 25
```

**键格式**：双下划线 `__` 分隔策略名和参数名。策略中通过 `get_tf_param(self, "overbought", context.timeframe, default)` 查表。

**核心文件**：`src/signals/strategies/tf_params.py`（TFParamResolver）、`src/signals/strategies/base.py`（get_tf_param）、`src/config/signal.py`（加载 section）、`src/app_runtime/factories/signals.py`（构建注入）。

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

### 两种模式（互斥）

**单 consensus**: `voting_enabled=True` 且 `voting_groups=[]` → 所有策略投票 → `strategy="consensus"`

**多 voting group**: `voting_groups` 非空 → 每组独立投票 → 产生 group.name 信号 → **全局 consensus 自动禁用**（`_voting_engine = None`）

当前配置为**多 voting group 模式**（4 组）。两种模式不会同时存在。

### 组内策略不能独立发信号（三层保护）

**加入 voting group 的策略，默认丧失独立发出交易信号的能力**。它们的评估结果只汇入组投票，不单独触发交易：

1. **SignalRuntime 层**（`runtime_evaluator.py`）：组员 decision 不 persist、不 publish signal event，直接 return
2. **BacktestEngine 层**（`runner.py`）：组员 decision 只 record_evaluation（统计），不 process_decision（开仓）
3. **ExecutionGate 兜底**（`gate.py`）：即使 signal event 泄漏，gate 以 `"voting_group_member"` 理由阻止下单

**例外**：`standalone_override` 集合中的策略可豁免（既参与投票又保留独立下单能力）。当前无豁免策略。

成员集合构建：
```python
_voting_group_members = frozenset(
    name for group in policy.voting_groups for name in group.strategies
) - policy.standalone_override
```

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
