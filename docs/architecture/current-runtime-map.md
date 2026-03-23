# 当前运行时组件映射

> 基于主分支代码实际分析，非理想化架构图。
> 目的：让新成员和 AI 助手准确理解"系统启动后到底有哪些活跃对象、线程和队列"。

---

## 1. 全局单例容器

系统启动后，所有组件通过 `src/api/deps.py` 中的全局单例 `_c: _Container` 持有。

```
_Container（全局唯一）
├── _MarketContainer
│   ├── service: MarketDataService           761 行
│   ├── storage_writer: StorageWriter
│   ├── ingestor: BackgroundIngestor
│   └── indicator_manager: UnifiedIndicatorManager  1,366 行
│
├── _SignalContainer
│   ├── signal_module: SignalModule
│   ├── signal_runtime: SignalRuntime        1,744 行
│   ├── htf_cache: HTFStateCache
│   ├── signal_quality_tracker: SignalQualityTracker
│   ├── calibrator: ConfidenceCalibrator
│   ├── performance_tracker: StrategyPerformanceTracker
│   └── market_structure_analyzer: MarketStructureAnalyzer
│
├── _TradingContainer
│   ├── trade_registry: TradingAccountRegistry
│   ├── trade_module: TradingModule
│   ├── trade_executor: TradeExecutor
│   ├── position_manager: PositionManager
│   ├── trade_outcome_tracker: TradeOutcomeTracker
│   └── pending_entry_manager: PendingEntryManager
│
├── _MonitoringContainer
│   ├── health_monitor: HealthMonitor（SQLite）
│   └── monitoring_manager: MonitoringManager
│
├── economic_calendar_service: EconomicCalendarService
└── market_impact_analyzer: Any（可选）
```

**访问方式**：
- API 路由：`FastAPI.Depends(get_market_service)` 等 21 个 getter
- 内部服务：直接 `from src.api.deps import get_*` 导入调用
- 所有 getter 首次调用时触发 `_ensure_initialized()`（双检锁 + `threading.Lock`）

---

## 2. 初始化顺序（`_ensure_initialized()`）

```
_ensure_initialized()                     ← deps.py:381, 双检锁保护
│
│ ── 阶段 1：配置加载 ──
│  get_runtime_ingest_settings()
│  get_runtime_market_settings()
│
│ ── 阶段 2：市场数据层 ──
│  ① create_market_service()             → MarketDataService
│  ② create_storage_writer()             → StorageWriter
│  ③ service.attach_storage(writer)      → 连接 DB fallback
│  ④ create_ingestor()                   → BackgroundIngestor
│  ⑤ create_indicator_manager()          → UnifiedIndicatorManager
│
│ ── 阶段 3：交易与日历 ──
│  ⑥ build_trading_components()
│     ├─ EconomicCalendarService
│     ├─ TradingAccountRegistry
│     └─ TradingModule
│  ⑦ MarketImpactAnalyzer（可选）
│
│ ── 阶段 4：信号系统 ──
│  ⑧ build_signal_components()            ← factories/signals.py, 最复杂
│     ├─ MarketStructureAnalyzer
│     ├─ MarketRegimeDetector
│     ├─ ConfidenceCalibrator
│     ├─ StrategyPerformanceTracker
│     ├─ SignalModule + register_all_strategies
│     ├─ HTFStateCache
│     ├─ set_intrabar_eligible_override()  → 策略驱动 intrabar 指标集合
│     ├─ SignalRuntime
│     ├─ PositionManager
│     ├─ TradeOutcomeTracker
│     ├─ ExecutionGate
│     ├─ PendingEntryManager（延迟绑定 execute_fn）
│     ├─ TradeExecutor
│     ├─ SignalQualityTracker
│     └─ 注册 listener 连接:
│        signal_runtime → trade_executor.on_signal_event
│        signal_runtime → signal_quality_tracker.on_signal_event
│        signal_runtime → htf_cache.on_signal_event
│        position_manager → trade_outcome_tracker.on_position_closed
│
│ ── 阶段 5：监控 ──
│  ⑨ get_health_monitor()                → HealthMonitor（SQLite）
│  ⑩ get_monitoring_manager()            → MonitoringManager
│
│ ── 阶段 6：热重载 ──
│  ⑪ register_signal_hot_reload()        → signal.ini 变更回调
│
└─ _initialized = True
```

---

## 3. 启动顺序（`lifespan` 上下文管理器）

`_ensure_initialized()` 完成后，`src/api/lifespan.py` 按序启动各组件后台线程：

