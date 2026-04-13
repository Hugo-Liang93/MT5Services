# Intrabar 数据流图

> 文档类型：当前 intrabar 支链实现图。
> 启动顺序、持久化全景、日志路径与健康探针请对照 `docs/design/full-runtime-dataflow.md`；本文只展开 intrabar 支链。

## 1. 全局视角：子 TF close 驱动父 TF Intrabar 合成

```
                           ┌─────────────────────────────┐
                           │         MT5 Terminal         │
                           └──────────┬──────────────────┘
                                      │
                           ┌──────────▼──────────────────┐
                           │    BackgroundIngestor        │
                           │    src/ingestion/ingestor.py │
                           └──────────┬──────────────────┘
                                      │
                           ┌──────────▼──────────────────┐
                           │  _ingest_ohlc()              │
                           │  所有 TF bar close 检测      │
                           └──┬───────────────────────────┘
                              │
      ┌───────── │ ─── tf in trigger_reverse? ──┐
      │ YES      │                              │ NO
      ▼          ▼                              ▼
 ┌────────────────────────┐              confirmed 事件
 │ _synthesize_parent_    │              走正常链路
 │   intrabar()           │
 │ 从内存 confirmed bars  │
 │ 合成父 TF 当前 bar     │
 │ + 入 DB (intrabar)     │
 └───────────┬────────────┘
             │
             │  同时：子 TF 的 confirmed
             │  事件走正常 confirmed 链路
             │
             │ service.set_intrabar(symbol, parent_tf, bar, metadata=...)
             ▼
              ┌─────────────────────────────────────┐
              │  MarketDataService.set_intrabar()    │
              │  src/market/service.py:444           │
              │  ┌─ 写入 _intrabar_cache (API 用)   │
              │  ├─ 写入 _intrabar_metadata_cache   │
              │  │   (trigger_tf / synthesized_at / │
              │  │    stale_threshold / child count)│
              │  └─ event_bus.dispatch_intrabar()    │
              └──────────────┬──────────────────────┘
                             │ ThreadPoolExecutor(2) 异步
                             ▼
              ┌─────────────────────────────────────┐
              │  MarketEventBus.dispatch_intrabar()  │
              │  src/market/event_bus.py:127         │
              │  广播所有 intrabar_listeners         │
              └──────────────┬──────────────────────┘
                             │
                             ▼
                    ══════════════════
                    指标 → 信号 管道
                    （见下方 §2, §3）
                    ══════════════════
```

## 2. 指标计算管道

```
              MarketEventBus
                    │
                    │ _on_intrabar(symbol, tf, bar)
                    ▼
 ┌──────────────────────────────────────────────────────────┐
 │  UnifiedIndicatorManager                                 │
 │  src/indicators/manager.py                               │
 │                                                          │
 │  _on_intrabar() ─── put_nowait ──→ _intrabar_queue      │
 │  (立即返回，不阻塞 ingestor)       Queue(maxsize=200)    │
 │                                    满则丢弃 (L3)         │
 │                                         │                │
 │  _intrabar_loop() (专用线程) ◄──────────┘                │
 │    │                                                     │
 │    ├─ 无 snapshot_listeners? → 跳过（省 CPU）            │
 │    ├─ 节流: max(1s, tf_seconds × 2%)                     │
 │    │                                                     │
 │    ▼                                                     │
 │  _process_intrabar_event()                               │
 │  src/indicators/bar_event_handler.py:261                 │
 │    │                                                     │
 │    ├─ get_intrabar_eligible_names()                      │
 │    │    启动时由策略 preferred_scopes 自动推导            │
 │    │                                                     │
 │    ├─ _load_intrabar_bars()                              │
 │    │    N-1 confirmed bars + 当前 intrabar bar           │
 │    │                                                     │
 │    ├─ _compute_intrabar_results_for_bars()               │
 │    │    仅计算 eligible 指标子集                          │
 │    │                                                     │
 │    ├─ _apply_delta_metrics()                             │
 │    │    N-bar 变化率                                     │
 │    │                                                     │
 │    └─ publish_intrabar_snapshot()                        │
 │         src/indicators/snapshot_publisher.py:86          │
 │         ┌─ store_preview_snapshot()（函数名沿用历史实现）     │
 │         │   去重: (bar_time, indicators) 完全相同 → 跳过 │
 │         └─ publish_snapshot(scope="intrabar")            │
 │              通知所有 _snapshot_listeners                 │
 └────────────────────────────┬─────────────────────────────┘
                              │
                              │ listener(symbol, tf, bar_time, indicators, "intrabar")
                              ▼
                     ═══════════════════
                     信号评估管道（§3）
                     ═══════════════════
```

