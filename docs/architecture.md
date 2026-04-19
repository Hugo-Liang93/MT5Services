# 系统架构参考

> 合并自原 `docs/architecture/` 下 4 个文件（overview / data-flow / module-boundaries / module-layout）。
> 更新日期：2026-04-12

---

## 0. 当前审查入口

当前代码库风险台账集中在 **`docs/codebase-review.md`**。涉及架构、策略、性能、生命周期、配置漂移的整改，应先对照该文档确认：

- 全量文档导航与分层规则见：**`docs/README.md`**。
- 全量运行时数据流与当前项目状态总览见：**`docs/design/full-runtime-dataflow.md`**。
- 信号链路的职责边界与真实数据流详见：**`docs/design/signals-dataflow-overview.md`**。
- 统一启动入口与 CLI 映射详见：**`docs/design/entrypoint-map.md`**。

- 是否仍受 `config/*.local.ini` 覆盖影响；
- 是否会触碰信号 → 风控 → 交易主链路；
- 是否需要同步 ADR 或运行文档；
- 是否会增加 API/Studio/readmodel 对私有实现的依赖。

### 0.1 本文边界

`architecture.md` 只负责：

1. 系统分层
2. 模块边界
3. 依赖方向
4. 文档入口

运行时验证、启动结论、日志路径、健康探针、链路状态不在本文重复展开，统一以下列文档为准：

1. `docs/codebase-review.md`
2. `docs/design/full-runtime-dataflow.md`
3. `docs/design/entrypoint-map.md`

---

## 1. 系统定位与分层

FastAPI 量化交易运行时，核心链路：行情采集 → 指标计算 → 单策略信号评估 → execution intent 交付 → 风控准入 → 交易执行 → 持仓管理。

### 1.1 领域层

| 目录 | 职责 |
|------|------|
| `src/market/` | 运行时内存行情缓存（唯一事实源）|
| `src/ingestion/` | 从 MT5 拉取 quote/tick/OHLC/intrabar，写入缓存 + 持久化队列 |
| `src/indicators/` | 收盘与盘中事件处理，生成指标快照 |
| `src/signals/` | 策略注册、单策略评估、状态机、信号持久化、execution intent 发布 |
| `src/trading/` | execution intent 消费、下单执行、价格确认入场、持仓跟踪、结果追踪 |
| `src/calendar/` | 经济日历同步与交易窗口保护 |
| `src/persistence/` | 8 通道异步 StorageWriter + TimescaleDB 仓储 + Retention Policy |
| `src/monitoring/` | 内存环形缓冲健康指标 + SQLite 告警 + Pipeline Trace |
| `src/risk/` | 风控规则、保证金守卫 |
| `src/readmodels/decision.py` | 上下文融合决策摘要（`build_decision_brief`） |
| `src/market_structure/` | 市场结构分析 |
| `src/backtesting/` | 回测引擎 + 参数优化 + Paper Trading |
| `src/research/` | 信号挖掘：指标预测力分析 + 阈值扫描 + Regime 亲和度调优 |

`src/trading/` 当前再细分为 4 条主子域：

- `src/trading/application/`：交易应用服务、审计、幂等回放、MT5 交易业务服务
- `src/trading/commands/`：operator command 提交、消费、结果合同
- `src/trading/execution/`：执行器、pre-trade pipeline、执行门禁、执行健康
- `src/trading/runtime/`：账户注册表与后台线程生命周期基础设施

### 1.2 运行时装配层（src/app_runtime/）

- `container.py` — 纯组件容器，不含业务逻辑
- `factories/` — 各领域组件工厂
- `builder.py` — 装配 AppContainer，不启动线程
- `runtime.py` — 启动/关闭/监控注册/生命周期
- `mode_controller.py` — 4 种运行模式：FULL/OBSERVE/RISK_OFF/INGEST_ONLY

### 1.3 读模型与展示层

- `src/readmodels/` — 统一运行时投影（Dashboard/Health/Trading/Signal 摘要）
- `src/studio/` — Studio 16 Agent 映射与事件桥接

### 1.4 API 层（src/api/）

- 只负责 HTTP 路由、中间件、Schema、DI 适配
- 所有业务接口 `/v1/...`，根路径只保留 `/health`
- 12 个子域路由包（trade/signal/market/indicators/monitoring/admin/economic/decision/backtest/paper-trading/studio/account）

---

## 2. 启动流程

### 构建阶段 — `build_app_container()`