```
lifespan(app) [src/api/lifespan.py:55-213]
│
├─ _ensure_initialized()                  ← 触发阶段 1-6
│
├─ 启动序列（按顺序）:
│  ① storage_writer.start()              → DB 队列刷写线程
│  ② indicator_manager.start()           → 注册 listener + 4 个后台线程
│  ③ ingestor.start()                    → MT5 采集主线程
│  ④ economic_calendar_service.start()   → 日历同步线程
│  ⑤ signal_runtime.start()             → 信号评估事件循环线程
│  ⑥ calibrator.load() + start_background_refresh()
│  ⑦ pending_entry_manager.start()      → 价格监控线程
│  ⑧ position_manager.start() + sync_open_positions()
│  ⑨ monitoring_manager.register_component(...) × 6 + start()
│
├─ startup_status["phase"] = "running"
│
├─ yield                                  ← 应用运行中
│
└─ shutdown:
   ① monitoring_manager.stop()
   ② signal_runtime.stop()
   ③ trade_executor.shutdown()
   ④ pending_entry_manager.shutdown()
   ⑤ position_manager.stop()
   ⑥ ingestor.stop()
   ⑦ market_data.shutdown()
   ⑧ indicator_manager.shutdown()
   ⑨ economic_calendar_service.stop()
   ⑩ storage_writer.stop()                ← 最后停，确保残余数据落盘
```

---

## 4. 活跃线程清单

系统运行期间的所有后台线程：

| # | 线程名/来源 | 所属组件 | 职责 |
|---|-----------|---------|------|
| 1 | `uvicorn-main` | FastAPI | HTTP 请求处理 |
| 2 | `ingestor-main` | BackgroundIngestor | MT5 行情采集主循环（poll_interval=0.5s） |
| 3 | `indicator-event` | UnifiedIndicatorManager | 处理 durable closed-bar events |
| 4 | `indicator-writer` | UnifiedIndicatorManager | SQLite 事件批量写入 |
| 5 | `indicator-intrabar` | UnifiedIndicatorManager | 盘中预览指标计算 |
| 6 | `indicator-reload` | UnifiedIndicatorManager | 可选：配置热加载 |
| 7 | `signal-runtime` | SignalRuntime | 信号评估事件循环（双队列消费） |
| 8 | `pending-entry` | PendingEntryManager | Quote 价格确认入场监控 |
| 9 | `position-manager` | PositionManager | 持仓监控、止损跟踪 |
| 10 | `calibrator-refresh` | ConfidenceCalibrator | 后台定时刷新校准数据 |
| 11 | `economic-sync` | EconomicCalendarService | 日历数据定时同步 |
| 12 | `monitoring` | MonitoringManager | 定时健康巡检 |
| 13 | `storage-*` × N | StorageWriter | 每个通道一个刷写线程（ticks/quotes/ohlc/...） |
| 14 | `mds-listener-*` × 2 | MarketDataService | listener 异步线程池 |

**总计**：约 16-20 个活跃线程

---

## 5. 队列架构

### 5.1 信号运行时队列

| 队列 | 容量 | 可靠性 | 消费者 | 满溢策略 |
|------|------|--------|--------|---------|
| `_confirmed_events` | 4,096 | 严格 | signal-runtime 线程 | backpressure 重试 1s → 丢弃 + WARNING |
| `_intrabar_events` | 8,192 | best-effort | signal-runtime 线程 | put_nowait 丢弃 |

**调度策略**：confirmed 优先；每连续消费 5 个 confirmed 后检查 intrabar（Anti-starvation）

### 5.2 指标系统队列

| 队列 | 容量 | 消费者 | 满溢策略 |
|------|------|--------|---------|
| `_intrabar_queue` | 200 | indicator-intrabar 线程 | 丢弃 |
| `_event_write_queue` | 2,048 | indicator-writer 线程 | 异步 SQLite 批量写入 |
| OHLC event store | SQLite | indicator-event 线程 | durable，持久化 |

### 5.3 持久化队列（StorageWriter）

| 通道 | 队列大小 | 刷写间隔 | 批大小 | 满溢策略 |
|------|---------|---------|-------|---------|
| ticks | 20,000 | 1s | 1,000 | drop_oldest |
| quotes | 5,000 | 1s | 200 | auto |
| intrabar | 10,000 | 5s | 200 | drop_newest |
| ohlc | 5,000 | 1s | 200 | block |
| ohlc_indicators | 5,000 | 1s | 200 | block |
| economic_calendar | 1,000 | 2s | 100 | auto |

### 5.4 交易执行队列

| 队列 | 容量 | 消费者 | 满溢策略 |
|------|------|--------|---------|
| TradeExecutor 内部 | 有限 | executor daemon 线程 | confirmed 信号 backpressure 重试 |
| PendingEntryManager 内部 | 有限 | pending-entry 线程 | 超时过期清理 |

---

## 6. 事件回调链

### 6.1 K 线收盘事件链

