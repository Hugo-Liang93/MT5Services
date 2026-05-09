# 全量运行时数据流图（当前实现版）

> 更新日期：2026-05-06
> 目的：给当前代码库一份“可审查、可对照实现、可映射真实运行结果”的完整数据流图。  
> 本文不是理想架构图，而是基于当前仓库与真实启动结果整理的运行时真相。
> 策略开发规范、regime、单策略契约与规划类方案不在本文重复展开，统一由各专题文档维护。

---

## 0. 当前验证基线

本轮结论基于以下已执行验证：

1. 真实容器启动验证：直接调用 `deps._ensure_initialized()` + `runtime.start()`，确认 `startup.ready=true`，且 `startup_summary.mode` 与 `RuntimeModeController` 当前模式一致。
2. 真实 Web 入口验证：直接运行 `python -m src.entrypoint.web`，探测：
   - `GET /health`
   - `GET /v1/monitoring/health/ready`
3. 系统主链路专项回归通过：
   - `采集 -> 缓存 -> closed-bar 事件 -> indicator durable queue -> health/readmodel`
   - 相关专项回归 `56 passed`

当前可确认的系统态：

| 项 | 当前状态 | 说明 |
|---|---|---|
| Web 入口 | 正常启动 | `python -m src.entrypoint.web` 可进入服务态 |
| `/v1/monitoring/health/ready` | `ready` | main 需 `storage_writer=ok`、`ingestion=ok`、`market_data_health=ok`、`indicator_engine=ok`；若启用 `tick_derived` 策略还需 `tick_feature_health=ok`；executor 需 `execution_intent_consumer=ok`、`operator_command_consumer=ok`、`pending_entry=ok`、`position_manager=ok`、`account_risk_state=ok` |
| 启动状态恢复 | 已收敛 | `TradingStateRecovery` 会以 MT5 当前持仓为事实源，把本地遗留 open position runtime state 标记为 `closed / mt5_missing` |
| 采集线程 | 运行中 | `ingest_alive=true` |
| 异步落库线程 | 运行中 | `writer_alive=true` |
| 指标事件循环 | 运行中 | `event_loop_running=true` |
| 信号运行时 | 运行中 | `signal_runtime.is_running()=true` |
| 经济日历 | 运行中 | 最近一轮刷新成功 |
| TradeExecutor | `is_running=false` | 这是当前实现的“懒启动 worker”语义，不代表组件未装配；只有收到首个交易信号才拉起执行线程 |

补充约束：

1. `TradeExecutor`、`pending_orders`、`pre_trade_checks` 只负责阶段性执行事实和正式结果对象，不再各自补发 terminal pipeline 事件。
2. `ExecutionIntentConsumer` 是 execution intent 主链唯一的 terminal 事件发射者，统一落 `execution_succeeded / execution_skipped / execution_failed`。
3. 单账户/本地 queue worker 不再吞掉 `process_event()` 的正式结果，而是复用同一 terminal 结果解释服务发出终态；因此单账户与 worker 主链的 terminal 语义一致。
4. `BackgroundIngestor.health_snapshot()` 是市场采集健康事实源：MT5 circuit、abandoned calls、quote/tick/OHLC lane freshness 和 per-symbol backoff 都从这里进入 ready 与 monitoring。
5. `ExecutionIntentConsumer.health_snapshot()` 与 `OperatorCommandConsumer.health_snapshot()` 是执行消费推进事实源：ready 不再只看线程 alive，而是同时检查 poll/claim/complete 进展、连续错误和 in-flight stall。
6. `SignalEvent.metadata` 是 EntryPolicy 与执行审计的正式合同：SignalModule 的策略 metadata 与上下文 metadata 会在 SignalRuntime 发布边界合并，确保 `ENTRY_INTENT`、`RECENT_BARS`、spread、bar_time 不在信号到执行之间丢失。
7. `StrategyCapability.market_data_requirements` 是策略对 quote/tick/OHLC 的正式依赖声明。`SignalRuntime.required_market_data_lanes()` 按已调度 symbol/TF 展开具体 lane，`BackgroundIngestor.health_snapshot().dependency_contract` 负责把 required lane stale/missing 升级为 `critical`。
8. `RuntimeReadModel.tradability_state_summary()`、`TradeAdmissionService` 与 `TradeExecutor` 共同消费 `market_data_health` 公开投影；required lane critical 会以 `market_data_unhealthy` 进入准入报告和预交易过滤，不再只是 ready 探针告警。
9. `TradingFlowTraceReadModel` 是单笔交易 lifecycle 的 canonical 投影 owner：signal、pipeline、command audit、pending/position runtime、`position_sl_tp_history` 与 outcome 在这里汇总为 `lifecycle.summary / entry / management / exit / outcome / timeline / data_gaps`，API 与 workbench 不再各自拼接。
10. `TickFeatureEngine` 是 tick-derived 特征与健康事实拥有者：`MT5 ticks -> persisted ticks -> TickBatchEvent -> TickFeatureSnapshot -> SignalRuntime tick_derived -> execution intent -> admission -> risk -> executor` 已成为正式链路；`tick_feature_health` 与 `market_data_health` 分开投影，只有 tick-derived 策略受该健康事实硬阻断。