```text
MarketDataService → StorageWriter → BackgroundIngestor → UnifiedIndicatorManager
→ EconomicCalendarService → TradingModule → SignalModule + SignalRuntime + ExecutionIntent delivery
→ TradeExecutor + PendingEntryManager + PositionManager
→ HealthMonitor + MonitoringManager → RuntimeReadModel → StudioService
```

### 运行阶段 — `AppRuntime.start()`

```text
AppRuntime.start()
  -> RuntimeModeController.start()
  -> RuntimeComponentRegistry.apply_mode(FULL)
     1. storage
     2. performance_warmup
     3. ingestion
     4. pipeline_trace
     5. indicators（内部同时启动 EconomicCalendarService）
     6. signals
     7. trade_execution
     8. pending_entry
     9. position_manager
    10. paper_trading
```

关闭顺序按 registry 反向执行，详见 `docs/design/full-runtime-dataflow.md`。

---

## 3. 数据流

### 3.1 主链路

```text
MT5 → BackgroundIngestor → MarketDataService(内存缓存)
  ├─ StorageWriter(8通道异步) → TimescaleDB
  ├─ OHLC 收盘事件 → IndicatorManager → 指标快照
  │    └─ SignalRuntime(confirmed优先+intrabar) → 单策略评估 → 状态机/发布
  │         └─ ExecutionIntentPublisher → execution_intents
  │              → ExecutionIntentConsumer → TradeExecutor
  │              → PendingEntryManager → MT5 下单
  └─ 子 TF close → 合成父 TF intrabar bar → set_intrabar() (trigger 模式)
       └─ 同上 intrabar 管道 → IntrabarTradeCoordinator → intrabar_armed
            → ExecutionIntentPublisher → execution_intents → worker 执行
```

### 3.2 持久化链路

**TimescaleDB**（26 表，14 hypertable，三级 retention policy）：
- L1 审计（730 天）：trade_outcomes, trade_command_audits
- L2 业务（180-365 天）：ohlc, signal_events, signal_outcomes, auto_executions
- L3 高频（7-90 天）：ticks, quotes, ohlc_intrabar, signal_preview_events, pipeline_trace_events

**StorageWriter 8 通道**：ticks / quotes / intrabar / ohlc / ohlc_indicators / economic_calendar / economic_calendar_updates / signal_preview

**本地 SQLite**：
- `events.db` — OHLC 收盘事件队列（7 天保留）
- `signal_queue.db` — confirmed 信号持久化队列（消费即删）
- `health_alerts.db` — 告警历史（轻量）

**内存**：
- MetricsStore（deque 环形缓冲）— 健康指标，重启后重新采集

**日志**：
- `data/logs/mt5services.log` — RotatingFileHandler 100MB×10
- `data/logs/errors.log` — WARNING+ 级别独立文件

### 3.3 前端供给

```text
运行时组件 → RuntimeReadModel → /v1/admin/* + /v1/monitoring/*
运行时组件 → StudioService → /v1/studio/*（16 Agent SSE）
pipeline_trace_events + signal/trade → TradingFlowTraceReadModel → /v1/trade/trace/*
```

---

## 4. 模块边界

### 关键约束

| 模块 | 可以做 | 不可以做 |
|------|-------|---------|
| `app_runtime` | 创建/装配/启动/关闭组件 | HTTP 路由、前端拼装 |
| `api` | HTTP 适配、DI、响应封装 | 装配组件、重复聚合、读私有属性 |
| `readmodels` | 运行时状态投影 | 启动线程、执行交易、写库 |
| `studio` | 展示层适配 | 直连私有属性 |
| `monitoring` | 健康检查、告警 | API 层补数据 |

### 依赖方向

```text
允许：api → deps → container | api → readmodels | studio → 只读适配
禁止：app_runtime → api | readmodels → api | api → 领域私有实现
```

---

## 5. 模块布局规范

### 总体原则

1. 先按领域拆目录，再按职责拆子包
2. 外部模块优先依赖子包公开接口
3. 包根目录只保留跨子域共享对象
4. 新增文件先判断是否属于现有子包职责
5. 禁止空壳转发文件

### 5.1 src/market/ — 运行时行情缓存

| 文件 | 职责 | 边界 |
|------|------|------|
| `service.py` | MarketDataService：**运行时数据唯一事实源**。持有 Tick/Quote/OHLC/Intrabar 状态，RLock 线程安全 | 只持有数据和通知。不做指标计算、不做信号评估、不做持久化 |
| `event_bus.py` | MarketEventBus：事件订阅/分发 | 只做事件路由。不处理事件内容 |