```
BackgroundIngestor._ingest_ohlc()
  → MarketDataService.set_ohlc_closed()
  → MarketDataService.enqueue_ohlc_closed_event()
    ├─→ ohlc_close_listeners（通过线程池异步分发）
    │   └─→ UnifiedIndicatorManager._on_ohlc_closed()
    │       → event_store.record_event()          [SQLite durable]
    │       → _event_write_queue.put()             [异步批量]
    │
    └─→ event_store → indicator-event 线程
        → process_closed_bar_events_batch()
        → pipeline.compute() → 全量 21 个指标
        → publish_snapshot(scope="confirmed")
          → SignalRuntime._on_snapshot()
            → _confirmed_events.put()
```

### 6.2 Intrabar 事件链

```
BackgroundIngestor._ingest_intrabar()
  → MarketDataService.set_intrabar()
    → intrabar_listeners（通过线程池异步分发）
      └─→ UnifiedIndicatorManager._on_intrabar()
          → _intrabar_queue.put_nowait()          [可丢弃]

indicator-intrabar 线程:
  → _load_intrabar_bars()
  → pipeline.compute() → 仅 8 个 intrabar 指标
  → store_preview_snapshot() 去重检查
  → publish_snapshot(scope="intrabar")
    → SignalRuntime._on_snapshot()
      → _intrabar_events.put_nowait()
```

### 6.3 信号事件广播

```
SignalRuntime._publish_signal_event(event)
  ├─→ TradeExecutor.on_signal_event()       → 自动下单
  ├─→ SignalQualityTracker.on_signal_event() → 信号质量追踪
  └─→ HTFStateCache.on_signal_event()       → HTF 方向缓存
```

### 6.4 交易结果反馈

```
TradeExecutor 下单成功
  → TradeOutcomeTracker.on_trade_opened()

PositionManager 检测关仓
  → TradeOutcomeTracker.on_position_closed()
    → StrategyPerformanceTracker(source="trade")
    → trade_outcomes 表

SignalQualityTracker N bars 到期
  → StrategyPerformanceTracker(source="signal")
  → signal_outcomes 表
  → ConfidenceCalibrator（后台查 DB）
```

---

## 7. 锁与并发控制

| 锁 | 类型 | 保护对象 | 竞争热度 |
|----|------|---------|---------|
| `MarketDataService._lock` | RLock | 所有缓存 + listener 列表 | 高（读多写少） |
| `SignalRuntime._shard_locks[key]` | Lock | 每个 (symbol,tf) 的状态 | 低（分片） |
| `SignalRuntime._listener_lock` | Lock | signal_listeners 列表 | 极低 |
| `IndicatorManager._results_lock` | RLock | _results + _last_preview_snapshot | 中等 |
| `IndicatorManager._snapshot_listeners_lock` | Lock | snapshot_listeners 列表 | 极低 |
| `deps._init_lock` | Lock | 初始化标志 | 仅启动时 |

---

## 8. 外部依赖

| 系统 | 用途 | 连接方式 |
|------|------|---------|
| MT5 终端 | 行情采集 + 交易执行 | MT5 Python binding（进程内） |
| TimescaleDB (PostgreSQL) | 历史数据持久化 | psycopg2 连接池 |
| SQLite (health_monitor.db) | 健康指标存储 | 本地文件 |
| SQLite (events.db) | 指标事件 durable store | 本地文件 |

---

## 9. 内存缓存清单

| 缓存 | 所属 | 类型 | 上限 | 淘汰策略 |
|------|------|------|------|---------|
| `_tick_cache` | MarketDataService | Dict[symbol, Deque] | tick_cache_size per symbol | FIFO（deque maxlen） |
| `_quote_cache` | MarketDataService | Dict[symbol, Quote] | 200 条 | 计数剪枝（stale × 10） |
| `_ohlc_closed_cache` | MarketDataService | Dict[key, List[OHLC]] | ohlc_cache_limit per key | 保留最新 N 根 |
| `_intrabar_cache` | MarketDataService | Dict[key, List[OHLC]] | intrabar_max_points | 新 bar 清空 |
| `_results` | IndicatorManager | OrderedDict | 2,000 条 | LRU |
| `_last_preview_snapshot` | IndicatorManager | OrderedDict | 500 条 | LRU |
| `_vote_fusion_cache` | SignalRuntime | Dict | 2,000 条 | 时间窗口 |
| `_indicator_miss_counts` | SignalRuntime | Dict | 500 条 | 保留高频 |
| RuntimeSignalState | SignalRuntime | Dict[(sym,tf,strategy)] | 无硬上限 | 不淘汰 |
| Calibrator 缓存 | ConfidenceCalibrator | 内存 + 文件 | 按策略/regime | 文件持久化 |
