# 系统架构参考

> 合并自原 `docs/architecture/` 下 4 个文件（overview / data-flow / module-boundaries / module-layout）。
> 更新日期：2026-04-05

---

## 1. 系统定位与分层

FastAPI 量化交易运行时，核心链路：行情采集 → 指标计算 → 信号评估 → 风控准入 → 交易执行 → 持仓管理。

### 1.1 领域层

| 目录 | 职责 |
|------|------|
| `src/market/` | 运行时内存行情缓存（唯一事实源）|
| `src/ingestion/` | 从 MT5 拉取 quote/tick/OHLC/intrabar，写入缓存 + 持久化队列 |
| `src/indicators/` | 收盘与盘中事件处理，生成指标快照 |
| `src/signals/` | 策略注册、信号评估、投票、状态机、信号持久化 |
| `src/trading/` | 下单执行、价格确认入场、持仓跟踪、结果追踪 |
| `src/calendar/` | 经济日历同步与交易窗口保护 |
| `src/persistence/` | 8 通道异步 StorageWriter + TimescaleDB 仓储 + Retention Policy |
| `src/monitoring/` | 内存环形缓冲健康指标 + SQLite 告警 + Pipeline Trace |
| `src/risk/` | 风控规则、保证金守卫 |
| `src/decision/` | 上下文融合决策引擎 |
| `src/market_structure/` | 市场结构分析 |
| `src/backtesting/` | 回测引擎 + 参数优化 + Paper Trading |

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
→ EconomicCalendarService → TradingModule → SignalModule + SignalRuntime + TradeExecutor
→ HealthMonitor + MonitoringManager → RuntimeReadModel → StudioService
```

### 运行阶段 — `AppRuntime.start()`

```text
1. StorageWriter（含 init_schema + retention_policies）
2. UnifiedIndicatorManager
3. BackgroundIngestor
4. EconomicCalendarService
5. SignalRuntime
6. ConfidenceCalibrator + PerformanceTracker warmup
7. PendingEntryManager
8. PositionManager
9. MonitoringManager
```

关闭顺序反向执行。

---

## 3. 数据流

### 3.1 主链路

```text
MT5 → BackgroundIngestor → MarketDataService(内存缓存)
  ├─ StorageWriter(8通道异步) → TimescaleDB
  └─ OHLC 收盘事件 → IndicatorManager → 指标快照
       └─ SignalRuntime(confirmed优先+intrabar) → 策略评估 → VotingEngine
            └─ TradeExecutor → PendingEntryManager → MT5 下单
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
| `ingestor.py` | BackgroundIngestor：主轮询循环，从 MT5 拉取 quote/tick/OHLC/intrabar | 只做数据拉取和分发。不做指标计算、不做信号评估 |

采集节奏：`poll_interval`（tick/quote）、`ohlc_interval`（收盘 bar）、`intrabar_interval`（盘中快照），三者独立节流。

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
| `confidence.py` | 置信度管线纯函数（`apply_htf_alignment`、`apply_intrabar_decay`） |

子包：

| 子包 | 职责 | 边界 |
|------|------|------|
| `orchestration/` | SignalRuntime 协调器 + 协作者。管理事件队列、状态机、投票、regime 亲和度 | 只做信号流编排。不实现策略逻辑、不下单 |
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
| `runtime_recovery.py` | confirmed/preview 状态还原 | 启动恢复 |
| `state_machine.py` | preview → armed → confirmed 状态转换 | 纯逻辑 |
| `vote_processor.py` | 投票处理：fusion/emit/process_voting | 纯函数 |
| `htf_resolver.py` | HTF 配置解析、指标查询、对齐乘数计算 | 纯函数 |
| `affinity.py` | Regime 亲和度计算 + 快速拒绝检查 | 纯函数 |
| `policy.py` | SignalPolicy + VotingGroupConfig 配置对象 | 数据类 |
| `voting.py` | StrategyVotingEngine：多策略加权投票 | 纯逻辑 |

### 5.5 src/trading/ — 交易执行与持仓管理

根目录共享对象：`models.py`（领域模型）、`ports.py`（8 个 Protocol 跨边界合约）、`registry.py`（账户注册）、`trading_service.py`（底层 MT5 交易适配）