**写入者**：仅 `BackgroundIngestor`。**读取者**：API 路由、IndicatorManager、SignalRuntime。

### 5.2 src/ingestion/ — 数据采集

| 文件 | 职责 | 边界 |
|------|------|------|
| `ingestor.py` | BackgroundIngestor：主循环从 MT5 拉取 quote/tick/OHLC confirmed bars；intrabar 由子 TF close 事件驱动合成（Trigger 模式，唯一路径） | 只做数据拉取和分发。不做指标计算、不做信号评估 |

采集节奏：`poll_interval`（tick/quote）、`ohlc_interval`（收盘 bar），两者独立节流。Intrabar 不走轮询，由 Trigger 模式驱动。
Intrabar trigger：`signal.ini [intrabar_trading.trigger]` 配置 parent_tf → trigger_tf 映射，子 TF bar close 时 `_synthesize_parent_intrabar()` 从内存 confirmed bars 合成父 TF 当前 bar，注入 `set_intrabar()` 管道。`_ingest_ohlc()` 中未收盘 bar 直接跳过。

### 5.3 src/indicators/ — 指标计算

| 文件/子包 | 职责 | 边界 |
|----------|------|------|
| `manager.py` | UnifiedIndicatorManager：统一编排 confirmed + intrabar 两条指标链路 | 协调器。不实现具体指标算法 |
| `bar_event_handler.py` | 收盘 K 线事件批处理（从 manager 拆分） | 只处理 confirmed bar close 事件 |
| `pipeline_runner.py` | 指标管道运行逻辑 | 调用 pipeline.compute()，不定义指标 |
| `result_store.py` | 指标结果存储与规范化 + delta 计算 | 只做存储和 N-bar 变化率（`_d3`/`_d5`） |
| `snapshot_publisher.py` | 快照发布与去重 | 只做发布决策。相同快照不重复发布 |
| `delta_metrics.py` | N-bar 变化率纯函数 | 纯计算，无状态 |
| `core/` | 具体指标实现函数（`func(bars, params) → dict`） | 每个指标一个函数。不依赖运行时组件 |
| `engine/` | OptimizedPipeline：DAG 调度 + standard/incremental 双模式 | 负责执行顺序和缓存。不定义指标 |
| `cache/` | 指标缓存策略 | 不做计算 |
| `monitoring/` | 指标健康检查 | 不参与计算链路 |

### 5.4 src/signals/ — 信号评估与决策

根目录共享对象：

| 文件 | 职责 |
|------|------|
| `service.py` | SignalModule：策略注册 + `evaluate()` 统一入口 |
| `models.py` | SignalEvent / SignalContext / SignalDecision 数据类 |
| `confidence.py` | 置信度管线纯函数（`apply_intrabar_decay`） |

子包：

| 子包 | 职责 | 边界 |
|------|------|------|
| `orchestration/` | SignalRuntime 协调器 + 协作者。管理事件队列、状态机、投票、regime 亲和度、IntrabarTradeCoordinator（bar 计数稳定性 → intrabar_armed 交易信号） | 只做信号流编排。不实现策略逻辑、不下单 |
| `strategies/` | 全部策略实现 + 策略注册/目录/参数解析 | 只做信号判断（`evaluate() → SignalDecision`）。不访问交易状态 |
| `evaluation/` | Regime 检测、Calibrator 校准、PerformanceTracker 绩效追踪 | 只做评估和统计。不做信号发布 |
| `execution/` | SignalFilterChain（时段/点差/经济/波动率过滤） | 只做过滤决策（`should_evaluate() → bool`）。不做评估 |
| `tracking/` | SignalRepository（信号持久化与查询） | 只做读写 DB |
| `analytics/` | DiagnosticsEngine、插件注册 | 只做诊断分析，不影响信号流 |
| `contracts/` | 交易时段常量 | 只定义常量 |

`orchestration/` 内部协作者：

