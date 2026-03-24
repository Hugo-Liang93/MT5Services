# MT5Services 系统架构全链路流程图

> 重构后最终版本 — 2026-03-23

---

## 全局总览

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              MT5 终端 (Windows)                                │
│                    quote / tick / ohlc / trade / account                        │
└──────────────┬──────────────────────────────────────────┬──────────────────────┘
               │ 拉取行情                                  │ 下单/平仓
               ▼                                          ▲
┌──────────────────────────┐                  ┌───────────┴──────────────┐
│   数据采集模块            │                  │   交易执行模块            │
│   BackgroundIngestor     │                  │   TradingModule          │
│   src/ingestion/         │                  │   src/trading/           │
└──────┬───────┬───────────┘                  └──────────────────────────┘
       │       │                                          ▲
  写缓存    写队列                                         │ dispatch
       │       │                                          │
       ▼       ▼                                          │
┌────────────┐ ┌────────────┐     ┌───────────┐    ┌──────┴──────────┐
│MarketData  │ │StorageWriter│     │ API 路由  │    │ TradeExecutor   │
│Service     │ │(多通道队列) │     │ FastAPI   │    │ src/trading/    │
│src/market/ │ │src/persist/ │     │ src/api/  │    │signal_executor  │
└──────┬─────┘ └──────┬─────┘     └─────┬─────┘    └────────▲────────┘
       │              │                 │                    │
   收盘事件       异步落库           HTTP 请求         SignalEvent
       │              │                 │                    │
       ▼              ▼                 │                    │
┌──────────────────────────┐            │         ┌──────────┴──────────┐
│   指标计算模块            │    读缓存   │         │   信号运行时         │
│   UnifiedIndicatorManager│◄───────────┘         │   SignalRuntime     │
│   src/indicators/        │                      │   src/signals/      │
└──────────┬───────────────┘                      │   orchestration/    │
           │                                      └─────────────────────┘
      指标快照                                             ▲
           │                                               │
           └───────────────────────────────────────────────┘
                      snapshot listener 回调
```

---

## 阶段一：数据采集

```
┌──────────────────────────────────────────────────────────────────────┐
│  模块: src/ingestion/ingestor.py  —  BackgroundIngestor             │
│  职责: 从 MT5 终端拉取行情数据，写入内存缓存和持久化队列            │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  主循环 (sleep = poll_interval, 默认 0.5s)                           │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │  对每个 (symbol, timeframe):                                  │    │
│  │                                                               │    │
│  │  ① get_quote()                                                │    │
│  │     产出: Quote{bid, ask, spread, time}                       │    │
│  │     消费方: MarketDataService.set_quote()                     │    │
│  │             StorageWriter.enqueue("quotes", ...)              │    │
│  │                                                               │    │
│  │  ② copy_ticks_from()                                          │    │
│  │     产出: List[Tick{bid, ask, volume, time}]                  │    │
│  │     消费方: MarketDataService.extend_ticks()                  │    │
│  │             StorageWriter.enqueue("ticks", ...)               │    │
│  │                                                               │    │
│  │  ③ copy_rates_from_pos()   (按 ohlc_interval 节流)            │    │
│  │     产出: List[OHLC{open, high, low, close, volume, time}]   │    │
│  │     消费方: MarketDataService.set_ohlc_closed()               │    │
│  │             StorageWriter.enqueue("ohlc", ...)                │    │
│  │     ★ 触发收盘事件: enqueue_ohlc_closed_event()               │    │
│  │                                                               │    │
│  │  ④ get_ohlc(count=1)      (按 intrabar_interval 节流)         │    │
│  │     产出: 当前未收盘 bar 的 OHLC 快照                          │    │
│  │     消费方: MarketDataService.set_intrabar()                  │    │
│  │     去重: OHLC 值与上次完全相同 → 跳过                         │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                      │
│  数据产出汇总:                                                       │
│  ┌────────────┬─────────────────────┬────────────────────────────┐   │
│  │ 数据类型    │ 写入内存缓存         │ 写入持久化队列             │   │
│  ├────────────┼─────────────────────┼────────────────────────────┤   │
│  │ Quote      │ MarketDataService   │ StorageWriter → quotes     │   │
│  │ Tick       │ MarketDataService   │ StorageWriter → ticks      │   │
│  │ OHLC(收盘) │ MarketDataService   │ StorageWriter → ohlc       │   │
│  │ Intrabar   │ MarketDataService   │ (不落库)                    │   │
│  └────────────┴─────────────────────┴────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 阶段二：指标计算

