# Signals 策略模块数据流总览（当前实现版）

用于后续设计会话的运行时“真相”快照。按职责边界展开，便于你快速判断修改会影响哪些链路。
> 文档类型：当前实现真相（signals 域）。
> 信号域之外的启动顺序、日志路径、健康探针与持久化全景，请对照 `docs/design/full-runtime-dataflow.md`。
> 策略开发规范、regime 亲和度、单策略运行规则和 TF 参数不在本文重复展开，请对照 `docs/signal-system.md`。

## 0. 总体目标

信号模块的职责是：
1. 接受指标快照（confirmed/intrabar）；
2. 做策略评估、置信度变换、规则过滤；
3. 在可行时发布统一 `SignalEvent`；
4. 统一交给执行层，不直接下单、不直接改交易状态。

## 0.1 Claude 风格数据流图（结构化 ASCII）

```text
┌───────────────────────────────────────────────────────────────┐
│                      市场数据入链路                            │
└───────────────┬───────────────────────────────────────────────┘
                │
                ▼
┌───────────────────────────┐
│ BackgroundIngestor       │
│ - tick / quote / ohlc    │
└───────────┬──────────────┘
            │
            ├───────────────┐
            ▼               ▼
┌───────────────────────┐   ┌────────────────────────────┐
│ MarketDataService     │   │ UnifiedIndicatorManager    │
│ - 缓存内存             │   │ - confirmed pipeline        │
│ - 事件分发             │   │ - intrabar pipeline         │
└───────────┬───────────┘   └──────────────┬──────────────┘
            │                                   │
            │                                   ▼
            │                      ┌───────────────────────────────┐
            │                      │ publish_snapshot(scope=conf/ib)│
            │                      └───────────────┬──────────────┘
            │                                      │
            └──────▶ StorageWriter (持久化)            ▼
               (ticks/quotes/ohlc/intrabar)   ┌──────────────────────┐
                                            │ SignalRuntime._on_snapshot │
                                            │ add_snapshot_listener      │
                                            └───────────┬───────────────┘
                                                        │
                                                        │ check_warmup_barrier
                                                        │
                                                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│ SignalRuntime 入队层                                                 │
│                                                                    │
│  scope=confirmed ────────────▶ _confirmed_events（WAL）               │
│  scope=intrabar ────────────▶ _intrabar_events（内存）                │
└──────────────────────────────────────────────────────────────────────┘
                                                        │
                                                        ▼
                                            ┌──────────────────────────────┐
                                            │ 主循环 process_next_event()    │
                                            │ 1. dequeue_event                │
                                            │ 2. 过滤/退市窗口               │
                                            │ 3. regime 检测                 │
                                            │ 4. 策略评估 evaluate_strategies │
                                            │ 5. transition_and_publish     │
                                            └───────────────┬──────────────┘
                                                            │
                                                            ▼
                                   ┌───────────────────────────────────────────────┐
                                   │ runtime_evaluator                             │
                                   │ - scope 门控/指标齐套                      │
                                   │ - 信心调整（intrabar decay / event decay）     │
                                   │ - confirmed: transition_confirmed()           │
                                   │ - intrabar: coordinator 稳定计数             │
                                   └───────────────┬───────────────┬───────────┘
                                                   │ confirmed       │ intrabar
                                                   ▼                 ▼
                                   ┌─────────────────────┐   ┌──────────────────────┐
                                   │ SignalEvent(confirmed│   │ intrabar_armed_*     │
                                   │  /confirmed_* state) │   │（稳定计数达阈值）     │
                                   └──────────┬──────────┘   └──────────┬───────────┘
                                              │                         │
                                              └─────────────┬───────────┘
                                                            ▼
                                           ┌────────────────────────────────┐
                                            │ Signal Runtime Listeners        │
                                            │ - ExecutionIntentPublisher     │
                                            │ - SignalQualityTracker         │
                                            │ - HTFStateCache                │
                                           └────────────────────────────────┘
                                                            │
                                                            ▼
                                      ┌─────────────────────────────────────┐
                                        │ confirmed / intrabar_armed          │
                                        │ → ExecutionIntentPublisher          │
                                        │ → execution_intents                 │
                                        │ → ExecutionIntentConsumer           │
                                        │ → TradeExecutor.process_event       │
                                      └─────────────────────────────────────┘
```

## 0.2 Claude 风格状态变化快照（当前实现）