| 文件 | 职责 | 性质 |
|------|------|------|
| `runtime.py` | 门面：生命周期 + 队列管理 | 薄协调器 |
| `runtime_evaluator.py` | 策略评估 + confidence 调整 + 信号发布 | 核心逻辑 |
| `runtime_processing.py` | 事件出队 + filter/regime + 单事件处理 | 流程编排 |
| `runtime_warmup.py` | warmup 屏障：backfilling/staleness/指标完整性检查 | 纯逻辑 |
| `runtime_metadata.py` | snapshot metadata 组装：spread/bar_progress/trace_id | 纯函数 |
| `runtime_recovery.py` | confirmed 状态还原 | 启动恢复 |
| `state_machine.py` | confirmed 状态转换（idle / confirmed_* / confirmed_cancelled） | 纯逻辑 |
| `htf_resolver.py` | HTF 配置解析、指标查询 | 纯函数 |
| `wal_queue.py` | WAL 持久化信号队列 | 持久化 |
| `policy.py` | SignalPolicy + RuntimeSignalState 配置对象 | 数据类 |

### 5.5 src/trading/ — 交易执行与持仓管理

根目录共享对象：`models.py`（领域模型）、`ports.py`（8 个 Protocol 跨边界合约）、`registry.py`（账户注册）、`trading_service.py`（底层 MT5 交易适配）

| 子包 | 职责 | 边界 | 禁止 |
|------|------|------|------|
| `application/` | TradingModule 聚合根 + CQRS 命令/查询分离 + 审计 + 信号执行 | 业务编排层。协调下层子包 | 不直接调 MT5 API |
| `execution/` | TradeExecutor 协调器 + ExecutionGate 准入（含 intrabar 策略白名单）+ RegimeSizing 仓位 + IntrabarTradeGuard（盘中去重 + confirmed 协调）+ 挂单提交 + 成本估算 | 下单决策和参数计算 | 不管理持仓生命周期 |
| `pending/` | PendingOrderManager：挂单追踪、过期、成交衔接 | 挂单状态管理 | 不做开仓决策 |
| `positions/` | PositionManager：持仓监控、止损跟踪、breakeven、日终平仓 | 仓位生命周期管理 | 不做开仓 |
| `closeout/` | ExposureCloseoutService：风险收口（平仓 + 撤单） | 只做紧急/日终平仓 | 不做常规交易 |
| `tracking/` | SignalQualityTracker + TradeOutcomeTracker：两条独立评估链路 | 只做结果追踪和回调 | 不做交易决策 |
| `state/` | 交易状态持久化 + 恢复 | 只做读写状态 | 不做业务逻辑 |

### 5.6 src/persistence/ — 持久化

| 文件/子包 | 职责 | 边界 |
|----------|------|------|
| `storage_writer.py` | StorageWriter：8 通道异步队列写入 | 只做入队和批量刷写。不做查询 |
| `db.py` | TimescaleWriter：连接池 + 仓储工厂 | 只做连接管理。业务逻辑在仓储层 |
| `retention.py` | 三级 retention policy（730/365/90 天） | 只做定期清理 |
| `validator.py` | Schema 完整性校验 | 只做检查 |
| `repositories/` | 7 个领域仓储（market/signal/trade/economic/runtime/backtest/pipeline_trace） | 只做 SQL 读写。不做业务逻辑 |
| `schema/` | DDL 脚本 | 只定义表结构 |

### 5.7 src/risk/ — 风控

| 文件 | 职责 | 边界 |
|------|------|------|
| `service.py` | RiskService：规则链执行入口 | 编排规则，不实现具体规则 |
| `rules.py` | 5 个风控规则（DailyLossLimit / AccountSnapshot / MarginAvailability / TradeFrequency / BrokerConstraint） | 每个规则独立判断 pass/block |
| `margin_guard.py` | 保证金动态检查 | 只做保证金计算 |
| `ports.py` | Protocol 定义 | 接口合约 |
| `runtime.py` | 运行时风控状态 | 状态追踪 |

### 5.8 src/config/ — 配置加载

| 文件 | 职责 | 边界 |
|------|------|------|
| `centralized.py` | CentralizedConfig + FileConfigManager 集成。所有 `get_*_config()` 函数入口 | 统一入口。不做业务逻辑 |
| `signal.py` | SignalConfig：信号模块专用配置（933 行 INI 解析） | 只解析 signal.ini |
| `mt5.py` | MT5Settings：终端连接配置 + 多账户支持 | 只解析 mt5.ini |
| `database.py` | DBSettings：PostgreSQL 连接参数 | 只解析 db.ini |
| `indicator_config.py` | ConfigManager + ConfigLoader：indicators.json 解析 | 只解析指标配置 |
| `file_manager.py` | FileConfigManager：INI 热重载 + 变更通知 | 文件监控基础设施 |
| `utils.py` | `load_config_with_base()`：分层 INI 合并（base → local → target → target.local） | 通用工具 |
| `models/` | Pydantic 配置模型定义 | 只定义数据结构 |