## 3. 信号评估 + 交易执行管道

```
              snapshot_listener
                    │
                    ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  SignalRuntime._on_snapshot()                                        │
 │  src/signals/orchestration/runtime.py:363                           │
 │                                                                      │
 │  ├─ enable_intrabar = false? → return                               │
 │  ├─ check_warmup_barrier()                                          │
 │  │    回补未完成 / staleness / 指标不全 → 跳过                      │
 │  ├─ build_snapshot_metadata()                                       │
 │  │    注入 spread + intrabar_synthesis 元数据                       │
 │  └─ _enqueue() → _intrabar_events                                  │
 │                   Queue(maxsize=8192)                                │
 │                   put_nowait, 满则丢弃 + 计数                       │
 └──────────────────────────────┬───────────────────────────────────────┘
                                │
                                ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  _loop() → process_next_event()     (signal-runtime 线程)           │
 │  src/signals/orchestration/runtime_processing.py:158                │
 │                                                                      │
 │  出队优先级（dequeue_event）:                                       │
 │  ┌─────────────────────────────────────────────────┐                │
 │  │ 1. 优先取 _confirmed_events (WAL 持久化)        │                │
 │  │ 2. 每 5 个 confirmed 让 1 个 intrabar 插队      │                │
 │  │ 3. confirmed 空 → _intrabar_events.get(0.5s)    │                │
 │  └─────────────────────────────────────────────────┘                │
 │                                                                      │
 │  ① is_stale_intrabar()  — 入队超 300s → 丢弃                       │
 │  ② apply_filter_chain() — 时段/冷却/点差/经济事件/波动率            │
 │  ③ detect_regime()      — intrabar 不更新 tracker，只读 stability   │
 │  ④ _evaluate_strategies()                                           │
 │       scope ∉ strategy.preferred_scopes → 跳过                      │
 │       required_indicators 不全 → 跳过                               │
 │       → strategy.evaluate(context) → SignalDecision                 │
│  ⑤ transition_and_publish()                                         │
│  ⑥ no_signal / intrabar_armed 收口                                  │
 │       ├─ confirmed scope：confirmed 状态机转换 + 可发 signal event   │
 │       ├─ publish_signal_event()                                     │
 │       └─ IntrabarTradeCoordinator.update() ──────────┐              │
 │            (仅 coordinator != None 且 buy/sell 时)    │              │
 └───────────────────────────────────────────────────────┼──────────────┘
                                                         │
          ┌──────────────────────────────────────────────┘
          │ stable_count >= min_stable_bars(3)
          │ AND confidence >= min_confidence(0.75)
          │ → 发布 intrabar_armed_buy/sell
          ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Signal Listeners                                                    │
 │                                                                      │
 │  ├─→ ExecutionIntentPublisher.on_signal_event()                     │
 │  │     (confirmed / intrabar_armed 统一发布 intent)                 │
 │  ├─→ SignalQualityTracker                                           │
 │  └─→ HTFStateCache                                                  │
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ intrabar_armed_* 事件
                                ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  execution_intents                                                  │
 │  main 写入 → executor claim                                         │
 └──────────────────────────────┬───────────────────────────────────────┘
                                ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  ExecutionIntentConsumer._process_intent()                          │
 │    └─ TradeExecutor.process_event(scope="intrabar")                 │
 │        └─ _handle_intrabar_entry()                                  │
 │                                                                      │
 │  1. IntrabarTradeGuard.can_trade()                                  │
 │     同 bar / 同策略 / 同方向 → 只允许一次                           │
 │                                                                      │
 │  2. ExecutionGate.check_intrabar()                                  │
 │     intrabar_trading_enabled? + 策略白名单                          │
 │                                                                      │
 │  3. _run_pre_trade_filters()                                        │
 │     复用完整 pre-trade filter chain                                 │
 │     + quote freshness                                               │
 │     + intrabar_synthesis freshness                                  │
 │                                                                      │
 │  4. _compute_params_helper()                                        │
 │     ATR 来自上一根 confirmed bar                                    │
 │                                                                      │
 │  5. 下单 (market / limit / stop)                                    │
 │                                                                      │
 │  6. IntrabarTradeGuard.record_trade()                               │
 └──────────────────────────────────────────────────────────────────────┘

                    ┌───────────────────────┐
                    │ 父 TF bar 最终收盘时  │
                    │ (confirmed 链路)      │
                    └───────────┬───────────┘
                                ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Confirmed 协调  (executor.py _handle_confirmed)                    │
 │                                                                      │
 │  guard.has_intrabar_position(symbol, tf, strategy, bar_time)?       │
 │    ├─ 有仓 + confirmed 同向  → skip（不重复开仓）                   │
 │    ├─ 有仓 + confirmed hold  → 不动（交给出场规则）                 │
 │    └─ 有仓 + confirmed 反向  → 正常处理（开反向仓）                 │
 │                                                                      │
 │  guard.on_parent_bar_close() → 清理该 bar 状态                      │
 └──────────────────────────────────────────────────────────────────────┘
```