```text
confirmed scope:
  idle
    ├─ buy  -> confirmed_buy
    ├─ sell -> confirmed_sell
    └─ hold -> idle
  confirmed_buy/confirmed_sell
    ├─ hold -> confirmed_cancelled
    ├─ opposite signal -> confirmed_{opposite}
    └─ no signal -> idle

intrabar scope:
  strategy snapshot 到达
    ├─ buy/sell 并满足阈值 -> armed（intrabar_armed_buy/sell）
    ├─ 同向稳定累计 -> 再次尝试（同 bar 去重）
    └─ 不满足 -> 仅更新稳定计数，不发 confirmed 状态机
```

## 0.3 看板图：Confirmed 主链路（仅收盘态）

```text
┌──────────────────────────────────────────────────────────────┐
│ Confirmed Pipeline（收盘态）                               │
└──────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────┐
│ SignalRuntime._on_snapshot
└──────────┬───────────┘
           ▼
      check_warmup_barrier
           ├─fail→ (Stop: wait next snapshot)
           └─pass
               │
               ▼
      enqueue to _confirmed_events (WAL)
               │
               ▼
      process_next_event()
               │
               ▼
      apply_filter_chain?
       ├─block: [scope/stage/budget] 跳过 + 统计
       └─pass
               │
               ▼
      detect_regime / tracker.update
               │
               ▼
      evaluate_strategies()
        ├─session/timeframe/scope miss
        └─needed_indicators missing
            → snapshot skip
               │
               ▼
      confidence adjustments
               │
               ▼
      transition_confirmed()
        ├─buy -> confirmed_buy
        ├─sell -> confirmed_sell
        └─hold -> confirmed_cancelled or idle
               │
               ▼
      publish SignalEvent
               │
               ▼
      ExecutionIntentPublisher.on_signal_event
                │
                └─写入 execution_intents，交由目标账户 worker claim
```

## 0.4 看板图：Intrabar 主链路（仅盘中态）

```text
┌──────────────────────────────────────────────────────────────┐
│ Intrabar Pipeline（盘中态）                                 │
└──────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────┐
│ IndicatorManager intrabar snapshot 发布
└──────────┬───────────┘
           ▼
      SignalRuntime._on_snapshot
           ├─fail warmup/首bar指标 => stop
           └─pass
               │
               ▼
      enqueue to _intrabar_events
               │
               ▼
      process_next_event()
               │
               ▼
      stale_intrabar? (>300s)
       ├─yes → drop + 计数
       └─no
               │
               ▼
      filter chain
       ├─block → 仅统计跳过
       └─pass
               │
               ▼
      evaluate_strategies()
               │
               ▼
      coordinator update
       ├─同向稳定计数不足 → 无 armed
       └─达到阈值
          ├─发 intrabar_armed_buy/sell
          ├─persist 决策（snapshot trace）
          ├─通知 listeners
          │   └─ ExecutionIntentPublisher.on_signal_event
          │      ├─只接 confirmed_* / intrabar_armed_* 可执行信号
          │      ├─写入 execution_intents
          │      └─由 ExecutionIntentConsumer → TradeExecutor 处理
          │      └─下单（同 bar 同策略同方向仅一次）
          └─进入等待下个父 bar
```

## 0.5 看板图：异常与退化路径（Drop / Fail / Recovery）

```text
┌───────────────────────────────────────────────────────────────────────┐
│ 异常与退化路径（优先排障顺序）                                    │
└───────────────────────────────────────────────────────────────────────┘

1) warmup 失败
   snapshot -> _on_snapshot -> warmup_checker=false -> 丢弃
   核查: indicators 是否齐套 / 首 bar 缓冲是否建立

2) 入列失败（队列满）
   confirmed: put/full -> backpressure -> 等待(timeout) -> 仍满则 drop
             （高优先级，影响最大）
   intrabar: put/full -> wait/replace -> 仍满则 drop
             （影响 arm 计时）

3) 过滤阻塞
   filter_chain.should_evaluate=False
   原因: sessions/spread/economic/volatility
   结果: 事件本次不进入策略评估

4) 策略门控失败
   scope / session / strategy_timeframes 预过滤 / needed_indicators 不满足
   结果: 该策略当前 snapshot 直接结束，不进入单策略状态流转

5) 运行态恢复
   runtime start -> runtime_recovery.restore_state()
   仅恢复 confirmed 最近状态
   intrabar 没有长期状态恢复

6) 监听器异常
   publish_signal_event -> listener 抛错
   计数到达阈值后自动摘除 listener（避免雪崩）

排障优先看:
SignalRuntime.status() + runtime_recovery + queue/drop/stale + filter_block_reason
```

## 1. 上游输入与触发源