| 子包 | 职责 | 边界 | 禁止 |
|------|------|------|------|
| `application/` | TradingModule 聚合根 + CQRS 命令/查询分离 + 审计 + 信号执行 | 业务编排层。协调下层子包 | 不直接调 MT5 API |
| `execution/` | TradeExecutor 协调器 + ExecutionGate 准入 + RegimeSizing 仓位 + 挂单提交 + 成本估算 | 下单决策和参数计算 | 不管理持仓生命周期 |
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
| `src/decision/` | `build_decision_brief()`：上下文融合（regime+indicators+signals+positions → 决策摘要） | 只读聚合，不做交易决策 |
| `src/market_structure/` | MarketStructureAnalyzer：支撑/阻力/趋势线分析 | 纯分析，不影响交易链路 |
| `src/readmodels/` | RuntimeReadModel + TradingFlowTraceReadModel：运行时状态只读投影 | 只读。不启动线程、不写库 |
| `src/studio/` | StudioService：16 Agent 注册 + SSE 事件桥接 + 纯映射函数 | 展示层适配。不直连私有属性 |
| `src/monitoring/` | `health/`（HealthMonitor + MetricsStore 环形缓冲 + checks + reporting）`pipeline/`（PipelineEventBus + 12 种事件 + TraceRecorder） | 只做观测。不影响业务链路 |
| `src/utils/` | 通用工具（timezone/sqlite_conn/event_store/memory_manager） | 无状态工具。不依赖领域模块 |
| `src/api/` | 12 个子域路由包，每个：`<domain>.py`（组合根）+ `<domain>_routes/`（路由+view_models）| 只做 HTTP 协议适配。业务逻辑下沉到服务层或 readmodels |

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

## 7. 策略 Category 分类与执行影响

策略通过 `category` 属性分为三类，影响多个下游行为：

| Category | 代表策略 | PendingEntry 入场模式 | HTF 冲突阻止 |
|----------|---------|-------------------|-----------:|
| `trend` | sma_trend, supertrend, macd_momentum | pullback（回调入场，参考价=均线支撑） | **阻止**（逆大周期不下单） |
| `reversion` | rsi_reversion, cci_reversion, bollinger_breakout | symmetric（对称入场，参考价=BB middle） | **豁免**（反转本身就是逆势） |
| `breakout` | donchian_breakout, asian_range_breakout | momentum（追涨入场，参考价=Donchian 关键位） | **阻止** |

HTF 冲突阻止配置：`signal.ini [htf_conflict_block]`，M5/M15/M30 趋势策略逆大周期方向直接拒绝。

---

## 8. RegimeSizing — 时间框架差异化参数

### SL/TP ATR 倍数（按 TF）

| TF | SL (ATR倍数) | TP (ATR倍数) |
|----|:-----------:|:-----------:|
| M5 | 1.8 | 2.5 |
| M15 | 1.8 | 2.5 |
| M30 | 2.0 | 2.5 |
| H1 | 2.0 | 2.5 |
| H4 | 2.2 | 3.0 |
| D1 | 2.5 | 3.5 |

### 风险百分比缩放

| TF | 风险乘数 | 含义 |
|----|:-------:|------|
| M5 | ×0.50 | 短周期仓位减半 |
| M15 | ×0.75 | |
| M30 | ×0.90 | |
| H1 | ×1.00 | 基准 |
| H4 | ×1.20 | |
| D1 | ×1.50 | 长周期仓位放大 |

### Per-TF min_confidence

`signal.ini [timeframe_min_confidence]`：M5=0.78, H1=0.70, D1=0.60（短周期要求更高置信度）。

---

## 9. Indicator Delta 机制

核心动量指标自动计算 N-bar 变化率（一阶导数），通过 `indicators.json` 的 `delta_bars` 字段配置：

- 覆盖指标：`rsi14`, `macd`, `cci20`, `stochrsi`, `adx14`
- 输出格式：`{"rsi": 28.5, "rsi_d3": -8.2, "rsi_d5": -12.0}`（`_d3`/`_d5` 后缀）
- 实现：`src/indicators/result_store.py`

---

## 10. 回测安全约束

`RecommendationEngine` 自动拒绝不可信的参数建议：

| 约束 | 阈值 | 说明 |
|------|------|------|
| 过拟合比率 | > 1.5 拒绝 | avg(IS_sharpe) / avg(OOS_sharpe) |
| 一致性率 | < 50% 拒绝 | OOS 盈利窗口占比 |
| OOS 样本量 | < 10 笔拒绝 | 样本不足无法判断 |
| 单参数变更 | 裁剪到 ±30% | 防止极端跳跃 |
| 亲和度调整 | 步幅 ±15% | 渐进调整 |
| 胜率优先级 | Regime-specific > 全局 | 按市况分层评估 |