## 4. 队列与去重汇总

```
┌─────────────────┬──────────────┬──────────┬───────────────────────────┐
│ 节点            │ 队列/缓冲    │ 容量     │ 丢弃策略                  │
├─────────────────┼──────────────┼──────────┼───────────────────────────┤
│ Ingestor→       │ _intrabar_   │ 200      │ put_nowait, 满则丢弃      │
│ IndicatorMgr    │ queue        │          │                           │
├─────────────────┼──────────────┼──────────┼───────────────────────────┤
│ IndicatorMgr→   │ (直接回调)   │ —        │ —                         │
│ SignalRuntime    │              │          │                           │
├─────────────────┼──────────────┼──────────┼───────────────────────────┤
│ SignalRuntime    │ _intrabar_   │ 8192     │ put_nowait, 满则丢弃      │
│ 内部            │ events       │          │                           │
├─────────────────┼──────────────┼──────────┼───────────────────────────┤
│ SignalRuntime    │ _confirmed_  │ 4096/WAL │ 回压等待 → 超时才丢弃    │
│ 对比 confirmed  │ events       │          │ WAL 持久化重启可恢复      │
└─────────────────┴──────────────┴──────────┴───────────────────────────┘

┌─────────────────┬───────────────────┬────────────────────────────────┐
│ 去重节点        │ 位置              │ 逻辑                           │
├─────────────────┼───────────────────┼────────────────────────────────┤
│ Ingestor        │ _synthesize_      │ 合成 bar OHLC 不变 → 跳过     │
│                 │ parent_intrabar() │                                │
├─────────────────┼───────────────────┼────────────────────────────────┤
│ IndicatorMgr    │ snapshot_         │ (bar_time, indicators)         │
│                 │ publisher.py:33   │ 完全相同 → 不发布              │
├─────────────────┼───────────────────┼────────────────────────────────┤
│ SignalRuntime    │ runtime_          │ 入队超 300s → stale 丢弃      │
│                 │ processing.py:45  │                                │
├─────────────────┼───────────────────┼────────────────────────────────┤
│ TradeGuard      │ intrabar_         │ 同 bar/策略/方向 →             │
│                 │ guard.py:35       │ 只允许入场一次                 │
└─────────────────┴───────────────────┴────────────────────────────────┘

┌─────────────────┬───────────────────┬────────────────────────────────┐
│ 节流节点        │ 位置              │ 公式                           │
├─────────────────┼───────────────────┼────────────────────────────────┤
│ Ingestor 采集   │ 子 TF bar 周期    │ 由 trigger 配置决定            │
│ (trigger 模式)  │                   │ (如 H1=M5 → 每 5min)          │
├─────────────────┼───────────────────┼────────────────────────────────┤
│ IndicatorMgr    │ manager.py:458    │ max(1s, tf×2%)                 │
│ 计算            │                   │                                │
└─────────────────┴───────────────────┴────────────────────────────────┘
```

## 5. Trigger 配置示例

```
signal.ini [intrabar_trading.trigger]:

    M5  = M1       M15 = M1       M30 = M1       H1  = M5       H4  = M15

反查表 (_intrabar_trigger_reverse):

    M1  → [M5, M15, M30]
    M5  → [H1]
    M15 → [H4]

示例：M1 bar close 时
  → 同时合成 M5 + M15 + M30 三个 TF 的 intrabar bar
  → 各自注入 set_intrabar() → 独立走完 指标→信号→交易 管道
```