```text
┌───────────────┐
│    MT5 接入     │
└───────┬───────┘
        │
        ▼
┌───────────────────────┐
│ BackgroundIngestor     │
│ - tick / quote / ohlc  │
│ - 初筛与分发            │
└───────┬───────────────┘
        │
        ├───────────────┐
        ▼               ▼
┌───────────────────┐ ┌────────────────────────────┐
│ MarketDataService │ │ UnifiedIndicatorManager     │
│ - 内存缓存         │ │ - confirmed pipeline        │
│ - bus 事件推送      │ │ - intrabar pipeline         │
└───────┬───────────┘ └───────────────┬────────────┘
        │                               │
        │                               ├────▶ confirmed snapshot.publish_snapshot()
        │                               └────▶ intrabar snapshot.publish_snapshot()
        ▼                               ▼
┌───────────────────────┐        ┌───────────────────────┐
│   StorageWriter       │        │ SignalRuntime        │
│ - ticks/quotes/ohlc   │        │ add_snapshot_listener│
│ - intrabar            │        │ _on_snapshot         │
│ - 持久化链路         │        └──────────┬──────────┘
└───────────────────────┘                   │
                                            ▼
                                     check_warmup_barrier()
                                      ├─不通过: 丢弃
                                      └─通过: _enqueue -> 事件队列
                                              │
                                              ▼
                                    等待 process_next_event
```

confirmed 与 intrabar 的快照发布路径：

1. `UnifiedIndicatorManager` 在各自 pipeline 完成后调用 snapshot listeners；
2. `SignalRuntime` 通过 `snapshot_source.add_snapshot_listener(self._on_snapshot)` 监听；
3. `signal.ini`/策略声明决定 intrabar 指标集合；
4. `SignalRuntime._on_snapshot()` 只在 warmup 通过时入队。

## 2. SignalRuntime 主链路
```text
事件链（主时序）

0) UnifiedIndicatorManager
   └─ publish_snapshot(scope=confirmed|intrabar, metadata, indicators)
      -> SignalRuntime._on_snapshot

1) _on_snapshot
   └─ warmup / 首bar 校验与 metadata 构建
      ├─不通过: 记录 block_reason -> return
      └─通过: 入队
          ├─confirmed -> _confirmed_events（WAL）
          └─intrabar  -> _intrabar_events（可丢弃）

2) process_next_event()
   └─ dequeue_event() 取 confirmed 优先（intrabar 间歇插入）
      ├─intrabar stale>300s: drop + skip
      └─继续
         └─SignalFilterChain.apply_filter_chain()
            ├─block: block reason 统计
            └─pass:
                ├─confirmed: detect_regime + tracker update
                └─evaluate_strategies + transition/publish
                   ├─confirmed -> SignalEvent(scope=confirmed, state=confirmed_*)
                   └─intrabar armed -> SignalEvent(scope=intrabar_armed_*)
```

### 2.4 数据流建议可视化（渲染优化）

- 第一层：Claude 风格结构图（模块边界）
- 第二层：Claude 风格事件时序清单（因果顺序）
- 第三层：Claude 风格状态清单（确认/盘中状态切换）
- 已全量改为文本图，便于 diff、review 与命令行阅读

### 2.1 入队分流

- confirmed 进入 `_confirmed_events`（WAL 持久化）
- intrabar 进入 `_intrabar_events`（内存队列）
- 两队列并发：confirmed 优先，已通过 `CONFIRMED_BURST_LIMIT` 机制周期性给 intrabar 机会；
- confirmed 事件支持 backpressure；intrabar 在满时采取等待/替换策略，仍保留可丢弃语义。

### 2.2 单事件处理

`process_next_event()` 按以下顺序执行：

1. 取队列事件（按 scope 优先级）；
2. intrabar 超时 `>300s` 跳过；
3. `SignalFilterChain` 过滤（时段/点差/经济事件/波动率）；
4. regime 检测（confirmed 更新 tracker，intrabar 只读 stability）；
5. `evaluate_strategies()`；
6. `transition_and_publish()`（confirmed 状态变更/事件发布 + intrabar armed）。

### 2.3 任务分拆（重要）

`SignalRuntime` 的主要职责交给 `orchestration/*_module.py` 细化：

- `runtime_processing.py`：`on_snapshot` + 入队与预检查；
- `runtime_processing.py`：队列与过滤主流程；
- `runtime_evaluator.py`：策略评估、置信度调整、发布；
- `runtime_recovery.py`：confirmed 运行态恢复；
- `state_machine.py`：confirmed 状态转换；
- `intrabar_trade_coordinator.py`：intrabar 稳定计数与 armed 触发；