### 5.9 src/app_runtime/ — 应用运行时装配

| 文件 | 职责 | 边界 |
|------|------|------|
| `container.py` | AppContainer：纯组件持有（flat dataclass） | 只持有引用。不含业务逻辑、不启动线程 |
| `builder.py` | `build_app_container()`：构建全部组件、连接依赖 | 只做装配。不启动后台线程 |
| `runtime.py` | AppRuntime：`start()`/`stop()` 生命周期管理 | 只做启动/关闭编排 |
| `mode_controller.py` | 4 种运行模式切换 | 只做模式状态管理 |
| `lifecycle.py` | RuntimeManagedComponent Protocol | 只定义接口 |
| `factories/` | 各领域组件工厂函数（market/storage/trading/signals/indicators） | 每个工厂只构建自己领域的组件 |

### 5.10 src/backtesting/ — 回测与优化

| 子包 | 职责 | 边界 |
|------|------|------|
| `engine/` | BacktestEngine：逐 bar 回放 + PortfolioTracker 模拟持仓 | 复用生产 SignalModule/Pipeline。不重新实现策略逻辑 |
| `filtering/` | BacktestFilterSimulator：包装生产 SignalFilterChain，注入历史时间 | 不重新实现过滤逻辑 |
| `analysis/` | 统计分析（metrics/correlation/monte_carlo/report） | 纯函数，不依赖运行时组件 |
| `optimization/` | ParameterOptimizer + WalkForwardValidator + RecommendationEngine | 编排多次 BacktestEngine 运行 |
| `data/` | HistoricalDataLoader + DataCache（进程级 FIFO 缓存） | 只做数据加载和缓存 |
| `paper_trading/` | PaperTradingBridge + 独立 portfolio/tracker | 影子交易，不触及真实账户 |
| 根目录 | `models.py`（BacktestConfig 8 个嵌套子配置）、`config.py`（backtest.ini 加载）、`component_factory.py`（CLI/API 共享组件构建） | |

### 5.11 其他领域包

| 包 | 职责 | 边界 |
|---|------|------|
| `src/calendar/` | EconomicCalendarService + TradeGuard（高风险时段阻止交易） | 只做日历同步和时段判断 |
| `src/clients/` | 外部 API 客户端（MT5 market/account/trading + 经济日历 4 源） | 只做 API 调用封装。不含业务逻辑 |
| `src/readmodels/decision.py` | `build_decision_brief()`：上下文融合（regime+indicators+signals+positions → 决策摘要） | 只读聚合，不做交易决策 |
| `src/market_structure/` | MarketStructureAnalyzer：支撑/阻力/趋势线分析 | 纯分析，不影响交易链路 |
| `src/readmodels/` | RuntimeReadModel + TradingFlowTraceReadModel：运行时状态只读投影 | 只读。不启动线程、不写库 |
| `src/studio/` | StudioService：16 Agent 注册 + SSE 事件桥接 + 纯映射函数 | 展示层适配。不直连私有属性 |
| `src/monitoring/` | `health/`（HealthMonitor + MetricsStore 环形缓冲 + checks + reporting）`pipeline/`（PipelineEventBus + 12 种事件 + TraceRecorder） | 只做观测。不影响业务链路 |
| `src/utils/` | 通用工具（timezone/sqlite_conn/event_store/memory_manager） | 无状态工具。不依赖领域模块 |
| `src/api/` | 12 个子域路由包，每个：`<domain>.py`（组合根）+ `<domain>_routes/`（路由+view_models）| 只做 HTTP 协议适配。业务逻辑下沉到服务层或 readmodels |
| `src/ops/` | `cli/`（22 个运维 CLI：backtest_runner / diagnose_no_trades / mining_runner / aggression_search / walkforward_runner / health_check 等）+ `stress/`（压测：intent_latency_probe / replay_intrabar / storage_saturation）+ `mt5_session_gate.py` | 运维工具入口。调用领域包但不被领域包调用（终态工具）。**动手写 scratch/ 诊断脚本前先查 `ls src/ops/cli/`，避免重复造轮子**——详见 CLAUDE.md §SOP "工具使用准则" |

---

## 6. 新增文件归位规则

1. 是否属于现有领域目录？→ 放入对应目录
2. 是否属于现有子包职责？→ 放入对应子包
3. 是否只是现有文件过大？→ 拆到同子包内新文件
4. 只有形成稳定新职责时才新增子包

**不应新增子包的情况**：只有一个 helper、只是命名好看、无稳定对外接口。