```
┌──────────────────────────────────────────────────────────────────────┐
│  模块: src/indicators/  —  UnifiedIndicatorManager                   │
│  职责: 收盘/盘中事件驱动的技术指标流水线计算与快照发布               │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─── confirmed 链路 ────────────────────────────────────────────┐   │
│  │                                                                │   │
│  │  触发: MarketDataService.set_ohlc_closed()                    │   │
│  │        → enqueue_ohlc_closed_event() 写入 events.db           │   │
│  │                                                                │   │
│  │  消费: bar_event_handler.process_closed_bar_events_batch()    │   │
│  │        → pipeline_runner._load_confirmed_bars(N 根完整收盘 K 线)         │   │
│  │        → engine/pipeline.py 计算全部 21 个 enabled 指标        │   │
│  │        → result_store 规范化 + delta_bars 一阶导数计算         │   │
│  │                                                                │   │
│  │  产出: Dict[indicator_name, Dict[field, value]]               │   │
│  │        示例: {"rsi14": {"rsi": 42.5, "rsi_d3": -2.1},        │   │
│  │               "atr14": {"atr": 3.2, "atr_sma": 2.8}, ...}    │   │
│  │                                                                │   │
│  │  发布: snapshot_publisher.publish_snapshot(scope="confirmed")  │   │
│  │        → 通知所有 snapshot_listener（含 SignalRuntime）         │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌─── intrabar 链路 ─────────────────────────────────────────────┐   │
│  │                                                                │   │
│  │  触发: MarketDataService.set_intrabar()                       │   │
│  │                                                                │   │
│  │  消费: _intrabar_loop 独立线程                                 │   │
│  │        → _load_intrabar_bars(N-1 收盘 + 当前 bar)              │   │
│  │        → 仅计算 8 个 intrabar 指标 (策略声明自动推导)          │   │
│  │          rsi14, stoch_rsi14, williamsr14, cci20,              │   │
│  │          boll20, keltner20, donchian20, adx14                 │   │
│  │                                                                │   │
│  │  去重: store_preview_snapshot() 对比上次结果                   │   │
│  │        → 相同则跳过发布（策略保持上次状态）                    │   │
│  │                                                                │   │
│  │  发布: publish_snapshot(scope="intrabar")                     │   │
│  │        → 通知 SignalRuntime                                    │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  启动 warmup:                                                        │
│  _event_loop 启动后前 30s 内每 2s 重试 reconcile_all()，              │
│  一旦 ingestor 回填的 OHLC 缓存就绪即计算全部指标（含 HTF TF），     │
│  消除高 TF（H1/D1）的冷启动盲区。                                     │
│                                                                      │
│  HTF 指标缓存:                                                       │
│  _results[symbol_tf_indicator] 每个 key 仅存最新一条 IndicatorResult，│
│  含 bar_time 元数据。get_indicator() 返回值包含 _bar_time 字段，      │
│  策略/runtime 可据此判断 HTF 数据新鲜度。                              │
│                                                                      │
│  快照写入持久化:                                                     │
│  StorageWriter.enqueue("ohlc_indicators", snapshot_dict)             │
│  → TimescaleDB ohlc_indicators 表                                    │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 阶段三：信号评估（核心调度链路）

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  模块: src/signals/orchestration/runtime.py  —  SignalRuntime               │
│  职责: 事件驱动的信号运行时主循环                                           │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  输入: 指标快照事件 (symbol, timeframe, indicators, scope, metadata)         │
│                                                                              │
│  ┌─── 双队列分流 ────────────────────────────────────────────────────────┐   │
│  │  _confirmed_events (2048, 不可丢弃, 优先消费)                         │   │
│  │  _intrabar_events  (4096, 可丢弃, confirmed 为空时才消费)             │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│  ┌─── STEP 1: 快照级过滤 ───────────────────────────────────────────────┐   │
│  │  模块: src/signals/execution/filters.py  —  SignalFilterChain        │   │
│  │  消费: symbol, spread_points, utc_now, indicators                    │   │
│  │                                                                       │   │
│  │  ① SessionFilter         → 非交易时段?  → SKIP                       │   │
│  │  ② SessionTransitionFilter → 时段切换冷却期? → SKIP                   │   │
│  │  ③ SpreadFilter           → 点差过宽?   → SKIP                       │   │
│  │  ④ EconomicEventFilter    → 高影响事件窗口? → SKIP                    │   │
│  │  ⑤ VolatilitySpikeFilter  → ATR 暴增?   → SKIP                       │   │
│  │                                                                       │   │
│  │  产出: (allowed: bool, reason: str)                                   │   │
│  │  不通过 → 直接 return True (计入 processed, 跳过后续全部)             │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                              │ 通过                                          │
│                              ▼                                               │
│  ┌─── STEP 2: Regime 检测 ──────────────────────────────────────────────┐   │
│  │  模块: src/signals/evaluation/regime.py  —  MarketRegimeDetector     │   │
│  │  消费: indicators (adx14, boll20, keltner20)                         │   │
│  │                                                                       │   │
│  │  硬分类: detect(indicators) → RegimeType                              │   │
│  │    ┌─ KC-BB Squeeze (BB完全在KC内)    → BREAKOUT                      │   │
│  │    ├─ ADX ≥ 23                        → TRENDING                      │   │
│  │    ├─ ADX < 18 且 BB宽度 < 0.8%       → BREAKOUT                     │   │
│  │    ├─ ADX < 18                        → RANGING                       │   │
│  │    ├─ 18 ≤ ADX < 23                   → UNCERTAIN                    │   │
│  │    └─ 无数据                           → UNCERTAIN                    │   │
│  │                                                                       │   │
│  │  概率化: detect_soft(indicators) → SoftRegimeResult                   │   │
│  │    产出: {TRENDING: 0.35, RANGING: 0.10, BREAKOUT: 0.45, ...}        │   │
│  │                                                                       │   │
│  │  写入: regime_metadata["_regime"], ["_soft_regime"]                   │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│  ┌─── STEP 3: 快速全拒绝检查 ──────────────────────────────────────────┐   │
│  │  方法: _any_strategy_eligible()                                       │   │
│  │  消费: regime, 所有策略的 regime_affinity, min_affinity_skip          │   │
│  │                                                                       │   │
│  │  逻辑: 遍历当前 (symbol, tf) 的所有策略，                             │   │
│  │        检查 scope/session/timeframe/affinity 过滤条件。               │   │
│  │        若 NO 策略能通过 → 跳过后续所有计算 (市场结构、策略循环、投票) │   │
│  │                                                                       │   │
│  │  效果: 极端 trending 时均值回归策略全部 affinity < 0.15 → early exit  │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                              │ 至少一个策略 eligible                         │
│                              ▼                                               │
│  ┌─── STEP 4: 策略评估循环  _evaluate_strategies() ────────────────────┐   │
│  │                                                                       │   │
│  │  for strategy in strategies:                                          │   │
│  │    ├─ 4a. session/timeframe/scope 过滤                                │   │
│  │    ├─ 4b. required_indicators 缺失检查                                │   │
│  │    ├─ 4c. affinity gate (min_affinity_skip)                           │   │
│  │    │       消费: strategy.regime_affinity + regime                     │   │
│  │    │       产出: effective_affinity (soft regime 时加权平均)           │   │
│  │    ├─ 4d. 快照去重 (is_new_snapshot)                        │   │
│  │    │                                                                  │   │
│  │    ├─ 4e. 市场结构分析 (延迟按需, 首次触发时计算一次)                 │   │
│  │    │  ┌─────────────────────────────────────────────────────────┐     │   │
│  │    │  │ 模块: src/market_structure/analyzer.py                  │     │   │
│  │    │  │ 方法: analyze_cached(scope=...)                         │     │   │
│  │    │  │ 消费: MarketDataService.get_ohlc_closed(N 根 K 线)     │     │   │
│  │    │  │ 产出: MarketStructureContext                            │     │   │
│  │    │  │   {breakout_state, reclaim_state, compression_state,   │     │   │
│  │    │  │    sweep_confirmation_state, structure_bias,            │     │   │
│  │    │  │    previous_day_high/low, asia_range, ...}             │     │   │
│  │    │  │ 缓存: confirmed → 更新缓存, intrabar → 复用缓存       │     │   │
│  │    │  └─────────────────────────────────────────────────────────┘     │   │
│  │    │                                                                  │   │
│  │    ├─ 4f. HTF 指标注入                                                │   │
│  │    │       消费: signal.ini [strategy_htf] 配置 + IndicatorManager   │   │
│  │    │       产出: htf_payload = {"H1": {"adx14": {...}, ...}}          │   │
│  │    │                                                                  │   │
│  │    ├─ 4g. 策略评估                                                    │   │
│  │    │  ┌─────────────────────────────────────────────────────────┐     │   │
│  │    │  │ 模块: src/signals/service.py  —  SignalModule.evaluate()│     │   │
│  │    │  │ 消费: SignalContext{indicators, metadata, htf_indicators}│    │   │
│  │    │  │                                                         │     │   │
│  │    │  │ 内部置信度管线:                                          │     │   │
│  │    │  │   raw_confidence (策略规则)                              │     │   │
│  │    │  │     × effective_affinity (regime 亲和度)                 │     │   │
│  │    │  │     × session_performance_multiplier (日内绩效)          │     │   │
│  │    │  │     → ConfidenceCalibrator (长期统计校准)                │     │   │
│  │    │  │     = final_confidence                                  │     │   │
│  │    │  │                                                         │     │   │
│  │    │  │ 产出: SignalDecision{direction, confidence, reason, ...}   │     │   │
│  │    │  └─────────────────────────────────────────────────────────┘     │   │
│  │    │                                                                  │   │
│  │    ├─ 4h. Intrabar 置信度衰减 (scope=intrabar 时 × decay)            │   │
│  │    │                                                                  │   │
│  │    ├─ 4i. HTF 方向对齐修正                                            │   │
│  │    │       消费: htf_context_fn(symbol, tf) → HTFStateCache           │   │
│  │    │       逻辑: 对齐 → base=1.10, 冲突 → base=0.70                  │   │
│  │    │              × strength (HTF confidence 偏离 0.5)                │   │
│  │    │              × stability (方向连续 bar 数加成)                   │   │
│  │    │              × intrabar_ratio (scope=intrabar 时半强度)          │   │
│  │    │       参数: signal.ini [htf_alignment] 配置化                    │   │
│  │    │       写入: regime_metadata[htf_direction, htf_alignment]        │   │
│  │    │       ★ 修正后的 confidence 被持久化（信号记录=执行值）           │   │
│  │    │                                                                  │   │
│  │    ├─ 4j. 状态机转换                                                  │   │
│  │    │       confirmed: idle → confirmed_buy/sell/cancelled             │   │
│  │    │       intrabar:  idle → preview → armed (稳定≥15s)               │   │
│  │    │       产出: transition_metadata{signal_state, ...}               │   │
│  │    │       无转换 → continue (不持久化/不发布)                         │   │
│  │    │                                                                  │   │
│  │    ├─ 4k. 持久化                                                      │   │
│  │    │       模块: src/signals/tracking/repository.py                   │   │
│  │    │       写入: TimescaleDB signal_records 表                        │   │
│  │    │       产出: SignalRecord{signal_id, ...}                         │   │
│  │    │                                                                  │   │
│  │    └─ 4l. 发布 SignalEvent                                            │   │
│  │            产出: SignalEvent{symbol, tf, strategy, direction,            │   │
│  │                    confidence, signal_state, scope, indicators, ...}  │   │
│  │            广播给所有 signal_listener                                  │   │
│  │                                                                       │   │
│  │  循环结果收集: snapshot_decisions: List[SignalDecision]                │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│  ┌─── STEP 5: 跨策略表决 ──────────────────────────────────────────────┐   │
│  │  模块: src/signals/orchestration/voting.py  —  StrategyVotingEngine  │   │
│  │  消费: snapshot_decisions (步骤 4 的全部决策)                         │   │
│  │                                                                       │   │
│  │  Signal Fusion 去重:                                                  │   │
│  │    同 bar 内 intrabar+confirmed 同策略 → 保留 confirmed              │   │
│  │                                                                       │   │
│  │  单 consensus 模式:                                                   │   │
│  │    buy_score = Σconf(buy) / Σconf(all)                                │   │
│  │    sell_score = Σconf(sell) / Σconf(all)                               │   │
│  │    → 达到 threshold(0.40) → 产出 strategy="consensus" 信号           │   │
│  │    → disagreement_factor 惩罚多空分歧                                 │   │
│  │                                                                       │   │
│  │  多组 voting 模式:                                                    │   │
│  │    每个 VotingGroupConfig → 独立 engine → 产出 strategy=group.name   │   │
│  │                                                                       │   │
│  │  产出: vote 信号 → 状态机转换 → 持久化 → 发布 SignalEvent            │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 阶段四：信号广播与消费

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  SignalRuntime._publish_signal_event(event: SignalEvent)                     │
│  广播给 3 个独立 listener，每个 listener 独立消费、互不干扰                 │
├─────────────────────────────┬────────────────────────┬───────────────────────┤
│                             │                        │                       │
│  Listener 1                 │  Listener 2            │  Listener 3           │
│                             │                        │                       │
▼                             ▼                        ▼                       │
┌─────────────────┐  ┌──────────────────┐  ┌───────────────────┐              │
│ TradeExecutor   │  │SignalQuality     │  │ HTFStateCache     │              │
│                 │  │Tracker           │  │                   │              │
│ 消费:           │  │                  │  │ 消费:             │              │
│  scope=confirmed│  │ 消费:            │  │  confirmed_buy/   │              │
│  confirmed_buy/ │  │  scope=confirmed │  │  sell/cancelled   │              │
│  sell 信号      │  │  所有策略事件    │  │                   │              │
│                 │  │                  │  │ 产出:             │              │
│ (详见阶段五)    │  │ 产出:            │  │  缓存 HTF 方向    │              │
│                 │  │  N bars 后评估   │  │  (symbol, htf_tf) │              │
│                 │  │  → signal_       │  │  → ("buy"/"sell", │              │
│                 │  │    outcomes 表   │  │     updated_at)   │              │
│                 │  │  → Calibrator    │  │                   │              │
│                 │  │  → Performance   │  │ 被消费方:         │              │
│                 │  │    Tracker       │  │  SignalRuntime     │              │
│                 │  │                  │  │  (HTF方向对齐修正) │              │
└─────────────────┘  └──────────────────┘  └───────────────────┘              │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 阶段五：交易执行全链路

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  TradeExecutor.on_signal_event(event: SignalEvent)                          │
│  模块: src/trading/signal_executor.py                                       │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  输入: SignalEvent{symbol, strategy, direction, confidence, signal_state, ...} │
│                                                                              │
│  ┌─── GATE 1: 基础过滤 ─────────────────────────────────────────────────┐   │
│  │  scope != "confirmed"  → return                                       │   │
│  │  signal_state 不含 "confirmed" → return                               │   │
│  │  config.enabled == False → return                                     │   │
│  │  direction not in ("buy","sell") → return                                │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│  ┌─── GATE 2: 熔断器 ───────────────────────────────────────────────────┐   │
│  │  连续失败 ≥ max_consecutive_failures → circuit_open                   │   │
│  │  自动恢复: 超过 circuit_auto_reset_minutes → half-open 重试           │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│  ┌─── GATE 3: 策略域准入  ExecutionGate ────────────────────────────────┐   │
│  │  模块: src/trading/execution_gate.py                                  │   │
│  │  消费: event.strategy, event.metadata                                 │   │
│  │                                                                       │   │
│  │  ① Voting Group 保护:                                                 │   │
│  │     属于 voting group 的策略不能单独触发交易                           │   │
│  │     (standalone_override 白名单可豁免)                                │   │
│  │                                                                       │   │
│  │  ② Trade Trigger 白名单:                                              │   │
│  │     非空时仅白名单内策略可触发 (如 ["consensus"])                     │   │
│  │                                                                       │   │
│  │  ③ require_armed 门控:                                                │   │
│  │     检查 previous_state / preview_state_at_close 是否含 "armed"      │   │
│  │                                                                       │   │
│  │  产出: (allowed: bool, reason: str)                                   │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│  ┌─── GATE 4: 置信度门槛 ──────────────────────────────────────────────┐   │
│  │  confidence < min_confidence (默认 0.7) → skip                        │   │
│  │  ★ 此处 confidence 已包含 HTF 修正 (在 SignalRuntime 中完成)          │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│  ┌─── GATE 5: 持仓数量限制 ────────────────────────────────────────────┐   │
│  │  _reached_position_limit(symbol)                                      │   │
│  │  消费: PositionManager.active_positions() + TradingModule.positions() │   │
│  │  检查: max(tracked_count, live_count) ≥ limit → skip                 │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│  ┌─── 交易参数计算 ─────────────────────────────────────────────────────┐   │
│  │  模块: src/trading/sizing.py  —  compute_trade_params()               │   │
│  │  消费: ATR, account_balance, close_price, timeframe                   │   │
│  │                                                                       │   │
│  │  产出: TradeParameters                                                │   │
│  │    ├─ entry_price                                                     │   │
│  │    ├─ stop_loss   (ATR × SL倍数, 按 TF 差异化: M1=1.0..D1=2.0)     │   │
│  │    ├─ take_profit (ATR × TP倍数, 按 TF 差异化: M1=2.0..D1=4.0)     │   │
│  │    ├─ position_size (基于 risk_percent + balance + contract_size)     │   │
│  │    └─ risk_reward_ratio                                               │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│  ┌─── GATE 6: 成本检查 ────────────────────────────────────────────────┐   │
│  │  _estimate_cost_metrics()                                             │   │
│  │  消费: spread_points, stop_distance, symbol_point                     │   │
│  │  检查: spread_to_stop_ratio > max (默认 0.33) → skip                 │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│  ┌─── 执行交易 _execute() ──────────────────────────────────────────────┐   │
│  │                                                                       │   │
│  │  调用: TradingModule.dispatch_operation("trade", payload)             │   │
│  │                                                                       │   │
│  │  ┌─── TradingModule 内部 ─────────────────────────────────────────┐  │   │
│  │  │  模块: src/trading/service.py                                   │  │   │
│  │  │                                                                 │  │   │
│  │  │  ① 幂等性保护 (request_id 去重)                                 │  │   │
│  │  │  ② auto_entry_enabled / close_only_mode 检查                   │  │   │
│  │  │  ③ PreTradeRiskService.precheck()                              │  │   │
│  │  │     ┌───────────────────────────────────────────────────┐      │  │   │
│  │  │     │ 模块: src/risk/service.py                         │      │  │   │
│  │  │     │                                                   │      │  │   │
│  │  │     │ ① AccountSnapshotRule: 持仓数/手数/保证金          │      │  │   │
│  │  │     │ ② DailyLossLimitRule: 日损失限制 (risk.ini)       │      │  │   │
│  │  │     │ ③ MarginAvailabilityRule: 保证金动态检查          │      │  │   │
│  │  │     │ ④ TradeFrequencyRule: 交易频率限制                │      │  │   │
│  │  │     │ ⑤ ProtectionRule: SL/TP 合规性                    │      │  │   │
│  │  │     │ ⑥ SessionWindowRule: 交易时段检查                  │      │  │   │
│  │  │     │ ⑦ MarketStructureRule: 市场结构检查               │      │  │   │
│  │  │     │ ⑧ EconomicEventRule: 经济事件窗口                  │      │  │   │
│  │  │     │ ⑨ CalendarHealthRule: 日历健康检查                 │      │  │   │
│  │  │     │ (fast-fail 顺序: 廉价检查优先, 外部查询靠后)      │      │  │   │
│  │  │     │ 任一 rule 拒绝 → PreTradeRiskBlockedError         │      │  │   │
│  │  │     └───────────────────────────────────────────────────┘      │  │   │
│  │  │  ④ TradingService.execute_trade()                              │  │   │
│  │  │     → MT5 order_send() → 成交结果                              │  │   │
│  │  │  ⑤ 操作审计 (TradeOperationRecord → DB)                       │  │   │
│  │  └─────────────────────────────────────────────────────────────────┘  │   │
│  │                                                                       │   │
│  │  成功后:                                                              │   │
│  │    ├─ PositionManager.track_position(ticket, ...)  → 追踪仓位        │   │
│  │    ├─ TradeOutcomeTracker.on_trade_opened(...)      → 登记活跃交易   │   │
│  │    ├─ _persist_execution_fn([log])                  → auto_executions│   │
│  │    └─ 重置熔断器失败计数                                              │   │
│  │                                                                       │   │
│  │  风控拒绝: PreTradeRiskBlockedError → 记录 risk_blocks, skip          │   │
│  │  异常失败: consecutive_failures++ → 达阈值 → circuit_open             │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 阶段六：仓位管理与结果追踪

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  模块: src/trading/position_manager.py  —  PositionManager                  │
│  职责: 追踪由 TradeExecutor 开仓的仓位，移动止损/保本，日终平仓             │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  输入: track_position(ticket, signal_id, symbol, direction, params, ...)       │
│                                                                              │
│  后台对账循环 (reconcile_interval = 10s):                                   │
│  ┌───────────────────────────────────────────────────────────────────────┐   │
│  │  ① 查询 MT5 活跃仓位列表                                              │   │
│  │  ② 对比 tracked positions:                                            │   │
│  │     - MT5 有、tracker 无 → 尝试恢复 (resolve_position_context)        │   │
│  │     - tracker 有、MT5 无 → 仓位已关闭                                 │   │
│  │  ③ 仓位已关闭:                                                        │   │
│  │     → close_callback → TradeOutcomeTracker.on_position_closed()       │   │
│  │  ④ 仓位仍开:                                                          │   │
│  │     → 移动止损 (trailing_atr_multiplier × ATR)                        │   │
│  │     → 保本止损 (价格移动 > breakeven_atr_threshold × ATR)             │   │
│  │  ⑤ 日终检查 (UTC 21:00):                                              │   │
│  │     → end_of_day_close_enabled → 自动平仓所有 tracked positions       │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  产出:                                                                       │
│  ┌────────────────┬────────────────────────────────────────────────────┐     │
│  │ 事件            │ 消费方                                             │     │
│  ├────────────────┼────────────────────────────────────────────────────┤     │
│  │ 仓位关闭       │ TradeOutcomeTracker → trade_outcomes 表            │     │
│  │                │                      → PerformanceTracker(trade)   │     │
│  │ 止损修改       │ TradingService → MT5 order_modify()               │     │
│  │ 日终平仓       │ TradingService → MT5 order_send(close)            │     │
│  └────────────────┴────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│  绩效反馈双链路                                                              │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  链路 A: 信号质量 (SignalQualityTracker)                                    │
│  ┌───────────────────────────────────────────────────────────────────────┐   │
│  │  输入: scope=confirmed 的 SignalEvent                                 │   │
│  │  逻辑: 每收到 confirmed event → 推进同 (sym,tf) 所有 pending 的       │   │
│  │        bars_elapsed; 达到 bars_to_evaluate(5) → 用最新 close 评估     │   │
│  │  产出:                                                                │   │
│  │    → signal_outcomes 表 (DB)                                          │   │
│  │    → StrategyPerformanceTracker.record_outcome(source="signal")       │   │
│  │    → ConfidenceCalibrator 后台线程每 15 分钟查询 DB 聚合胜率          │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  链路 B: 交易结果 (TradeOutcomeTracker)                                     │
│  ┌───────────────────────────────────────────────────────────────────────┐   │
│  │  输入: TradeExecutor.on_trade_opened() + PositionManager.on_closed() │   │
│  │  逻辑: 用实际成交价 vs 平仓价计算盈亏                                 │   │
│  │  产出:                                                                │   │
│  │    → trade_outcomes 表 (DB)                                           │   │
│  │    → StrategyPerformanceTracker.record_outcome(source="trade")        │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  ┌─── StrategyPerformanceTracker ────────────────────────────────────────┐   │
│  │  模块: src/signals/evaluation/performance.py                          │   │
│  │  消费: signal/trade 两链路的 outcome 回调                             │   │
│  │  状态: 纯内存, per-strategy 滚动统计                                  │   │
│  │        (wins, losses, streak, PnL, per-regime 维度)                   │   │
│  │  产出: get_multiplier(strategy, regime) → [0.50, 1.20]               │   │
│  │        → 被 SignalModule.evaluate() 置信度管线消费                    │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  ┌─── ConfidenceCalibrator ──────────────────────────────────────────────┐   │
│  │  模块: src/signals/evaluation/calibrator.py                           │   │
│  │  消费: DB signal_outcomes 表 (后台线程每 15 分钟查询)                 │   │
│  │  逻辑: 按 (strategy, direction, regime) 聚合近 8 小时胜率                │   │
│  │        分阶段校准: <50笔 不校准, 50-100 轻微, 100+ 正常              │   │
│  │  产出: calibrate(strategy, direction, confidence) → 校准后 confidence    │   │
│  │        → 被 SignalModule.evaluate() 置信度管线消费                    │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 模块依赖关系总览

```
                        ┌─────────────────┐
                        │   config/*.ini  │
                        │   配置加载模块   │
                        └────────┬────────┘
                                 │ 读取配置
          ┌──────────────────────┼──────────────────────┐
          ▼                      ▼                      ▼
  ┌───────────────┐    ┌─────────────────┐    ┌─────────────────┐
  │ BackgroundIn- │    │  MarketData     │    │  StorageWriter  │
  │ gestor        │───▶│  Service        │    │  (多通道队列)    │
  │               │    │  (内存缓存)     │    │                 │
  └───────┬───────┘    └────────┬────────┘    └────────┬────────┘
          │                     │ get_ohlc_closed       │ 异步落库
          │                     ▼                       ▼
          │            ┌─────────────────┐      ┌─────────────┐
          │            │ UnifiedIndicator│      │ TimescaleDB │
          │            │ Manager         │      │             │
          │            └────────┬────────┘      └─────────────┘
          │                     │ snapshot                  ▲
          │                     ▼                           │ 查询
          │  ┌──────────────────────────────────────────┐   │
          │  │          SignalRuntime                    │   │
          │  │  ┌──────────────┐  ┌──────────────────┐ │   │
          │  │  │SignalFilter  │  │MarketStructure   │ │   │
          │  │  │Chain (5 个)  │  │Analyzer (缓存)   │ │   │
          │  │  └──────────────┘  └──────────────────┘ │   │
          │  │  ┌──────────────┐  ┌──────────────────┐ │   │
          │  │  │MarketRegime  │  │SignalModule      │ │   │
          │  │  │Detector      │  │(策略+置信度管线) │ │   │
          │  │  └──────────────┘  └──────────────────┘ │   │
          │  │  ┌──────────────┐  ┌──────────────────┐ │   │
          │  │  │VotingEngine  │  │HTFStateCache     │ │   │
          │  │  │(表决)        │  │(方向缓存)        │ │   │
          │  │  └──────────────┘  └──────────────────┘ │   │
          │  └─────────────┬────────────────────────────┘   │
          │                │ SignalEvent                     │
          │    ┌───────────┼────────────┬───────────┐       │
          │    ▼           ▼            ▼           │       │
          │  ┌──────┐  ┌────────┐  ┌────────┐      │       │
          │  │Trade │  │Signal  │  │HTFState│      │       │
          │  │Execu-│  │Quality │  │Cache   │      │       │
          │  │tor   │  │Tracker │  │        │      │       │
          │  └──┬───┘  └───┬────┘  └────────┘      │       │
          │     │          │                        │       │
          │     ▼          ▼                        │       │
          │  ┌──────────────────────┐               │       │
          │  │   ExecutionGate     │               │       │
          │  │   (策略准入检查)     │               │       │
          │  └──────────┬──────────┘               │       │
          │             ▼                           │       │
          │  ┌──────────────────────┐               │       │
          │  │   TradingModule     │               │       │
          │  │   ┌──────────────┐  │               │       │
          │  │   │PreTradeRisk  │  │               │       │
          │  │   │Service       │  │               │       │
          │  │   └──────────────┘  │               │       │
          │  └──────────┬──────────┘               │       │
          │             │ MT5 order                 │       │
          │             ▼                           │       │
          │  ┌──────────────────────┐               │       │
          │  │  PositionManager    │───────────────┘       │
          │  │  ┌──────────────┐   │                       │
          │  │  │TradeOutcome  │───┼───────────────────────┘
          │  │  │Tracker       │   │  write trade_outcomes
          │  │  └──────────────┘   │
          │  └─────────────────────┘
          │
          └────────────────────────────────▶ MT5 终端
```

---

## 数据流转矩阵

| 数据对象 | 生产方 | 消费方 | 存储位置 |
|---------|--------|--------|---------|
| Quote | Ingestor → MT5 | MarketDataService (缓存) | TimescaleDB quotes |
| Tick | Ingestor → MT5 | MarketDataService (缓存) | TimescaleDB ticks |
| OHLC (收盘) | Ingestor → MT5 | MarketDataService + IndicatorManager | TimescaleDB ohlc |
| Intrabar OHLC | Ingestor → MT5 | MarketDataService + IndicatorManager | 仅内存 |
| 指标快照 | IndicatorManager Pipeline | SignalRuntime (listener) | TimescaleDB ohlc_indicators |
| RegimeType | MarketRegimeDetector | SignalRuntime → 策略 affinity 过滤 | regime_metadata (不单独落库) |
| MarketStructureContext | MarketStructureAnalyzer | 策略 evaluate context.metadata | 内存缓存 (scope-aware) |
| SignalDecision | SignalModule.evaluate() | SignalRuntime → 状态机 → 持久化 | TimescaleDB signal_records |
| SignalEvent | SignalRuntime 状态机转换 | TradeExecutor / QualityTracker / HTFCache | 广播 (不单独落库) |
| HTF 方向缓存 | HTFStateCache (listener) | SignalRuntime (HTF 对齐修正) | 内存 {(sym,tf): (dir,time)} |
| TradeParameters | sizing.compute_trade_params() | TradeExecutor → TradingModule | payload 的一部分 |
| TradeOperationRecord | TradingModule | 审计查询 / 仓位恢复 | TimescaleDB trade_operations |
| TrackedPosition | PositionManager | 移动止损 / 对账 / 关仓回调 | 内存 (可从 MT5+审计恢复) |
| signal_outcomes | SignalQualityTracker | ConfidenceCalibrator (后台聚合) | TimescaleDB signal_outcomes |
| trade_outcomes | TradeOutcomeTracker | PerformanceTracker (实时反馈) | TimescaleDB trade_outcomes |
| PerformanceMultiplier | PerformanceTracker | SignalModule 置信度管线 | 纯内存 (session 级重置) |
| CalibrationFactor | ConfidenceCalibrator | SignalModule 置信度管线 | 内存 (每 15 分钟刷新) |

---

## 置信度管线完整路径

```
  strategy.evaluate(context)
         │
         ▼ raw_confidence (策略规则输出, 0.0~1.0)
         │
    ×  effective_affinity
         │  硬分类: affinity[current_regime]
         │  软分类: Σ(prob[r] × affinity[r])
         ▼ post_affinity_confidence
         │
    ×  session_performance_multiplier
         │  来源: StrategyPerformanceTracker.get_multiplier()
         │  因素: session_win_rate × streak × profit_factor
         ▼
    →  ConfidenceCalibrator.calibrate()
         │  来源: DB 近 8 小时 signal_outcomes 聚合胜率
         │  阶段: <50笔(不校准) / 50-100(α=0.10) / 100+(α=0.15)
         ▼
    max(confidence_floor, result)
         ▼ final_confidence_from_evaluate
         │
    ×  intrabar_confidence_factor (scope=intrabar 时, 策略级覆盖 > 全局 0.85)
         ▼
    ×  htf_alignment_multiplier
         │  base × strength × stability  (signal.ini [htf_alignment] 配置)
         │  对齐: base=1.10  |  冲突: base=0.70  |  无数据: × 1.0
         │  intrabar 时半强度: mul = 1.0 + (raw-1.0) × ratio
         ▼
    min(1.0, result)
         ▼ ═══════════════════════════════════════════════
           最终 confidence — 持久化值 = SignalEvent.confidence
           TradeExecutor 直接使用此值与 min_confidence 比较
```

---

## 模块间数据生产-消费链路总图

> 节点 = 模块，边 = 数据流向，标注数据类型与传递方式。

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              MT5 终端                                      │
└──┬──────────────────────────────────────────────────────────────────┬──────┘
   │ ❶ Quote/Tick/OHLC/Intrabar (MT5 API 拉取)                      │
   ▼                                                                 │
┌──────────────────────┐                                             │
│  BackgroundIngestor   │                                             │
│  (数据采集 — 唯一的   │                                             │
│   MT5 数据生产者)     │                                             │
└──┬──────┬─────┬──────┘                                             │
   │      │     │                                                    │
   │      │     │ ❷ Queue rows                                       │
   │      │     ▼                                                    │
   │      │  ┌───────────────────┐     ┌───────────────────┐         │
   │      │  │  StorageWriter    │────▶│  TimescaleDB      │         │
   │      │  │  (异步多通道队列)  │     │  quotes/ticks/    │         │
   │      │  │                   │     │  ohlc/indicators/  │         │
   │      │  └───────────────────┘     │  signal_records/   │         │
   │      │                            │  signal_outcomes/  │         │
   │      │                            │  trade_outcomes/   │         │
   │      │                            │  trade_operations  │         │
   │      │                            └──────▲────────────┘         │
   │      │                                   │ DB write             │
   │      │ ❸ set_ohlc_closed / set_intrabar  │                      │
   │      ▼                                   │                      │
   │   ┌──────────────────────────┐           │                      │
   │   │  MarketDataService       │           │                      │
   │   │  (内存缓存 — 运行时数据  │           │                      │
   │   │   唯一权威)              │           │                      │
   │   │                          │           │                      │
   │   │  缓存: _ohlc_closed_cache│           │                      │
   │   │        _intrabar_cache   │           │                      │
   │   │        _tick_cache       │           │                      │
   │   │        _quote_cache      │           │                      │
   │   └──┬───────────┬──────────┘           │                      │
   │      │           │                      │                      │
   │      │ ❹ 收盘事件 │ ❺ intrabar listener │                      │
   │      ▼           ▼                      │                      │
   │   ┌──────────────────────────────────┐  │                      │
   │   │  UnifiedIndicatorManager         │  │                      │
   │   │  (指标编排器)                     │  │                      │
   │   │                                  │  │                      │
   │   │  confirmed 链路:                  │  │                      │
   │   │    events.db → pipeline → 21指标  │  │                      │
   │   │    → result_store(_results)       │  │                      │
   │   │    → publish(scope="confirmed")   │  │                      │
   │   │                                  │  │                      │
   │   │  intrabar 链路:                   │  │                      │
   │   │    queue → pipeline → 8指标       │  │                      │
   │   │    → dedup → publish("intrabar") │  │                      │
   │   │                                  │  │                      │
   │   │  warmup: 启动30s内每2s reconcile  │  │                      │
   │   │  _results 含 _bar_time 元数据     │  │                      │
   │   └──┬──────────────┬────────────────┘  │                      │
   │      │              │                   │                      │
   │      │ ❻ snapshot   │ ❼ get_indicator   │                      │
   │      │  listener    │  (HTF 按需查询)   │                      │
   │      ▼              │                   │                      │
   │   ┌──────────────────────────────────┐  │                      │
   │   │  SignalRuntime                    │  │                      │
   │   │  (信号主循环 — 核心调度器)         │  │                      │
   │   │                                  │  │                      │
   │   │  STEP 1: SignalFilterChain       │  │                      │
   │   │    Session/Spread/Economic/      │  │                      │
   │   │    Volatility 过滤               │  │                      │
   │   │  STEP 2: Regime 检测 (一次)      │  │                      │
   │   │  STEP 3: 快速全拒绝              │  │                      │
   │   │  STEP 4: 策略循环                │  │                      │
   │   │    4a-4d: 六重门控过滤            │  │                      │
   │   │    4e: 市场结构分析 (延迟按需)    │  │                      │
   │   │    4f: HTF 指标注入◄──────────────┘  │                      │
   │   │         staleness检查(2×TF周期)      │                      │
   │   │    4g: strategy.evaluate()           │                      │
   │   │    4h: intrabar衰减(策略级>全局)     │                      │
   │   │    4i: HTF方向对齐(多维加权)         │                      │
   │   │    4j-4l: 状态机→持久化→发布         │                      │
   │   │  STEP 5: VotingEngine                │                      │
   │   │                                      │                      │
   │   └──┬──────────┬──────────┬─────────────┘                      │
   │      │          │          │                                     │
   │      │ ❽        │ ❾        │ ❿                                   │
   │      │ Signal   │ Signal   │ Signal                              │
   │      │ Event    │ Event    │ Event                               │
   │      ▼          ▼          ▼                                     │
   │  ┌────────┐ ┌────────┐ ┌────────────┐                           │
   │  │Trade   │ │Signal  │ │HTFState    │                           │
   │  │Executor│ │Quality │ │Cache       │                           │
   │  │        │ │Tracker │ │            │                           │
   │  │入队列  │ │        │ │缓存方向+   │                           │
   │  │backpre-│ │N bars  │ │confidence  │                           │
   │  │ssure   │ │后评估  │ │+stable_bars│                           │
   │  └──┬─────┘ └──┬─────┘ └────────────┘                           │
   │     │          │                                                │
   │     │ ⓫        │ ⓬ signal_outcomes                              │
   │     ▼          ▼                                                │
   │  ┌────────────────────────┐  ┌─────────────────────┐            │
   │  │ ExecutionGate          │  │ PerformanceTracker   │            │
   │  │ (group/whitelist/armed)│  │ (日内绩效→乘数)     │            │
   │  └──┬─────────────────────┘  └──────────▲──────────┘            │
   │     │                                   │                       │
   │     │ ⓭ confidence/sizing               │ ⓮ trade_outcomes     │
   │     ▼                                   │                       │
   │  ┌────────────────────────┐             │                       │
   │  │ TradingModule          │             │                       │
   │  │ ┌────────────────────┐ │             │                       │
   │  │ │PreTradeRiskService │ │             │                       │
   │  │ │ 9 条规则 fast-fail │ │             │                       │
   │  │ └────────────────────┘ │             │                       │
   │  │ TradingService→MT5 ⓯  │             │                       │
   │  └──┬─────────────────────┘             │                       │
   │     │                                   │                       │
   │     │ ⓰ track_position                  │                       │
   │     ▼                                   │                       │
   │  ┌────────────────────────┐             │                       │
   │  │ PositionManager        │             │                       │
   │  │ 移动止损/保本/日终平仓  ├─────────────┘                       │
   │  │ on_close→TradeOutcome  │   trade_outcomes                    │
   │  │ Tracker                │                                     │
   │  └────────────────────────┘                                     │
   │                                                                 │
   │  ⓯ MT5 order_send ─────────────────────────────────────────────┘
   └─────────────────────────────────────────────────────────────────┘
```

### 数据流边标注索引

| 编号 | 数据 | 生产方 | 消费方 | 传递方式 |
|:---:|------|--------|--------|---------|
| ❶ | Quote/Tick/OHLC/Intrabar | MT5 终端 | BackgroundIngestor | MT5 API 拉取 |
| ❷ | 持久化行 | BackgroundIngestor | StorageWriter | 异步队列 enqueue |
| ❸ | OHLC/Intrabar 缓存更新 | BackgroundIngestor | MarketDataService | 同步方法调用 |
| ❹ | 收盘事件 (symbol, tf, bar_time) | MarketDataService | IndicatorManager | 事件回调 → SQLite events.db |
| ❺ | Intrabar bar 更新 | MarketDataService | IndicatorManager | 同步 listener → 内部 queue |
| ❻ | 指标快照 (scope, indicators) | IndicatorManager | SignalRuntime | snapshot_listener 回调 |
| ❼ | HTF 指标查询 (含 _bar_time) | IndicatorManager._results | SignalRuntime | 同步 get_indicator() + staleness 检查 |
| ❽ | SignalEvent (confirmed) | SignalRuntime | TradeExecutor | signal_listener 回调 → 内部 queue (backpressure) |
| ❾ | SignalEvent (confirmed) | SignalRuntime | SignalQualityTracker | signal_listener 回调 |
| ❿ | SignalEvent (confirmed) | SignalRuntime | HTFStateCache | signal_listener 回调 |
| ⓫ | 准入检查 | TradeExecutor | ExecutionGate | 同步方法调用 |
| ⓬ | signal_outcomes 行 | SignalQualityTracker | DB + PerformanceTracker | 回调 + DB write |
| ⓭ | TradeParameters | TradeExecutor | TradingModule | 同步方法调用 |
| ⓮ | trade_outcomes 行 | TradeOutcomeTracker | DB + PerformanceTracker | 回调 + DB write |
| ⓯ | MT5 order_send | TradingModule | MT5 终端 | MT5 API |
| ⓰ | track_position | TradeExecutor | PositionManager | 同步方法调用 |

### 关键节点场景说明

| 节点 | 场景 | 行为 |
|------|------|------|
| **Ingestor→MDS** | MT5 连接冻结 | 超时 10s 后跳过，指数退避冷却（ingest.ini [error_recovery] 可配） |
| **Ingestor→MDS** | 同一 bar 多次 intrabar 更新 | OHLC 值相同 → 跳过 set_intrabar()；不同 → 追加到缓存（API 用） |
| **MDS→IndicatorManager** | bar 收盘事件 | 写入 events.db 持久化 → _event_loop claim → 全量指标计算 |
| **MDS→IndicatorManager** | intrabar 更新 | listener 同步触发（>100ms 记 warning） → 内部 queue → 8 指标 |
| **IndicatorManager warmup** | 服务启动 | 前 30s 每 2s 重试 reconcile，等 ingestor 回填 OHLC 后立即计算 |
| **IndicatorManager→Runtime** | HTF 指标查询 | get_indicator() 返回 _bar_time；staleness > 2×TF 周期 → 跳过注入 |
| **Runtime 过滤** | 所有策略 affinity 不足 | 快速全拒绝，跳过市场结构/策略循环/投票（CPU 节省） |
| **Runtime→TradeExecutor** | 队列满 | 1s backpressure 重试；仍满则丢弃 + 计数器 |
| **TradeExecutor 熔断** | 连续失败 ≥ 3 | 熔断开路；超过 30min 自动半开重试 |
| **RiskService 规则** | 各规则拒绝 | 返回独立错误码（MARGIN_INSUFFICIENT_PRE / TRADE_FREQUENCY_LIMITED 等） |
| **PositionManager** | 仓位恢复失败 | warning 日志（非 debug），恢复计数返回给调用方 |
| **PerformanceTracker** | 样本不足 | 回退到同 category 策略聚合绩效（StrategyCategory 枚举约束） |