## 3. 策略评估路径（`evaluate_strategies`）

对每个 `(symbol, timeframe)` 的目标策略按以下顺序走门控：

1. `strategy_sessions` 白名单；
2. `strategy_timeframes` 白名单（已在 `SignalRuntime` 初始化/热更新时预过滤）；
3. scope 门控（`scope in strategy.preferred_scopes`）；
4. `needed_indicators` 严格齐套；
5. 快照去重（`RuntimeSignalState` 内的 signature）。

通过后调用：

- `SignalModule.evaluate(symbol, timeframe, strategy, indicators, metadata, htf_indicators, persist=False)`
- 经过 `apply_confidence_adjustments`：
  - intrabar decay；
  - 经济事件 decay；
  - confidence floor。

随后：

- confirmed：按 `transition_confirmed()` 计算 `idle`、`confirmed_buy`、`confirmed_sell`、`confirmed_cancelled`；
- intrabar：调用 coordinator 更新稳定计数。

### 2.X 策略能力清单统一契约（新增）

- 标准输入口：`SignalModule.strategy_capability_catalog()` → `SignalPolicy.strategy_capability_contract()`
- 输出最小字段：`name / valid_scopes / needed_indicators / needs_intrabar / needs_htf / regime_affinity / htf_requirements`
- 回测与实盘运行时都消费同一份能力快照；`/admin/strategies/capability-reconciliation` 提供 module/policy 对账视图（`module_only` / `runtime_only` / `drift_items`），
  `/admin/strategies/capability-contract` 提供两端完整能力清单及对账结果，支持新增/回归时一眼确认入链一致性。

### 2.X.1 策略能力执行计划（runtime/backtest 统一语义）

- Runtime 状态新增：`strategy_capability_execution_plan`
- Backtest 结果新增：`strategy_capability_execution_plan`
- Admin 对账入口：`GET /admin/strategies/capability-execution-plan`（`module/runtime/backtest`）
- 两端字段语义保持一致：`configured/scheduled/filtered`、scope 覆盖、`needed_indicators` 并集、`strategy_timeframes` 过滤原因。
- 两端执行计划现由共享构建器统一生成：`src/signals/contracts/execution_plan.py::build_strategy_capability_summary`，避免 runtime/backtest 双份实现漂移。
- admin `module_plan` 也复用同一构建器，对账入口统一按 `scheduled_strategies` 比较策略集合。

```text
策略能力执行计划（Claude 风格）

输入层
  configured_targets
    └─ 来自 targets 配置（symbol/timeframe/strategy）
  capability_contract
    └─ 来自 SignalModule.strategy_capability_contract()

调度层
  strategy_timeframes 预过滤
    ├─ scheduled_targets（保留）
    └─ filtered_targets（移除 + reason）
       └─ reason: strategy_timeframes_filtered / capability_missing / prefilter_excluded

能力层（仅对 scheduled_strategies）
  scope 覆盖
    ├─ confirmed
    └─ intrabar
  needs 标记
    ├─ needs_intrabar_strategies
    └─ needs_htf_strategies
  指标需求
    ├─ required_indicators_by_strategy
    └─ required_indicators_union

输出层
  runtime.status().strategy_capability_execution_plan
  backtest_result.strategy_capability_execution_plan
```

### 3.1 策略评估与状态流转（可视化）

```text
策略评估与状态流转

snapshot event
  └─ session 白名单
    └─ timeframe 白名单
    └─ strategy 能力快照门控（`SignalModule.strategy_capability_catalog()`）
    └─ scope 门控（统一口子）
      └─ needed_indicators 检查
        └─ snapshot 去重
            └─ SignalModule.evaluate(strategy context)
              └─ confidence_adjustments
                 ├─ intrabar decay
                 ├─ economic event decay
                 └─ confidence floor
                    └─ scope 分流
                       ├─ confirmed -> transition_confirmed -> 状态机
                       │              └─ persist? + publish(confirmed_* / confirmed_cancelled)
                       └─ intrabar  -> IntrabarTradeCoordinator
                                      └─ stable_count + 门槛判断（min_stable_bars/min_confidence）
                                         └─ 命中则 publish intrabar_armed_*，否则终止本轮
```

## 4. 单策略结果收口

当前运行时不再做 vote/consensus 聚合：

- 每个策略独立产出 `SignalDecision`；
- `buy/sell` 直接进入 `transition_and_publish()`；
- `hold` 或无方向结果直接在 trace 中记为 `no_signal`；
- 历史库中遗留的 `voting_completed` 仅在读侧归一化展示，不再有新的生产路径。