需要单独解释的运行态现象：

1. 当前是黄金休盘窗口，`market_data data latency critical` 类告警会出现，这是“没有新鲜行情”的环境现象，不等于系统链路断裂。
2. 经济日历外部源偶发超时仍可能发生，这是外部依赖可用性问题，不是当前本地数据管线契约错误。

---

## 1. 一图总览

```text
[外部系统]
  [MT5 Terminal] -------------------------------> [BackgroundIngestor]
  [Jin10 / 经济日历提供方] -----------------------> [EconomicCalendarService]

[运行时主链]
  [BackgroundIngestor]
      |--> 更新内存 -----------------------------> [MarketDataService]
      |--> 异步落库 -----------------------------> [StorageWriter] --> [TimescaleDB]
      |                                                           `-> [data/dlq/*.jsonl]
      `--> closed-bar / intrabar 事件 ----------> [UnifiedIndicatorManager]

  [MarketDataService]
      `--> TickBatchEvent -----------------------> [TickFeatureEngine]
                                                        |--> [TickFeatureBus]
                                                        |--> [TickFeatureHealthStore]
                                                        `--> tick_derived snapshot -> [SignalRuntime]

  [UnifiedIndicatorManager]
      |--> event_write_queue -------------------> [SQLite events.db]
      |<-- claim_next_events --------------------'
      |--> confirmed / intrabar snapshot ------> [SignalRuntime]
      `--> pipeline event ----------------------> [PipelineEventBus]
                                                    |
                                                    v
                                           [PipelineTraceRecorder] --> [TimescaleDB]

  [SignalRuntime]
      |--> confirmed_buy/sell ------------------> [ExecutionIntentPublisher(main)]
      |--> intrabar_armed_* --------------------> [ExecutionIntentPublisher(main)]
      |--> tick_derived_buy/sell ---------------> [ExecutionIntentPublisher(main)]
      |                                             |
      |                                             v
      |                                      [execution_intents]
      |                                             |
      |                                             v
      |                                   [ExecutionIntentConsumer(target account)]
      |                                             |
      |                                             v
      |                                        [TradeExecutor]
      |                                             |--> [PendingEntryManager] --> [StorageWriter]
      |                                             |--> [PositionManager] -----> [StorageWriter]
      |                                             `--> 执行阶段事实 -----------> [StorageWriter]
      `--> demo_validation signal --------------> [demo-main 实例真实下单链路（同 live 主链）] --> [StorageWriter]

  [FastAPI / Operator APIs]
      -> [OperatorCommandService]
      -> [operator_commands]
      -> [OperatorCommandConsumer(target account)]
      -> [TradingModule / RuntimeModeController / PendingEntryManager / Closeout]
      -> [trade_command_audits + PipelineEventBus]

  [EconomicCalendarService]
      |--> 日历事实 / 更新 ----------------------> [StorageWriter]
      `--> provider / job 状态 -----------------> [HealthMonitor + MonitoringManager]

[读模型 / 展示]
  [MarketDataService / Indicator / Signal / Executor / Pending / Position / EconomicCalendar / Health]
      --> [RuntimeReadModel] --> [FastAPI /v1/*]
                           `--> [StudioService]

  [TimescaleDB] --> [TradingFlowTraceReadModel] --> [FastAPI /v1/*]

[可观测 / 本地文件]
  [HealthMonitor + MonitoringManager] ----------> [health_monitor.db]
  [入口日志] ------------------------------------> [data/logs/*.log]
```

### 1.1 策略主线边界：PA、tick-derived 与 recovery

当前普通 `signals` 策略池只保留 PA candidate；高频验证分为 tick-derived 数据/特征路线和 bounded recovery 交易基建路线：

| 节点 | 事实源 | 状态拥有者 | 输出 |
|---|---|---|---|
| PA 市场数据输入 | MT5 OHLC M15/M30 confirmed bar | `BackgroundIngestor` / `MarketDataService` | closed-bar event |
| PA 指标计算 | `atr14`、`candle_pattern` 等 confirmed-bar 指标 | `UnifiedIndicatorManager` | confirmed indicator snapshot |
| PA 信号生成 | `structured_price_action` confirmed-only capability | `SignalRuntime` / `SignalModule` | `confirmed_buy/sell` + `ENTRY_INTENT` + `exit_spec` |
| PA 入场执行 | `config/entry_policy.ini` 中 `structured_price_action = oco_pullback_breakout` | `trading.entry_policy` / `ExecutionIntentPublisher` | execution intent |
| 风控门禁 | `config/instances/demo-main/risk.ini` 或 `live-main/risk.ini` | `PreTradeRiskService` / `src/risk/rules.py` | allow/block 风控结果 |
| tick-derived 数据输入 | MT5 tick `bid/ask/last/volume/time/time_msc/flags` | `BackgroundIngestor` / `MarketDataService` | `TickBatchEvent` |
| tick-derived 特征 | 短窗口 tick batch；最新 tick 必须具备 bid/ask 可成交侧价 | `TickFeatureEngine` / `TickFeatureCalculator` | `TickFeatureSnapshot` + `tick_feature_health`；缺 bid/ask 会进入 `blocked / quote_side_missing` |
| tick-derived 信号 | `preferred_scopes=("tick_derived",)` + `market_data_requirements=("tick",)` | `SignalRuntime` 独立 `tick_derived` 队列 | tick-derived execution intent |
| tick-derived 准入 | `market_data_health` + `tick_feature_health` + risk | `RuntimeReadModel` / `TradeAdmissionService` / `TradeExecutor` | stale/sparse/blocked 时 fail-closed |
| tick replay | persisted ticks + same `TickFeatureCalculator`；覆盖率只统计具备 bid/ask 的可成交 tick | `src/backtesting/tick_replay` | tick coverage、feature coverage、真实 spread、bid/ask 成交成本报告 |
| bounded recovery / martingale | 显式 canary/恢复周期命令，不消费普通策略绑定 | `src/trading/recovery` / `src.ops.cli.recovery_canary` | 初始单、补仓计划、cycle state、trace 与 cleanup |

Intrabar 仍是增强支线：只有策略显式声明 `preferred_scopes` 包含 `intrabar` 且 `[intrabar_trading].enabled_strategies` 非空时才进入盘中 armed 链路。tick-derived 不复用 intrabar 队列或 stale 语义；生产策略启用前必须先有同策略、同品种、同交易时段画像的 tick replay 报告，再做 demo canary。

### 1.2 Bounded Recovery / Martingale 验证边界

马丁类逻辑在本架构中归属 `trading.recovery` 资金管理域，不归属 `signals` 策略域。策略只能给方向、置信度和 tick feature 事实；是否进入恢复周期、是否递增手数、当前第几层、是否达到最大总手数或恢复目标，由 recovery 状态机和风控共同决定。

当前第一阶段链路仍是 replay-first，但 recovery 状态与执行前 DTO 已从补丁式策略信号中拆出：

```text
bid/ask ticks
  -> TickRecoveryReplayRunner
  -> RecoveryCanaryGate(replay coverage + hard caps + dry-run gate)
  -> BoundedRecoveryController
  -> RecoveryDecision(open_step / hold / block / close_cycle)
  -> RecoveryExecutionPlan / PositionScalingIntent(open_step only)
  -> RecoveryPreTradeGuard(policy + cycle + intent hard caps)
  -> TradeExecutor.execute_recovery_scaling_intent()
  -> RecoveryExecutionAdapter
  -> risk.ini[recovery_execution_canary] + protective_stop_points
  -> TradingModule.dispatch_operation("trade") -> trade_command_audits(execute_trade + recovery metadata)
  -> RecoveryCycleStateRecord -> recovery_cycle_states(account_key, cycle_id)
  -> TradingFlowTraceReadModel(source_signal_id -> recovery timeline)
  -> replay report(max_step_count / max_total_volume / estimated_pnl / blocked_count)
```

该链路用于验证高频 path-dependent 资金管理能否被现有 tick 数据、bid/ask 成交模型、频率上限与健康门禁承载。默认配置不直接下 demo/live 单：`recovery_execution_canary.enabled=false` 且 `dry_run=true`。进入真实 demo 前必须有实例级 local 显式开启、market recovery canary 显式配置保护性 SL、`RecoveryCanaryGate` 输出 `allowed=true/stage=dry_run_canary`、trace 可审查，并通过 recovery canary runbook。

---

## 2. 装配与启动顺序

### 2.1 构建阶段 `build_app_container()`

当前装配顺序来自 `src/app_runtime/builder.py`：

1. `build_market_layer()`
   - `MarketDataService`
   - `StorageWriter`
   - `BackgroundIngestor`
   - `UnifiedIndicatorManager`
   - `PipelineEventBus`
   - `PipelineTraceRecorder`
2. `build_trading_layer()`
   - `EconomicCalendarService`
   - `TradingModule`
   - `TradingStateStore / Recovery / Alerts`
3. `build_signal_layer()`（ADR-010 后内部新增 environment-aware 策略过滤）
   - 按 `runtime_identity.environment` 过滤策略集合：
     - environment=live → 装配 status ∈ {ACTIVE, ACTIVE_GUARDED}
     - environment=demo → 装配 status ∈ {ACTIVE, ACTIVE_GUARDED, DEMO_VALIDATION}
     - environment 未知 → 装配全集 + WARNING（向后兼容）
   - `SignalModule`（strategies = 过滤后子集）
   - `SignalRuntime`（targets = symbols × tfs × 过滤后策略）
   - `TradeExecutor`
   - `PendingEntryManager`
   - `PositionManager`
   - `ConfidenceCalibrator`
   - `PerformanceTracker`
4. `build_runtime_controls()`
   - runtime mode controller
   - trading state recovery startup reconciliation
5. `build_monitoring_layer()`
6. `build_runtime_read_models()`
7. `build_studio_service_layer()`

> ADR-010（2026-04-25）后 `build_paper_trading_layer()` 已整体删除。原"无摩擦影子交易"角色由 demo-main 实例真实下单替代；装配过滤在 `_filter_strategies_for_environment()` 内完成，下游所有组件天然只看到当前 environment 应该运行的策略集。

### 2.2 FULL 模式启动顺序

真实启动不是直接把所有线程一次性拉起，而是通过 `RuntimeModeController` 驱动 `RuntimeComponentRegistry`：

```text
AppRuntime.start()
  -> RuntimeModeController.start()
     -> storage
     -> performance_warmup
     -> ingestion
     -> pipeline_trace
     -> indicators
          -> EconomicCalendarService
          -> IndicatorEventWriter
          -> IndicatorEventLoop
          -> IndicatorIntrabar
     -> signals
     -> trade_execution
     -> pending_entry
     -> position_manager
```

ADR-010 后 `paper_trading` 组件已从 RuntimeComponentRegistry 删除。

注意：

1. `EconomicCalendarService` 当前不是独立 runtime component，而是挂在 `indicators` 启动栈内部。
2. `TradeExecutor.start()` 只清理生命周期状态，不主动起 worker 线程；真正收到首个信号后才 `_start_worker()`。
3. `executor` 角色实例当前不装配 `SignalRuntime`，只装配本地账户执行栈与 `ExecutionIntentConsumer`。
4. 即使走单账户/本地 listener 适配路径，terminal pipeline 事件也不再是“本地特例”；它们复用与 intent consumer 相同的结果解释合同。

---

## 3. 主链路分层图

## 3.1 行情采集层

```text
[MT5]
  -> [BackgroundIngestor]
       |--> get_quote() -----------------------> MarketDataService.set_quote()
       |--> get_ticks() -----------------------> MarketDataService.extend_ticks()
       |--> get_ohlc() / get_ohlc_from() -----> MarketDataService.set_ohlc_closed()
       |                                         `-> enqueue_ohlc_closed_event()
       |
       |--> get_quote() -----------------------> StorageWriter.enqueue('quotes')
       |--> get_ticks() -----------------------> StorageWriter.enqueue('ticks')
       `--> get_ohlc() ------------------------> StorageWriter.enqueue('ohlc')
```

职责要点：

1. `BackgroundIngestor` 负责节奏控制、增量拉取、回补与 child TF -> parent TF intrabar 合成。
2. `MarketDataService` 是运行时内存事实源，API、指标、信号都应从这里读当前内存状态。
3. `StorageWriter` 不是事实源，它只是异步 durable writer。

## 3.2 内存事实源与事件分发层

`MarketDataService` 当前同时承担三类职责：

1. Quote/Tick/OHLC/IntraBar 缓存；
2. 与持久化查询回填融合；
3. 市场事件分发（`MarketEventBus`）。

状态归属：

| 状态 | 拥有者 | 写入者 |
|---|---|---|
| `_quote_cache` | `MarketDataService` | `BackgroundIngestor` |
| `_tick_cache` | `MarketDataService` | `BackgroundIngestor` |
| `_ohlc_closed_cache` | `MarketDataService` | `BackgroundIngestor` / 指标结果回写 |
| `_intrabar_cache` | `MarketDataService` | `BackgroundIngestor` trigger 合成 |

## 3.3 指标 durable 事件链路

```text
MarketDataService.enqueue_ohlc_closed_event()
  -> MarketEventBus.dispatch_ohlc_closed()
  -> Indicator closed_bar_event_sink
  -> event_write_queue
  -> IndicatorEventWriter
  -> LocalEventStore.publish_events_batch()
  -> events.db
  -> IndicatorEventLoop.claim_next_events()
  -> process_closed_bar_events_batch()
  -> load_confirmed_bars / compute pipeline
  -> write_back_results()
       |--> MarketDataService.update_ohlc_indicators()
       |--> StorageWriter.enqueue('ohlc_indicators')
       `--> publish snapshot
```

这条链路是当前项目里最关键的“系统主通路”之一，因为它决定：

1. 采集到的 confirmed OHLC 是否真的变成 durable indicator event；
2. 指标计算是否能在进程抖动后恢复；
3. 后续信号评估是否有稳定输入。

当前已确认：

1. `IndicatorEventLoop` 已改为真实调用 `claim_next_events()`；
2. `closed-bar -> event_write_queue -> events.db` 的系统集成测试已补齐；
3. 启动后 `event_loop_running=true`。

## 3.4 Intrabar 支链

```text
[child TF close]
  -> BackgroundIngestor._synthesize_parent_intrabar()
  -> MarketDataService.set_intrabar()
  -> MarketEventBus.dispatch_intrabar()
  -> Indicator intrabar queue
  -> Indicator intrabar compute
  -> publish intrabar snapshot
  -> SignalRuntime._on_snapshot(scope=intrabar)
```

当前结论：

1. Intrabar 路径存在，但它是 confirmed 主链路的附属增强，不是系统 readiness 的必要条件。
2. 当前阶段如果只审系统通路，重点看 confirmed；如果审盘中交易能力，再看 intrabar。

---

## 4. 信号与交易扩展链路

## 4.1 信号评估

```text
Indicator snapshot
  -> SignalRuntime._on_snapshot()
  -> warmup / staleness / metadata
  -> confirmed queue (WAL) / intrabar queue (memory)
  -> process_next_event()
  -> SignalFilterChain
  -> regime / strategy evaluate
  -> publish SignalEvent
```

`SignalRuntime` 是信号域状态拥有者，关键状态包括：

1. confirmed 队列与 intrabar 队列；
2. confirmed state machine；
3. filter / no_signal / warmup / drop rate 统计；
4. snapshot listener 广播。

## 4.2 执行、挂单、持仓

```text
[SignalRuntime]
  -> [ExecutionIntentPublisher(main or single-account main)]
  -> [execution_intents]
  -> [ExecutionIntentConsumer(target account)]
  -> [TradeExecutor]
       |--> market / pending decision ---------> [PendingEntryManager] --> [StorageWriter]
       |--> position lifecycle follow-up ------> [PositionManager] -----> [StorageWriter]
       `--> execution audit / outcomes --------> [StorageWriter]
```

需要特别说明的一点：

1. 当前 `confirmed` 与 `intrabar_armed_*` 都已经统一收口到 `execution_intents`，不再保留 “SignalRuntime 直接调用 TradeExecutor” 作为正式 live 主链。
2. `executor` 实例只消费 intent，不生产信号；`main` 实例只生产 signal / intent，不直接持有多账户执行职责。
3. `TradeExecutor` 当前仍是 lazy worker 设计。也就是说，组件已装配、consumer 已可 claim intent，但内部执行线程只会在收到首个可执行事件后启动。
4. 因此 `/health` 里 `trade_executor.running=false`，当前更接近“worker 线程未被唤起”，不等于“执行器未装配”。

这不是系统主链路阻塞项，但它会影响你阅读运行态时的直觉，后续可以单独把 health 语义改成：

- `enabled`
- `listener_attached`
- `worker_running`

而不是只给一个 `running`。

## 4.3 Demo Validation 平行支路（ADR-010 替代 Paper Trading）

```text
[SignalRuntime SignalEvent (deployment.status=demo_validation)]
  -> [ExecutionIntentPublisher（按 runtime_identity.environment 路由）]
       environment="demo" → allows_demo_validation()
       environment="live" → allows_live_execution() (拒)
  -> [demo broker (真实下单 + demo 资金，与 live 共用 11 层风控)]
  -> [TradeExecutor / PositionManager / TradeOutcomeTracker]
  -> [StorageWriter / DB]
```

ADR-010 删除独立 `PaperTradingBridge` / `PaperTradeTracker` 后，"shadow validation"
职责转由 demo environment + `deployment.status=demo_validation` 承接：装配层按
environment 放宽到 `allows_demo_validation()`，publisher 按 environment 路由到 demo
executor，pre-trade 链路与 live 完全一致（11 层风控全跑），唯一区别是 broker
端 demo 资金。详见 §0dd / ADR-010。

## 4.4 Operator Command 控制链

```text
[FastAPI / Trade APIs]
  -> [OperatorCommandService.enqueue()]
  -> [operator_commands]
  -> [OperatorCommandConsumer(target account)]
       |--> RuntimeModeController / TradeControl / Closeout / PendingEntry
       `--> TradingModule operator actions
              -> [trade_command_audits]
              -> [PipelineEventBus(command_submitted / command_completed / command_failed)]
```

当前正式约束：

1. `OperatorCommandService`、`OperatorCommandConsumer` 与 `TradingModule` 现在共用单一 `operator command result` 合同，不再各自补字段或在 consumer 侧做历史结果归一化。
2. `command_submitted / command_completed / command_failed` 三类事件都以同一结果对象为事实源，`response_payload` 的 `accepted / status / action_id / command_id / audit_id / message / effective_state` 语义一致。
3. 这条链路当前仍与 `execute_trade` 普通交易返回值是两套结果合同；后者尚未完成同级别收口，审查时不要把二者混为同一契约。

---

## 5. 经济日历、风控与可观测侧链

## 5.1 经济日历

```text
[Jin10 / Provider]
  -> [EconomicCalendarService]
       |--> StorageWriter.enqueue('economic_calendar')
       |--> StorageWriter.enqueue('economic_calendar_updates')
       |--> RuntimeReadModel / /health
       `--> 交易窗口保护 / 风险判断
```

当前已确认：

1. `session_bucket` / `status` 契约已和 schema 对齐；
2. 启动后经济日历 job 状态能真实返回；
3. 外部 provider 偶发超时仍需视为外部依赖风险。

## 5.2 监控与 trace

```text
[Ingestor] --------------------------\
[MarketDataService] ------------------\
[IndicatorManager] --------------------> [HealthMonitor + MonitoringManager] --> [health_monitor.db]
[SignalRuntime] ----------------------/                                     `-> RuntimeReadModel.health_report()
[EconomicCalendarService] -----------/

[IndicatorManager] ------------------\
[SignalRuntime] ----------------------\
[ExecutionIntentPublisher] -----------+--> [PipelineEventBus] --> [PipelineTraceRecorder] --> [pipeline_trace_events]
[ExecutionIntentConsumer] -----------/
[TradeExecutor] ---------------------/
```

当前已确认：

1. `PipelineTraceRecorder` 生命周期已修正，不再在线程未停时误报停止；
2. listener 注册失败会显式抛错，不再“看似启动、实际没挂上总线”；
3. `TradingFlowTraceReadModel` 是交易主链路唯一业务 trace 投影：`signal_id / trace_id / intent_id / command_id / action_id` 任一正式标识都应能反查同一条 pipeline 链路，`/v1/trade/traces` 列表也按这些 execution 标识过滤；
4. `/v1/trade/traces` 在没有 `from_time / to_time` 且没有 `trace_id / signal_id / intent_id / command_id / action_id` 精确标识时，默认只投影最近 6 小时并返回 `metadata.default_window_applied=true`；历史审计必须显式给时间范围或使用 `by-*` 精确反查，避免高频 trace 历史聚合拖慢运行控制面；
5. readiness 目前纳入：
   - `storage_writer`
   - `ingestion`
   - `market_data_health`
   - `indicator_engine`
   - executor 角色的 `execution_intent_consumer / operator_command_consumer / pending_entry / position_manager / account_risk_state`
6. 启动状态恢复由 `TradingStateRecovery` 拥有：它只比较 MT5 当前持仓与 `TradingStateStore` 中 open position runtime state，并通过 `TradingStateStore.mark_position_missing()` 写入正式 closed state；运行时装配层只编排，不直接改写持仓细节。

---

## 6. 持久化地图

## 6.1 异步写入通道

当前 `StorageWriter` 已注册的 8 个通道：

| 通道 | 作用 | 事实源 |
|---|---|---|
| `ticks` | 高频 tick 落库 | `BackgroundIngestor` |
| `quotes` | quote 落库 | `BackgroundIngestor` |
| `intrabar` | intrabar 合成 bar 落库 | `BackgroundIngestor` |
| `ohlc` | confirmed OHLC 落库 | `BackgroundIngestor` |
| `ohlc_indicators` | 指标结果回写到 OHLC | `UnifiedIndicatorManager` |
| `economic_calendar` | 日历事实事件 | `EconomicCalendarService` |
| `economic_calendar_updates` | 日历快照更新 | `EconomicCalendarService` |
| `signal_preview` | 预览/盘中信号事件 | 信号链路 |

## 6.2 本地运行时文件

当前根目录 `data/` 下的运行态文件职责：

| 路径 | 作用 |
|---|---|
| `data/logs/mt5services.log` | 主运行日志 |
| `data/logs/errors.log` | warning/error 独立日志 |
| `data/dlq/*.jsonl` | StorageWriter 刷写失败批次 |
| `data/events.db` | 指标 durable event queue |
| `data/health_monitor.db` | 监控/告警轻量状态 |
| `data/calibrator_cache.json` | calibrator warm start 缓存 |
| `data/artifacts/*` | 手工回测、压测、启动排查等运行产物（非主链路状态） |

关键约束：

1. `src/` 下不应再落任何运行日志或运行时临时文件；
2. 当前日志路径已修正为锚定项目根目录 `data/`；
3. 仓库根目录不再保留平级 `runtime/`，手工执行产物统一收口到 `data/artifacts/`。

---

## 7. 状态所有权矩阵

这部分是审查项目状态时最重要的“谁拥有状态”的答案。

| 模块 | 拥有的核心状态 | 谁能写 | 谁只读 |
|---|---|---|---|
| `MarketDataService` | quote/tick/closed_ohlc/intrabar cache | `BackgroundIngestor` / indicator 回写 | API / indicators / signals |
| `StorageWriter` | 各通道 queue/pending/DLQ 状态 | 上游各模块 enqueue | health/readmodel |
| `LocalEventStore(events.db)` | indicator durable events | indicator writer loop | indicator event loop / monitoring |
| `UnifiedIndicatorManager.state` | event/intrabar queue、results、listeners、scope stats | indicator runtime | readmodel/monitoring |
| `SignalRuntime` | confirmed/intrabar queues、state machine、drop/filter/no_signal 统计 | signal orchestration | readmodel / monitoring |
| `TradeExecutor` | execution queue、circuit breaker、execution stats | executor | readmodel / monitoring |
| `PendingEntryManager` | active pending entries | pending manager | readmodel / API |
| `PositionManager` | tracked positions / reconcile state | position manager | readmodel / API |
| `TradingStateRecovery` | 启动/恢复期状态对账决策 | trading state recovery | runtime lifecycle / logs |
| `TradingStateStore` | pending/position runtime state records | trading state store | readmodel / recovery / manager |
| `EconomicCalendarService` | job status / provider status / refresh state | economic calendar runtime | readmodel / API / risk |
| `HealthMonitor` | metrics ring buffer / alerts | monitoring manager | runtime readmodel / API |
| `RuntimeReadModel` | 无自有业务状态，只做投影 | 无 | API / Studio |

审查原则：

1. 看数据正确性，先找“状态拥有者”，不要直接看展示层。
2. 看链路是否断，先看拥有者状态，再看读模型是否同步。
3. `StorageWriter` 和 `RuntimeReadModel` 都不是业务事实源，不要把它们当主状态真相。

---

## 8. 当前项目状态总结

### 8.1 已验证通的链路

当前已能确认以下链路处于“可运行、可观测、契约基本对齐”的状态：

1. 启动装配 -> runtime mode -> 组件拉起；
2. MT5 采集 -> `MarketDataService` 缓存；
3. confirmed OHLC -> closed-bar event -> `events.db` durable queue；
4. indicator event loop -> 指标结果回写；
5. `confirmed_buy/sell` 与 `intrabar_armed_*` -> `execution_intents` -> `ExecutionIntentConsumer` -> `TradeExecutor` 已形成统一执行主链；
6. `/health` 与 `/v1/monitoring/health/ready` 能反映系统主链路；
7. 日志、DLQ、health DB 都已落在根目录 `data/`；
8. 经济日历刷新与状态投影可工作。

### 8.2 当前不是阻塞，但要带着看的点

1. `TradeExecutor` 在启动后默认 `worker_running=false`，因为它是懒启动模型。  
   这不是启动失败，但 health 口径容易误导。

2. 休盘期间 `market_data data latency critical` 会出现。  
   这表示当前没有新鲜行情流入，不是“采集线程停了”。

3. 经济日历 provider 仍可能超时。  
   这不影响本地架构链路，但会影响外部数据完备性。

### 8.3 还没有被当前验证覆盖到的部分

这次验证还不等于“实盘绝对安全”，以下仍需在开盘窗口做下一阶段验证：

1. 持续 live feed 下的长时间稳定性；
2. 信号 -> 交易执行 -> 挂单/持仓/结果追踪 的真实业务闭环；
3. `TradeExecutor` health 语义与运行模式/懒启动语义的统一；
4. Paper trading 与真实交易结果的偏差对照。

---

## 9. 建议你如何使用这份图

如果你要全面审视当前项目状态，建议按下面顺序读：

1. 先看本文第 1 节和第 7 节：确认模块、数据源、状态拥有者。
2. 再看第 3 节和第 5 节：确认主链路和侧链是否一致。
3. 最后对照：
   - `docs/codebase-review.md`
   - `docs/architecture.md`
   - `docs/design/signals-dataflow-overview.md`
   - `docs/design/intrabar-data-flow.md`

如果下一步你要我继续，我建议直接做两件事之一：

1. 基于这份图，把“哪些状态点要做可视化面板”收成一版 dashboard 设计。
2. 等黄金开盘后，按这份图做一轮 live feed canary，把“系统通”和“交易通”分开验。
