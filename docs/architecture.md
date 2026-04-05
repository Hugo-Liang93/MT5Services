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

### src/trading/ — 7 个子包

根目录：`models.py` / `ports.py`（8 个 Protocol）/ `registry.py` / `trading_service.py`

子包：`application/`（CQRS 命令/查询）| `execution/`（下单/sizing/gate）| `pending/`（挂单追踪）| `positions/`（持仓管理）| `closeout/`（风险收口）| `tracking/`（信号质量+成交结果）| `state/`（状态持久化+恢复）

### src/signals/orchestration/ — 薄协调器 + 协作者

`runtime.py`（门面）+ `runtime_evaluator.py` + `runtime_processing.py` + `runtime_recovery.py` + `state_machine.py` + `vote_processor.py` + `htf_resolver.py` + `affinity.py` + `policy.py` + `voting.py`

### src/api/ — 12 个子域路由包

每个子域：`<domain>.py`（组合根）+ `<domain>_routes/`（路由+view_models）

### src/backtesting/ — 5 个子包

`engine/`（runner/indicators/signals/portfolio）| `filtering/`（simulator/builder/economic）| `analysis/`（metrics/correlation/monte_carlo/report）| `optimization/`（optimizer/walk_forward/recommendation）| `data/`（loader/store）+ `paper_trading/`（bridge/portfolio/tracker/config/models/ports）

### src/monitoring/ — 2 个子包

`health/`（monitor/metrics_store/checks/reporting/common）| `pipeline/`（event_bus/events/trace_recorder）

---

## 6. 新增文件归位规则

1. 是否属于现有领域目录？
2. 是否属于现有子包职责？
3. 是否只是现有文件过大，应拆到同子包？
4. 只有形成稳定新职责时才新增子包

**不应新增子包的情况**：只有一个 helper、只是命名好看、无稳定对外接口。