### 4.1 单策略与状态更新（状态角度）

```text
confirmed 状态机

[ * ]
  └─> ConfirmedIdle
      ├─ 买入决策          -> ConfirmedBuy
      ├─ 卖出决策          -> ConfirmedSell
      └─ hold 或空         -> ConfirmedIdle
ConfirmedBuy
  ├─ hold 本 bar          -> ConfirmedCancelled
  └─ 无信号/空心跳       -> ConfirmedIdle
ConfirmedSell
  ├─ hold 本 bar          -> ConfirmedCancelled
  └─ 无信号/空心跳       -> ConfirmedIdle
ConfirmedCancelled
  ├─ 本 bar 再买          -> ConfirmedBuy
  ├─ 本 bar 再卖          -> ConfirmedSell
  └─ 空信号              -> ConfirmedIdle
```

```text
intrabar 状态机

[ * ]
  └─> IB_Idle
      └─ intrabar buy/sell 决策到达 -> IB_Track
IB_Track
  ├─ 同向累计             -> stable_count+1（自循环）
  ├─ 方向变化 / 新 bar     -> stable_count 重置（自循环）
  ├─ stable_count>=阈值 且 conf>=阈值 -> IB_Armed
  └─ 本 bar 无决策        -> IB_Idle
IB_Armed
  ├─ 同 bar 同策略同方向重复事件 -> ignore（一次去重）
  └─ 退出本 bar -> [*]
```

## 5. 上下游事件与执行

发布路径：

- `SignalRuntime._publish_signal_event()` → 注册监听器
  - `ExecutionIntentPublisher.on_signal_event()`（confirmed / intrabar_armed）
  - `SignalQualityTracker`
  - `HTFStateCache`

intrabar 入口流程：

- `intrabar_armed_buy/sell` 进入 `ExecutionIntentPublisher`
- worker claim 后由 `TradeExecutor._handle_intrabar_entry()` 处理
- 经 `IntrabarTradeGuard`/`ExecutionGate`/完整前置 filters；
- 执行下单；
- `IntrabarTradeGuard` 记录同 bar 同策略同方向去重；
- 由 confirmed 收盘事件处理 intrabar 与 confirmed 的协调关系。

confirmed 与 intrabar 协同：

- 同向 intrabar 仓位下 confirmed 新开不重复；
- hold 持续由出场规则管理；
- confirmed 反向时按既有 confirmed 处理路径回撤或反向。

## 6. 关键数据对象（你最容易联调）

- `SignalEvent`：标准广播对象（scope, signal_state, reason, metadata）；
- `RuntimeSignalState`：每 target 的状态快照；
- `SignalDecision`：策略决策核心输入；
- snapshot metadata（`MetadataKey`）：
  - `trace_id`, `bar_time`, `snapshot_time`, `scope`, `regime`, `regime_probabilities`；
  - `confidence` 中间值：`raw_confidence`, `regime_affinity`, `session_performance_multiplier`, `post_performance_confidence`；
  - intrabar：`intrabar_stable_bars` 等指标。

## 7. 观测与排障建议（模块化前提）

1. 优先看 `SignalRuntime.status()`：确认 `queue/drop/stale/filter`；
2. 看 `single-strategy trace` 与 `pipeline trace`，确认是否是评分/过滤导致“没交易”；
3. 确认 `strategy_scope/session/timeframe` 白名单是否误收紧；
4. 看 `indicator requirement` 与 `_indicator_miss_counts`，避免首个 bar 指标不全；
5. 关注以下可观测字段（可直接用于回溯决策漂移）：
   - `drop_rates`：总丢弃率、`intrabar` 过期丢弃率、confirmed/intrabar 丢弃占比；
   - `dropped_intrabar_stale`：`intrabar` 入队后超过 300s 的过期丢弃计数；
6. 看 `runtime_recovery` 恢复记录与 `confirmed_cancelled` 频次，识别重启漂移。

## 8. 设计约束（用于后续会话）

1. 不要把 execution/filter/regime/persistence 逻辑塞回 `service.py`；
2. confirmed 与 intrabar 的职责不得混写在同一个状态机分支；
3. 关键语义变更优先通过公开端口更新（如 `set_warmup_ready_fn`, `set_intrabar_trade_coordinator`, `add_signal_listener`）；
4. 新增功能应优先在 `orchestration` 内按职责子模块落地，而不是在 main runtime 做长分支判断。