**跨包依赖方向**：
```
允许：api → deps → container → 任何领域包
允许：app_runtime → 任何领域包（构建时）
允许：readmodels → 领域包（只读）
禁止：领域包 → api（反向依赖）
禁止：领域包 → app_runtime（反向依赖）
禁止：utils → 领域包（工具层不依赖业务）
```

---

## 7. 专题文档入口

下列主题已经从系统总览中收口到专题文档，避免同一语义在 `architecture.md`、设计稿和运行时文档中重复维护：

| 主题 | 当前边界 | 详细文档 |
|------|------|------|
| signals 运行时链路、状态流转、观测点 | 由 signals 域运行时真相文档维护 | `docs/design/signals-dataflow-overview.md` |
| intrabar 子链路 | 由 intrabar 专题维护 | `docs/design/intrabar-data-flow.md` |
| 策略开发规范、regime、单策略主线、TF 参数 | 由 signals 领域设计文档维护 | `docs/signal-system.md` |
| PendingEntry、仓位管理、风控增强方案 | 由 trading/risk 设计文档维护 | `docs/design/pending-entry.md`、`docs/design/r-based-position-management.md`、`docs/design/risk-enhancement.md` |
| 回测、研究、参数规划 | 由研究/规划文档维护 | `docs/research-system.md`、`docs/design/next-plan.md` |

本文只保留跨模块视角，不再重复展开这些专题内部的参数表、算法细节和历史方案。

---

## 8. 架构决策记录（ADR）

已确定的设计决策集中管理在 **`docs/design/adr.md`**。

**规则**：修改涉及 ADR 的组件前，先读 `docs/design/adr.md` 对应条目了解上下文。评估后可变更，但须更新 ADR。

当前 ADR 索引：ADR-001（PipelineEventBus 同步 dispatch）、ADR-002（SignalRuntime 纯函数提取）、ADR-003（MetadataKey 常量化）、ADR-004（组件生命周期安全契约）、ADR-005（后台线程 join 超时后的线程引用保留）、ADR-006（跨模块边界禁止读写私有属性）。

## 9. 扩展契约（新增组件必须满足 5 个公共端口）

新增策略/指标/回测模块进入主链路前，必须同时满足以下 5 个公共端口，否则不允许上线：

1. **能力声明端口**  
   - 以 `SignalModule.strategy_capability_catalog()` 与 `SignalPolicy.strategy_capability_contract()` 为统一输入口。  
   - `SignalRuntime` 与 `BacktestEngine` 必须直接消费 `strategy_capability_catalog()`，禁止 `getattr` 探测、禁止 legacy 能力回退。  
   - 至少提供：  
   - 需能被 `SignalRuntime` / 回测读取同一份：  
     `name / valid_scopes / needed_indicators / needs_intrabar / needs_htf / regime_affinity / htf_requirements`。
   - 扩展项：`SignalPolicy.get_warmup_required_indicators()` 作为统一 warmup 指标口，禁止运行时使用兜底兼容回退。
   - 强制对账：新增 `admin/strategies/capability-reconciliation` 与 service 的对账口，确保 module/policy 入链前一致。
2. **状态快照端口**  
   - 提供可读 `status/diagnostics` 视图；上层通过公开接口查询，不直接读取私有字段。
   - 信号链路需包含 `strategy_capability_execution_plan`（`configured/scheduled/filtered`）以审查调度职责边界。
3. **调度可观测端口**  
   - 暴露关键治理点（能力门控、过滤通过率/阻断率、投票命中、丢弃率）并可用于 runtime/回测对账。
   - 回测结果应输出同语义 `strategy_capability_execution_plan`，用于与 runtime 快照直接比对。
   - `runtime/backtest` 的 execution plan 生成逻辑应复用同一构建器（`src/signals/contracts/execution_plan.py`），禁止双份实现。
   - 建议统一通过 `GET /admin/strategies/capability-execution-plan` 做 module/runtime/backtest 三方对账审查。
4. **失败可回退端口**  
   - 能力缺失或契约冲突必须 fail-fast（抛错 + 统一状态/告警通道反馈），不允许静默走“默认兼容支路”掩盖错误。
5. **回放一致端口**  
   - 回测与实盘必须消费同一能力声明（`strategy_capability_catalog`）并共享同类策略筛选语义，避免行为分歧。

该章节是阶段 B 的验收红线：新增能力只改声明与配置，新增逻辑应从公开端口接入，不新增私有分支。
