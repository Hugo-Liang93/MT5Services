# MT5Services — 系统架构文档

> 面向新成员和 AI 助手的项目快速上手指南。阅读本文档后应能理解：系统由哪些模块组成、数据如何流转、如何安全扩展。

---

## 目录

1. [系统概览](#1-系统概览)
2. [目录结构](#2-目录结构)
3. [启动流程](#3-启动流程)
4. [数据流](#4-数据流)
5. [核心模块详解](#5-核心模块详解)
6. [信号系统](#6-信号系统)
7. [配置系统](#7-配置系统)
8. [API 层](#8-api-层)
9. [风控层](#9-风控层)
10. [持久化层](#10-持久化层)

---

## 1. 系统概览

MT5Services 是一个**生产级** FastAPI 量化交易平台，通过 MT5 Python 绑定连接 MetaTrader 5 终端。

**技术栈**：
- Python 3.9–3.12 / FastAPI + uvicorn
- MetaTrader 5（Windows-only Python binding）
- TimescaleDB（PostgreSQL + 时序扩展）
- threading（所有后台任务）

**核心能力**：
- 实时行情采集与内存缓存
- 多指标计算流水线
- 多策略信号生成与复合策略编排
- 市场状态感知（Regime 分类）
- 跨策略投票共识（Voting Engine）
- 置信度校准（历史胜率反馈）
- 自动交易执行（TradeExecutor）
- 多层风险控制
- 经济日历过滤

---

## 2. 目录结构

```
MT5Services/
├── app.py                    # 启动入口（解析参数，启动 uvicorn）
├── config/                   # 所有配置文件
│   ├── app.ini               # 品种/时间框架（全局单一来源）
│   ├── signal.ini            # 信号运行时参数
│   ├── indicators.json       # 指标定义
│   └── composites.json       # 复合策略定义
└── src/
    ├── api/                  # FastAPI 路由 + DI 容器
    │   ├── deps.py           # FastAPI DI 适配层（getter 函数 + lifespan）
    │   └── factories/        # 组件工厂函数
    ├── calendar/             # 经济日历服务
    ├── clients/              # MT5 客户端（市场/账户/交易）
    ├── config/               # 配置加载 + Pydantic 模型
    ├── indicators/           # 指标引擎
    ├── ingestion/            # 后台数据采集
    ├── market/               # MarketDataService（内存缓存）
    ├── market_structure/     # 市场结构分析
    ├── monitoring/           # 健康检查 + 监控
    ├── persistence/          # TimescaleDB 写入器 + Schema
    ├── risk/                 # 前置风控服务
    ├── signals/              # 信号系统（核心）
    └── trading/              # 交易执行 + 持仓管理
```

---

## 3. 启动流程

入口：`app.py` → `uvicorn` → `src/api/__init__.py` → `src/api/deps.py` → `src/app_runtime/`

```
src/api/deps.py  _ensure_initialized()
    │
    ├─ build market domain       # MarketDataService / StorageWriter / BackgroundIngestor
    ├─ build trading domain      # EconomicCalendarService / TradingModule / registry
    ├─ build signal domain       # MarketStructureAnalyzer / SignalModule / SignalRuntime
    ├─ wire trackers             # HTFStateCache / SignalQualityTracker / TradeOutcomeTracker
    └─ build monitoring domain   # HealthMonitor / MonitoringManager

src/app_runtime/runtime.py  AppRuntime.start() / stop()
    │
    ├─ 1. storage.start()
    ├─ 2. ingestion.start()
    ├─ 3. economic_calendar.start()
    ├─ 4. indicators.start()
    ├─ 5. signals.start()
    ├─ 6. position_manager.start() + sync_open_positions()
    └─ 7. monitoring.start()
```

关闭顺序相反（`shutdown_components()`）。

所有组件通过 `AppContainer`（`src/app_runtime/container.py`）持有，`src/api/deps.py` 作为 FastAPI DI 适配层暴露 getter 函数。

---

## 4. 数据流

### 4.1 行情采集链路

```
MT5 Terminal（Windows）
    │
    ▼  每 interval 秒（config/app.ini）
BackgroundIngestor
    │
    ├─── MarketDataService（内存，src/market/）
    │       └─ Tick / Quote / OHLC / Intrabar 状态
    │
    └─── StorageWriter（异步队列）
             └─ TimescaleDB（持久化备份）
```

`MarketDataService` 是**运行时数据的唯一权威**，TimescaleDB 仅用于历史持久化，不是主读路径。

### 4.2 指标计算链路

```
BackgroundIngestor（OHLC bar close 事件）
    │
    ▼
UnifiedIndicatorManager
    │
    ├─ 依赖图解析（config/indicators.json）
    ├─ 计算流水线（standard / incremental / parallel）
    └─ 指标快照发布
            │
            ▼
    IndicatorSnapshots（存入 result_store）
```

### 4.3 信号生成链路

```
IndicatorManager 快照发布
    │
    ▼
SignalRuntime._on_snapshot()
    │
    ├─ 写入 _confirmed_events 队列（bar-close，不可丢弃）
    └─ 写入 _intrabar_events 队列（盘中实时，可丢弃）

    ▼（后台线程 process_next_event）

1. Regime 检测（MarketRegimeDetector，每快照一次）
2. RegimeTracker 稳定性评分
3. 过滤链（SignalFilterChain：点差/时段/经济事件/时段切换冷却）
4. 策略评估（_evaluate_strategies）
   ├─ 检查 session 白名单
   ├─ 检查 timeframe 白名单
   ├─ 检查 scope 匹配
   ├─ Pre-flight affinity gate（affinity < 0.15 直接跳过）
   ├─ 去重检查（snapshot signature）
   ├─ service.evaluate()
   │   ├─ strategy.evaluate(context) → raw_confidence
   │   ├─ × regime_affinity → post_affinity_confidence
   │   └─ ConfidenceCalibrator → final_confidence
   └─ 状态机转换（_transition_confirmed / transition_intrabar）
5. 投票引擎（_process_voting）
   ├─ 多组模式：每组独立 VotingEngine → group.name 信号
   └─ 单 consensus 模式：全局 VotingEngine → "consensus" 信号
6. 持久化（SignalRepository）
7. 事件发布（_publish_signal_event → 监听器）
```

### 4.4 交易执行链路

```
SignalRuntime 发布 SignalEvent
    │
    ▼
TradeExecutor（监听 signal_event）
    │
    ├─ require_armed 检查（可选，intrabar 需要 armed 状态）
    ├─ auto_trade_min_confidence 检查
    ├─ SignalFilterChain 过滤
    ├─ PreTradeRiskService 风控
    └─ TradingService.place_order() → MT5
```

---

## 5. 核心模块详解

### 5.1 MarketDataService（`src/market/service.py`）

- **职责**：线程安全的内存行情缓存
- **锁**：`threading.RLock`，所有读写均须持锁
- **写入者**：仅 `BackgroundIngestor`
- **读取者**：API 路由、指标管理器、信号运行时
- **数据类型**：Tick、Quote、OHLC bars、Intrabar（滚动）

### 5.2 BackgroundIngestor（`src/ingestion/ingestor.py`）

- **职责**：后台多线程数据采集
- **采集类型**：Tick（实时）、Quote（报价）、OHLC（收盘确认）、Intrabar（分钟内滚动）
- **事件发布**：bar close → 触发 IndicatorManager 计算

### 5.3 UnifiedIndicatorManager（`src/indicators/manager.py`）

- **职责**：指标编排与快照发布
- **计算模式**：`standard`（全量重计算）/ `incremental`（追加更新）/ `parallel`（并行计算）
- **依赖管理**：`dependency_manager.py` 解析 `indicators.json` 依赖图，保证计算顺序
- **快照监听器**：收盘快照通知 `SignalRuntime._on_snapshot()`

### 5.4 SignalRuntime（`src/signals/orchestration/runtime.py`）

见 [第 6 节 信号系统](#6-信号系统)。

### 5.5 TradeExecutor（`src/trading/signal_executor.py`）

- **职责**：将 `SignalEvent` 转换为实际下单请求（异步队列 + daemon 工作线程）
- **配置**：`auto_trade_enabled`、`auto_trade_min_confidence`、`auto_trade_require_armed`
- **队列**：内部 Queue（默认 64，可配置），confirmed 信号有 1s backpressure 重试
- **流程**：信号入队 → 工作线程取出 → ExecutionGate 准入 → 风控 → 仓位计算 → 下单

### 5.6 PositionManager（`src/trading/position_manager.py`）

- **职责**：持仓生命周期监控、止损跟踪、自动平仓触发
- **后台**：独立线程，定期扫描持仓状态

### 5.7 SignalQualityTracker / TradeOutcomeTracker（`src/trading/`）

- **SignalQualityTracker**（`signal_quality_tracker.py`）：信号预测质量追踪（N bars 后方向正确性），写入 `signal_outcomes` 表
- **TradeOutcomeTracker**（`trade_outcome_tracker.py`）：实际交易盈亏追踪（由 PositionManager 关仓触发），写入 `trade_outcomes` 表
- **用途**：为 `ConfidenceCalibrator`（长期统计校准）和 `StrategyPerformanceTracker`（日内实时反馈）提供数据

### 5.8 MarketStructureAnalyzer（`src/market_structure/analyzer.py`）

- **职责**：识别支撑/阻力位、趋势结构
- **注入**：结果写入 `regime_metadata["market_structure"]`，策略通过 `context.metadata` 访问

### 5.9 EconomicCalendarService（`src/calendar/service.py`）

- **职责**：获取并缓存经济日历事件
- **子模块**：`calendar_sync.py`（数据同步）、`trade_guard.py`（高风险时段检测）、`observability.py`（监控）

---

## 6. 信号系统

### 6.1 模块层次

```
src/signals/
├── service.py              # SignalModule：策略注册 + evaluate()
├── orchestration/
│   ├── runtime.py          # SignalRuntime：事件主循环
│   ├── policy.py           # SignalPolicy + VotingGroupConfig
│   └── voting.py           # StrategyVotingEngine
├── strategies/
│   ├── base.py             # SignalStrategy Protocol
│   ├── trend.py            # 趋势策略（6个）
│   ├── mean_reversion.py   # 均值回归策略（4个）
│   ├── breakout.py         # 突破/波动率策略（6个）
│   ├── session.py          # 时段动量策略（1个）
│   ├── price_action.py     # 价格行为策略（1个）
│   ├── composite.py        # 复合策略与工厂函数
│   ├── adapters.py         # IndicatorSource 适配器
│   ├── htf_cache.py        # 高时间框架状态缓存
│   └── registry.py         # 策略注册表
├── evaluation/
│   ├── regime.py           # MarketRegimeDetector + RegimeTracker
│   ├── calibrator.py       # ConfidenceCalibrator（历史胜率校准）
│   ├── performance.py      # StrategyPerformanceTracker（日内绩效追踪）
│   └── indicators_helpers.py
├── execution/
│   └── filters.py          # SignalFilterChain
├── tracking/
│   └── repository.py       # SignalRepository
├── analytics/
│   ├── diagnostics.py      # DiagnosticsEngine
│   └── plugins.py
└── contracts/
    └── sessions.py         # 交易时段常量
```

### 6.2 策略清单

**趋势跟踪**（`trend.py`）：

| 策略 | 指标 | Scope |
|------|------|-------|
| `sma_trend` | sma20, ema50 | confirmed |
| `macd_momentum` | macd | confirmed |
| `supertrend` | supertrend14, adx14 | confirmed |
| `ema_ribbon` | ema9, hma20, ema50 | confirmed |
| `hma_cross` | hma20, ema50 | confirmed |
| `roc_momentum` | roc12, adx14 | confirmed |

**均值回归**（`mean_reversion.py`）：

| 策略 | 指标 | Scope |
|------|------|-------|
| `rsi_reversion` | rsi14 | intrabar + confirmed |
| `stoch_rsi` | stoch_rsi14 | intrabar + confirmed |
| `williams_r` | williamsr14 | intrabar + confirmed |
| `cci_reversion` | cci20 | intrabar + confirmed |

**突破/波动率**（`breakout.py`）：

| 策略 | 指标 | Scope |
|------|------|-------|
| `bollinger_breakout` | boll20 | intrabar + confirmed |
| `keltner_bb_squeeze` | boll20, keltner20 | intrabar + confirmed |
| `donchian_breakout` | donchian20, adx14 | confirmed |
| `fake_breakout` | donchian20, atr14 | confirmed |
| `squeeze_release` | boll20, keltner20, macd | confirmed |
| `multi_timeframe_confirm` | sma20, ema50 | confirmed |

**时段/价格行为**：

| 策略 | 指标 | Scope |
|------|------|-------|
| `session_momentum` | atr14, supertrend14 | confirmed |
| `price_action_reversal` | atr14 + recent bars | confirmed |

**复合策略**（`composite.py`）：

| 策略 | 子策略 | Scope | 模式 |
|------|--------|-------|------|
| `trend_triple_confirm` | supertrend + macd_momentum + sma_trend | confirmed | majority |
| `breakout_double_confirm` | bollinger_breakout + donchian_breakout + keltner_bb_squeeze | confirmed | all_agree |

### 6.3 Regime 分类

`MarketRegimeDetector`（`src/signals/evaluation/regime.py`）：

```
优先级：
  1. Keltner-Bollinger Squeeze → BREAKOUT（波动率压缩即将爆发）
  2. ADX ≥ 23               → TRENDING
  3. ADX < 18 + BB宽 < 0.8% → BREAKOUT（蓄力盘整）
  4. ADX < 18               → RANGING
  5. 18 ≤ ADX < 23          → UNCERTAIN
  6. 无数据                  → UNCERTAIN（兜底）
```

`RegimeTracker` 跟踪 Regime 切换稳定性，稳定的 Regime 给出更高的乘数（1.0 = 完全稳定）。

### 6.4 置信度修正流程

```
strategy.evaluate() → raw_confidence
    × regime_affinity[current_regime]
    → post_affinity_confidence
    → ConfidenceCalibrator.calibrate()（如果已启用）
    = final_confidence

→ final_confidence < 0.55：不产生状态转换（静默丢弃）
→ affinity < 0.15（min_affinity_skip）：不调用 evaluate()（预飞门）
```

### 6.5 信号状态机

每个 `(symbol, timeframe, strategy)` 三元组维护 `RuntimeSignalState`：

**Intrabar 状态（Preview）**：
```
idle
  → preview_buy/sell       [方向改变 + confidence≥0.55 + bar_progress≥0.2]
  → armed_buy/sell         [方向稳定 ≥ 15s]
  → cancelled              [置信度降低]
```

**Confirmed 状态（Bar Close）**：
```
任意
  → confirmed_buy/sell     [bar close 时方向为 buy/sell]
  → confirmed_cancelled    [上一根有信号，本根转 hold]
  → idle                   [hold，无动作]
```

### 6.6 Voting Engine

**单 consensus 模式**：
```python
SignalPolicy(voting_enabled=True, voting_groups=[])
# → 所有策略投票 → strategy="consensus" 信号
```

**多 voting group 模式**：
```python
SignalPolicy(voting_groups=[
    VotingGroupConfig(name="trend_vote", strategies=frozenset(["supertrend", "ema_ribbon", "macd_momentum", "hma_cross", "session_momentum"])),
    VotingGroupConfig(name="reversion_vote", strategies=frozenset(["rsi_reversion", "stoch_rsi", "williams_r", "cci_reversion", "price_action_reversal"])),
    VotingGroupConfig(name="breakout_vote", strategies=frozenset(["bollinger_breakout", "donchian_breakout", "keltner_bb_squeeze", "squeeze_release", "fake_breakout"])),
])
# → 每组独立投票 → strategy="trend_vote" / "reversion_vote" / "breakout_vote" 信号
# → 全局 consensus 自动禁用
```

**算法**：
```
buy_score  = Σ conf(buy)  / Σ conf(all)
sell_score = Σ conf(sell) / Σ conf(all)

if score ≥ consensus_threshold (default=0.40):
    emit signal with confidence = score × (1 - disagreement_factor)
```

---

## 7. 配置系统

### 7.1 配置文件优先级

```
config/*.local.ini  （最高，已 gitignore）
config/*.ini        （已提交基础配置）
代码默认值          （Pydantic 模型字段默认）
```

### 7.2 关键规则

- `config/app.ini` 是品种/时间框架的**唯一来源**，禁止其他文件重复定义
- 通过 `src.config` 模块访问配置，不直接解析 `.ini`
- `*.local.ini` 存放密钥（login/password/API key），已加入 .gitignore

### 7.3 信号运行时配置（`config/signal.ini`）

关键参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `auto_trade_enabled` | false | 自动下单开关 |
| `auto_trade_min_confidence` | 0.70 | 自动下单最低置信度 |
| `min_preview_confidence` | 0.55 | 触发 preview 状态的最低置信度 |
| `min_preview_stable_seconds` | 15.0 | 从 preview 晋升 armed 的稳定时间 |
| `voting_enabled` | true | 跨策略投票开关 |
| `voting_consensus_threshold` | 0.40 | 投票共识阈值 |
| `min_affinity_skip` | 0.15 | 低于此 affinity 直接跳过评估 |

---

### 7.4 Phase 3 补充

- `soft_regime_enabled = true` 时，运行时会保留 dominant regime 供兼容路径使用，同时输出 `regime_probabilities` 并按概率加权计算 affinity。
- `config/indicators.json` 中的 `delta_bars` 会让指标管理器在 confirmed 和 intrabar 快照中补充 `*_d3`、`*_d5` 这类变化率字段。
- Voting 在计票前会做同 bar 同策略融合，避免 intrabar 与 confirmed 同向结果被重复计票。
- `TradeExecutor` 的 HTF 过滤改为软惩罚：冲突乘 `0.70`，对齐乘 `1.10`，随后再比较 `auto_trade_min_confidence`。
- `ConfidenceCalibrator` 采用 staged alpha：`<50` 样本禁用校准，`50-99` 使用 `warmup_alpha=0.10`，`100+` 使用 `alpha=0.15`。

---

## 8. API 层

### 8.1 认证

所有请求需要 `X-API-Key` 头（配置在 `config/market.ini`）。

### 8.2 响应结构

所有端点返回统一包装：

```python
{
    "success": true,
    "data": {...},
    "error": null,
    "error_code": null,   # AIErrorCode 枚举值
    "metadata": {...}
}
```

### 8.3 端点概览

| 路由前缀 | 主要功能 |
|---------|---------|
| `/` | 行情（symbols, quote, ticks, ohlc, stream SSE） |
| `/trade` | 下单（trade, close, close_all, precheck, estimate_margin） |
| `/account` | 账户（info, positions, orders） |
| `/economic` | 经济日历（calendar, risk-windows, trade-guard） |
| `/indicators` | 指标（list, {symbol}/{timeframe}, compute） |
| `/monitoring` | 监控（health, startup, queues, config/effective） |
| `/signals` | 信号（查询、诊断、策略列表、投票信息、结构与质量监控） |

---

## 9. 风控层

交易请求依次经过以下关卡（任一失败则拒绝）：

```
1. SignalFilterChain（src/signals/execution/filters.py）
   ├─ SessionFilter: 交易时段过滤
   ├─ SessionTransitionFilter: 时段切换冷却
   ├─ SpreadFilter: 点差过滤
   ├─ EconomicEventFilter: 经济事件窗口
   └─ VolatilitySpikeFilter: ATR 暴增过滤

2. PreTradeRiskService（src/risk/service.py）
   规则按 fast-fail 顺序执行（廉价检查优先）：
   ├─ ① AccountSnapshotRule: 持仓数/手数/保证金
   ├─ ② DailyLossLimitRule: 日损失限制
   ├─ ③ MarginAvailabilityRule: 保证金动态检查
   ├─ ④ TradeFrequencyRule: 交易频率限制
   ├─ ⑤ ProtectionRule: SL/TP 合规性
   ├─ ⑥ SessionWindowRule: 交易时段检查
   ├─ ⑦ MarketStructureRule: 市场结构检查
   ├─ ⑧ EconomicEventRule: 经济事件窗口
   └─ ⑨ CalendarHealthRule: 日历健康检查

3. TradeGuard（src/calendar/economic_calendar/trade_guard.py）
   └─ 高风险经济事件窗口内阻止交易
```

---

## 10. 持久化层

### 10.1 StorageWriter 通道配置

| 通道 | 大小 | 刷写间隔 | 溢出策略 |
|------|------|---------|---------|
| ticks | 20,000 | 1s | drop_oldest |
| quotes | 5,000 | 1s | auto |
| intrabar | 10,000 | 5s | drop_newest |
| ohlc | 5,000 | 1s | block |
| ohlc_indicators | 5,000 | 1s | block |
| economic_calendar | 1,000 | 2s | auto |

### 10.2 Schema 位置

所有 TimescaleDB 表定义在 `src/persistence/schema/`：

- `ticks.py` — Tick 数据
- `quotes.py` — 报价数据
- `ohlc.py` — OHLC K 线
- `intrabar.py` — 盘中分钟数据
- `signals.py` — 信号记录
- `auto_executions.py` — 自动交易记录
- `economic_calendar.py` — 经济日历事件
- `trade_operations.py` — 交易操作日志
- `runtime_tasks.py` — 启动任务状态

---

## 当前补充

- `SignalFilterChain` now includes `SessionTransitionFilter`, which suppresses new evaluations during the configured London -> New York cooldown window.
- `SignalRuntime` caches confirmed market-structure context per `(symbol, timeframe)` and reuses it for intrabar events; M1 uses `m1_lookback_bars` instead of the wider default lookback.
- `TradeExecutor` enforces `max_concurrent_positions_per_symbol` before dispatch and uses timeframe-specific ATR stop/target profiles.
- `PositionManager` supports UTC end-of-day closeout and records `last_end_of_day_close_date` in status output.
- `PreTradeRiskService` includes a `daily_loss_limit` rule, and trade APIs map that failure to the `daily_loss_limit` error code.
- signal diagnostics now expose `GET /signals/monitoring/quality/{symbol}/{timeframe}` for regime + quality monitoring.
