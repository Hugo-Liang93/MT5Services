# CLAUDE.md — MT5Services Codebase Guide

This file is the primary reference for AI assistants working on MT5Services. It covers architecture, conventions, workflows, and rules to follow when making changes.

---

## Language Preference

**请始终使用中文回复。** All responses, explanations, code comments suggestions, and conversations must be in Chinese (Simplified). Code itself (variable names, function names, string literals in source files) follows the existing conventions of each file.

---

## Project Overview

**MT5Services** is a production-ready FastAPI trading platform that connects to MetaTrader 5 (MT5) terminals. It provides real-time market data, technical indicators, signal generation, risk management, and trade execution via REST API, backed by TimescaleDB (PostgreSQL with time-series hypertables).

- **Python**: 3.9–3.12
- **Framework**: FastAPI + uvicorn
- **Database**: TimescaleDB (PostgreSQL extension)
- **Trading terminal**: MetaTrader 5 (Windows-only Python binding)
- **Entry point**: `python app.py` or `uvicorn src.api:app`

---

## Repository Structure

```
MT5Services/
├── app.py                    # 主启动器（解析 host/port，启动 uvicorn）
├── pyproject.toml            # 项目元数据、依赖、工具配置
├── requirements.txt          # 运行时依赖
├── config/                   # 所有配置文件
│   ├── app.ini               # 单一信号源：品种、时间框架、采集间隔、缓存限制
│   ├── market.ini            # API 服务配置（host、port、认证、CORS）
│   ├── mt5.ini               # MT5 终端连接与账户配置
│   ├── db.ini                # TimescaleDB 连接配置
│   ├── ingest.ini            # 后台数据采集配置
│   ├── storage.ini           # 多通道队列持久化配置
│   ├── economic.ini          # 经济日历与 Trade Guard 配置
│   ├── risk.ini              # 风险限制（仓位数量、SL/TP 要求）
│   ├── cache.ini             # 运行时内存缓存大小（覆盖 app.ini [limits]）
│   ├── signal.ini            # 信号模块配置
│   ├── indicators.json       # 指标定义与计算流水线
│   └── composites.json       # 复合策略组合定义
├── src/
│   ├── api/                  # FastAPI 路由、中间件、Schema、DI 容器
│   │   ├── factories/        # 组件工厂函数（market/storage/trading/signals/indicators）
│   │   ├── trade_dispatcher.py # TradeAPIDispatcher 统一交易调度入口
│   │   └── lifespan.py       # 启动/关闭生命周期管理
│   ├── calendar/             # 经济日历服务（原 src/core/economic_calendar_service.py）
│   │   └── economic_calendar/ # calendar_sync / trade_guard / observability
│   ├── clients/              # MT5 客户端封装（行情、交易、账户）
│   ├── config/               # 配置加载、合并、Pydantic 模型
│   │   ├── models/           # signal.py / runtime.py 配置模型
│   │   ├── database.py       # 数据库配置加载
│   │   ├── indicator_config.py  # 指标配置加载
│   │   ├── indicator_runtime.py # 指标运行时配置
│   │   ├── mt5.py            # MT5 终端配置加载
│   │   ├── signal.py         # 信号模块配置加载
│   │   ├── storage.py        # 存储配置加载
│   │   └── runtime.py        # 运行时配置加载
│   ├── indicators/           # 统一指标系统（管理器、引擎、缓存）
│   │   ├── manager.py        # UnifiedIndicatorManager 核心编排器
│   │   ├── bar_event_handler.py  # 收盘K线事件批处理
│   │   ├── pipeline_runner.py    # 指标管道运行逻辑
│   │   ├── result_store.py       # 指标结果存储与规范化
│   │   ├── snapshot_publisher.py # 指标快照发布与去重
│   │   ├── types.py              # 指标类型定义
│   │   ├── cache/            # 缓存子系统
│   │   │   ├── smart_cache.py    # 智能缓存机制
│   │   │   └── incremental.py    # 增量计算支持
│   │   ├── core/             # 指标计算函数（mean/momentum/volatility/volume）
│   │   ├── engine/           # 计算引擎
│   │   │   ├── pipeline.py       # 计算管道
│   │   │   ├── dependency_manager.py # 依赖管理器
│   │   │   └── parallel_executor.py  # 并行执行器
│   │   └── monitoring/       # 指标监控
│   │       └── metrics_collector.py  # 计算耗时收集器
│   ├── ingestion/            # 后台 Tick/OHLC/Intrabar 数据采集
│   ├── market/               # MarketDataService（原 src/core/market_service.py）
│   ├── market_structure/     # 市场结构分析器（MarketStructureAnalyzer）
│   │   └── models.py         # MarketStructureContext 数据类
│   ├── monitoring/           # 健康检查
│   │   ├── health_monitor.py # SQLite 指标存储、告警
│   │   ├── health_checks.py  # 健康检查实现
│   │   ├── health_common.py  # 健康检查通用工具
│   │   ├── health_reporting.py # 健康报告生成
│   │   └── manager.py        # MonitoringManager 定时巡检
│   ├── persistence/          # TimescaleDB 写入器、队列持久化、Schema 定义
│   │   ├── storage_writer.py # 基于队列的多通道持久化
│   │   ├── db.py             # 数据库连接管理
│   │   ├── validator.py      # 数据校验
│   │   ├── schema/           # 各通道数据表 Schema
│   │   └── repositories/     # 数据仓储层（按领域分离）
│   │       ├── trade_repo.py     # 交易操作仓储
│   │       ├── signal_repo.py    # 信号追踪仓储
│   │       ├── economic_repo.py  # 经济日历仓储
│   │       ├── market_repo.py    # 市场数据仓储
│   │       └── runtime_repo.py   # 运行时任务仓储
│   ├── risk/                 # 风险规则、模型、服务
│   │   └── models.py         # TradeIntent / RiskCheckResult / RiskAssessment
│   ├── signals/              # 信号生成策略、运行时、过滤器
│   │   ├── models.py         # SignalEvent / SignalContext / SignalDecision / SignalRecord
│   │   ├── analytics/        # 诊断分析工具
│   │   │   ├── diagnostics.py    # DiagnosticsEngine 实现
│   │   │   ├── interfaces.py     # DiagnosticsEngine Protocol 定义
│   │   │   └── plugins.py        # AnalyticsPluginRegistry 插件扩展
│   │   ├── contracts/        # 接口/常量定义（sessions.py）
│   │   ├── evaluation/       # Regime 分类、置信度校准、绩效追踪
│   │   │   ├── regime.py         # MarketRegimeDetector / SoftRegimeResult
│   │   │   ├── calibrator.py     # ConfidenceCalibrator 历史校准
│   │   │   ├── performance.py    # StrategyPerformanceTracker 日内绩效
│   │   │   └── indicators_helpers.py # 跨模块指标数据提取
│   │   ├── execution/        # 信号过滤器（SignalFilterChain）
│   │   ├── orchestration/    # SignalRuntime / SignalPolicy / StrategyVotingEngine
│   │   ├── strategies/       # 策略实现（trend/mean_reversion/breakout/composite）
│   │   │   └── adapters.py   # UnifiedIndicatorSourceAdapter（解耦信号与指标）
│   │   └── tracking/         # SignalRepository（信号持久化与查询）
│   ├── trading/              # TradingModule、TradeExecutor、PositionManager、OutcomeTracker
│   │   └── models.py         # TradeOperationRecord 数据类
│   └── utils/                # 通用工具、内存管理器
├── tests/                    # 测试套件（镜像 src/ 结构）
└── docs/                     # 架构文档、内部设计文档
```

---

## Configuration System

### Critical Rule: `config/app.ini` is the Single Source of Truth

所有交易品种、时间框架和全局采集间隔**仅**在 `config/app.ini` 中定义。其他 `.ini` 文件从此继承。禁止在其他配置文件中重复定义这些参数。

### Resolution Order (highest to lowest priority)

```
config/*.local.ini  （已被 .gitignore，用于密钥和机器特定覆盖）
    ↓
config/*.ini        （已提交的基础配置）
    ↓
code defaults       （src/config/centralized.py 中 Pydantic 模型的字段默认值）
```

### Key Config Files

| 文件 | 用途 |
|------|------|
| `config/app.ini` | 交易品种、时间框架、采集间隔、缓存限制 |
| `config/market.ini` | API host/port、认证（API key）、CORS |
| `config/mt5.ini` | MT5 终端路径、账户配置（login/password/server） |
| `config/db.ini` | PostgreSQL/TimescaleDB 连接参数 |
| `config/ingest.ini` | 采集间隔、回填限制、重试配置 |
| `config/storage.ini` | 队列通道大小、刷写间隔、批处理大小、溢出策略 |
| `config/economic.ini` | 日历数据源（FRED、TradingEconomics）、Trade Guard 窗口 |
| `config/risk.ini` | 最大仓位数、SL/TP 要求 |
| `config/cache.ini` | 运行时内存缓存大小（覆盖 app.ini [limits]，优先级更高） |
| `config/signal.ini` | 自动交易、仓位大小、过滤条件、状态机参数 |
| `config/indicators.json` | 指标定义、参数、依赖关系、流水线配置 |
| `config/composites.json` | 复合策略组合定义 |

### No `.env` Files

`.env` 文件已**废弃**。所有配置均在 `.ini` 文件中。`python-dotenv` 仅作为遗留兼容依赖保留。

---

## Architecture

### Startup Sequence

组件按以下顺序初始化（见 `src/api/lifespan.py` + `src/api/deps.py`）：

1. **StorageWriter** — TimescaleDB 多通道队列写入器
2. **MarketDataService** — 内存行情缓存（`src/market/`）
3. **BackgroundIngestor** — 后台行情数据采集线程
4. **EconomicCalendarService** — 日历同步（`src/calendar/`）
5. **UnifiedIndicatorManager** — 统一指标编排器
6. **MarketStructureAnalyzer** — 市场结构分析器（`src/market_structure/`）
7. **StrategyPerformanceTracker** — 日内策略绩效追踪（`src/signals/evaluation/performance.py`）
8. **SignalModule** — 策略注册与评估引擎
9. **SignalRuntime** — 事件驱动信号运行时（`src/signals/orchestration/`）
10. **TradeExecutor** — 信号自动交易执行器（回调注入 PerformanceTracker）
11. **PositionManager** — 持仓监控与管理
12. **MonitoringManager** — 健康检查与巡检

关闭顺序相反。所有组件作为单例通过 `_Container` 类（`src/api/deps.py`）访问，通过 `FastAPI.Depends()` 在路由处理器中暴露。

### Data Flow

```
BackgroundIngestor (src/ingestion/ingestor.py)
│  主轮询循环，sleep = poll_interval（默认 0.5s）
│  每轮对每个品种依次主动调用 MT5 API：
│
├─ get_quote()          → MarketDataService.set_quote()      每轮
├─ copy_ticks_from()    → MarketDataService.extend_ticks()   每轮
├─ copy_rates_from_pos()→ MarketDataService.set_ohlc_closed()按 ohlc_interval 节流
├─ get_ohlc(count=1)    → MarketDataService.set_intrabar()   按 intrabar_interval 节流
│
│  同时写入 StorageWriter（多通道异步队列）→ TimescaleDB
│
API 路由从 MarketDataService 缓存读取
    ↓
OHLC 收盘事件 → IndicatorManager → 快照发布
                                       ↓
                              SignalRuntime（src/signals/orchestration/）
                                       ↓
                              strategies 评估 + Regime 检测
                                       ↓
                         VotingEngine（consensus / group 投票）
                                       ↓
                        TradeExecutor → TradingService → MT5
```

> **采集节奏说明**：`poll_interval` 是主循环的 sleep 间隔（控制 tick/quote 的有效采集频率）。
> OHLC 和 intrabar 在主循环内有各自独立的 `next_*_at` 时间戳节流，不受 `poll_interval` 直接约束。

### In-Memory Cache Architecture

`MarketDataService` (`src/market/service.py`) 是**运行时数据的唯一权威**：
- 持有所有 Tick、报价、OHLC、Intrabar 状态
- 通过 `RLock` 保证线程安全
- 单一写入者：`BackgroundIngestor`
- 多个读取者：API 路由、指标管理器、信号运行时
- TimescaleDB 是**持久化备份**，不是主读路径

### Multi-Channel Persistence

`StorageWriter` (`src/persistence/storage_writer.py`) 每类数据使用独立的异步队列：

| 通道 | 队列大小 | 刷写间隔 | 批处理大小 | 满溢策略 |
|------|---------|---------|-----------|---------|
| ticks | 20,000 | 1s | 1,000 | drop_oldest |
| quotes | 5,000 | 1s | 200 | auto |
| intrabar | 10,000 | 5s | 200 | drop_newest |
| ohlc | 5,000 | 1s | 200 | block |
| ohlc_indicators | 5,000 | 1s | 200 | block |
| economic_calendar | 1,000 | 2s | 100 | auto |

### Persistence Repository Layer

`src/persistence/repositories/` 提供按领域分离的数据仓储抽象，与 `StorageWriter` 队列写入互补：

| 仓储 | 职责 |
|------|------|
| `trade_repo.py` | 交易操作记录读写 |
| `signal_repo.py` | 信号追踪记录查询与持久化 |
| `economic_repo.py` | 经济日历数据存取 |
| `market_repo.py` | 市场数据（OHLC/Tick/Quote）历史查询 |
| `runtime_repo.py` | 运行时任务状态持久化 |

### Signal Runtime Queue Architecture

`SignalRuntime` (`src/signals/orchestration/runtime.py`) 使用**两个独立队列**按 scope 分离：

| 队列 | 大小 | 可丢弃 | 说明 |
|------|------|--------|------|
| `_confirmed_events` | 2,048 | 否（有 backpressure 重试） | K 线收盘信号，优先消费 |
| `_intrabar_events` | 4,096 | 是 | 实时预览信号；仅在 confirmed 队列为空时处理 |

`process_next_event()` 总是先 `get_nowait()` 从 confirmed 队列取，空才 blocking wait intrabar 队列。

---

## Key Source Files

| 文件 | 职责 |
|------|------|
| `src/api/deps.py` | `_Container` DI 容器、单例管理 |
| `src/api/lifespan.py` | 启动/关闭生命周期（`shutdown_components`） |
| `src/api/factories/` | 各组件工厂函数（market/storage/trading/signals/indicators） |
| `src/api/__init__.py` | FastAPI app、CORS、API key 认证、路由注册 |
| `src/api/trade_dispatcher.py` | TradeAPIDispatcher：统一交易 API 调度入口，减少路由层重复逻辑 |
| `src/config/centralized.py` | Pydantic 配置模型（所有配置节） |
| `src/market/service.py` | 线程安全的内存行情缓存（MarketDataService） |
| `src/ingestion/ingestor.py` | 后台数据采集（Tick、OHLC、Intrabar） |
| `src/indicators/manager.py` | 统一指标编排器（UnifiedIndicatorManager） |
| `src/indicators/bar_event_handler.py` | 收盘 K 线事件批处理（从 manager 拆分） |
| `src/indicators/pipeline_runner.py` | 指标管道运行逻辑（从 manager 拆分） |
| `src/indicators/result_store.py` | 指标结果存储与规范化 |
| `src/indicators/snapshot_publisher.py` | 指标快照发布与去重 |
| `src/persistence/storage_writer.py` | 基于队列的多通道持久化 |
| `src/persistence/repositories/` | 数据仓储层：trade / signal / economic / market / runtime 五个仓储 |
| `src/calendar/service.py` | 经济日历服务（EconomicCalendarService） |
| `src/calendar/economic_calendar/trade_guard.py` | Trade Guard（阻止高风险时段交易） |
| `src/market_structure/analyzer.py` | 市场结构分析（MarketStructureAnalyzer） |
| `src/signals/models.py` | SignalEvent / SignalContext / SignalDecision / SignalRecord 数据类 |
| `src/signals/service.py` | SignalModule：策略注册、evaluate()、diagnostics |
| `src/signals/orchestration/runtime.py` | SignalRuntime：事件驱动评估主循环 |
| `src/signals/orchestration/policy.py` | SignalPolicy / VotingGroupConfig |
| `src/signals/orchestration/voting.py` | StrategyVotingEngine：多策略加权投票 |
| `src/signals/evaluation/regime.py` | MarketRegimeDetector / RegimeTracker / **SoftRegimeResult** |
| `src/signals/evaluation/calibrator.py` | ConfidenceCalibrator：分阶段历史胜率反馈校准 |
| `src/signals/evaluation/performance.py` | StrategyPerformanceTracker：日内策略绩效追踪（纯内存实时反馈） |
| `src/signals/evaluation/indicators_helpers.py` | 跨模块共享的指标数据提取函数集 |
| `src/signals/execution/filters.py` | SignalFilterChain：点差/时段/经济事件/**时段切换冷却**过滤 |
| `src/signals/tracking/repository.py` | SignalRepository：信号持久化与查询 |
| `src/signals/strategies/htf_cache.py` | HTFStateCache：高时间框架状态缓存 |
| `src/signals/strategies/adapters.py` | UnifiedIndicatorSourceAdapter：解耦信号模块与指标管理器 |
| `src/signals/analytics/interfaces.py` | DiagnosticsEngine Protocol 定义 |
| `src/signals/analytics/plugins.py` | AnalyticsPluginRegistry 插件扩展机制 |
| `src/trading/service.py` | TradingModule：账户、持仓、订单生命周期 |
| `src/trading/trading_service.py` | TradingService：底层下单、平仓、保证金计算 |
| `src/trading/signal_executor.py` | TradeExecutor：信号自动下单执行、日内头寸限制、HTF 软惩罚 |
| `src/trading/position_manager.py` | PositionManager：持仓监控、止损跟踪、日终自动平仓 |
| `src/trading/sizing.py` | 仓位计算、时间框架差异化 SL/TP（`TimeframeSLTP`） |
| `src/trading/outcome_tracker.py` | OutcomeTracker：交易结果跟踪与胜率统计 |
| `src/trading/registry.py` | TradingAccountRegistry：多账户注册与服务工厂 |
| `src/trading/models.py` | TradeOperationRecord 数据类 |
| `src/risk/models.py` | TradeIntent / RiskCheckResult / RiskAssessment 数据类 |
| `src/market_structure/models.py` | MarketStructureContext 数据类 |
| `src/monitoring/health_monitor.py` | SQLite 指标存储、告警、健康报告 |
| `src/monitoring/health_checks.py` | 健康检查具体实现（从 health_monitor 拆分） |
| `src/monitoring/health_reporting.py` | 健康报告生成 |
| `src/monitoring/manager.py` | MonitoringManager：定时巡检、组件协调 |

---

## Intrabar vs Confirmed 数据链路

指标和策略都在两条独立链路上运行，**必须理解这个区别**，否则会出现策略订阅了不存在的快照或指标缺失等问题。

### Intrabar 的准确含义

**intrabar ≠ 每一笔 tick**。正确理解是：**当前未收盘 K 线在一定频率间隔下的 OHLC 快照更新**。

| 属性 | 说明 |
|------|------|
| 触发方式 | BackgroundIngestor 主动轮询 `client.get_ohlc(symbol, tf, 1)` 获取当前未收盘 bar |
| 采集节流 | 每个 `(symbol, tf)` 有独立的 `next_intrabar_at` 时间戳；间隔 = per-tf config > global `intrabar_interval` > auto `max(5s, bar时长×5%)` |
| 数据内容 | N-1 根完整收盘 K 线 + 当前 bar（含最新 tick 的 open/high/low/close/volume）|
| 去重机制 | Ingestor 层：OHLC 值与上次相同则跳过 `set_intrabar()`；指标层：快照与上次完全相同则不发布 |
| 可靠性 | best-effort：队列满则丢弃，SignalRuntime 在消费前 confirmed 事件积压时延迟处理 |

> 即：当价格在两次计算间隔内没有实质变化（OHLC 未变 → 指标不变），上游不会触发任何新事件，策略的 preview 状态机保持原状，等待下一次有效更新。

### 链路全景

```
BackgroundIngestor._ingest_intrabar()
  │ 主动轮询 client.get_ohlc(symbol, tf, 1)
  │ 按 next_intrabar_at 独立节流（per-tf config > global > auto max(5s, bar×5%)）
  │ OHLC 去重：与上次相同则跳过
  ↓
 MarketDataService.set_intrabar() → 触发 IndicatorManager
  ↓
 _intrabar_queue（put_nowait，满则丢弃——best-effort）
  ↓
 _intrabar_loop 独立线程
  ↓ _load_intrabar_bars()
    ← N-1 根收盘K线 + 当前bar（含最新tick的 OHLC 值）
  ↓
 仅策略推导出的 intrabar 指标（当前 8 个）
  ↓
 store_preview_snapshot() 去重检查
  ├─ 结果与上次相同 → 跳过，不发布（策略保持上次状态）
  └─ 结果有变化 → publish_snapshot(scope="intrabar")
                    ↓
                  SignalRuntime._intrabar_events 队列（4096，可丢弃）
                    ↓
                  confirmed 队列空时才消费；scope="intrabar" 的策略评估

────────────────────── 对比：confirmed 链路 ──────────────────────

BackgroundIngestor._ingest_ohlc()
  │ 主动轮询 client.get_ohlc_from() / get_ohlc()
  │ 按 next_ohlc_at 独立节流（ohlc_interval，按时间框架可配不同间隔）
  │ 仅写入已收盘 bar
  ↓
 MarketDataService.set_ohlc_closed() + enqueue_ohlc_closed_event()
  ↓ 事件写入 events.db（持久化，不丢失）
  ↓ process_closed_bar_events_batch()
  ↓ _load_bars() ← N 根完整收盘 K 线
  ↓ 全部 21 个已启用指标
  ↓ publish_snapshot(scope="confirmed")
  ↓ SignalRuntime._confirmed_events 队列（2048，优先消费，不可丢弃）
  ↓ scope="confirmed" 的策略评估
```

### Intrabar 指标集合：自动推导，单一来源

Intrabar 计算哪些指标完全由策略的 `preferred_scopes` + `required_indicators` 在启动时自动推导：

```
intrabar 指标集合 = 所有满足以下条件的指标并集：
    "intrabar" ∈ strategy.preferred_scopes  AND  该指标 ∈ strategy.required_indicators
```

**推导流程（`src/api/factories/signals.py` 启动时执行）**：
```
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
| `donchian20` | breakout_double_confirm | 当前 bar 只能扩大通道，盘中值单调可信 |
| `adx14` | breakout_double_confirm | ADX 变化缓慢，盘中值与收盘差距极小 |

其余 13 个 enabled 指标（sma20, ema9/21/50/55, hma20, rsi5, macd, macd_fast, roc12, supertrend14, atr14, stoch14）仅在 confirmed 链路计算——因为使用它们的策略全部是 `preferred_scopes = ("confirmed",)`。

> **compute_mode 说明**：与链路无关，是同一链路内的计算方式。
> - `standard`：每次重算全部 N 根 bar（精确）
> - `incremental`：EMA/ATR 递推，只更新最新值（高效，对历史数据不敏感）

> **性能收益**：从 21 个指标全量 intrabar 压缩到 8 个，M1 每次 tick（约 1.2s）节省 13 次计算。

### 策略：preferred_scopes（唯一配置入口）

`SignalRuntime` 收到指标快照时，根据 `scope` 字段匹配策略的 `preferred_scopes`，**不匹配直接跳过**，不计算也不产生信号。

#### Confirmed-Only 策略（13 个）
仅在 K 线收盘时评估，使用完整历史收盘数据，信号稳定，适合趋势确认和突破判断。

| 策略名 | 所需指标（真实名称）| 场景 |
|--------|-------------------|------|
| `sma_trend` | sma20, ema50 | 均线金死叉趋势跟踪 |
| `macd_momentum` | macd | MACD 柱状图动量 |
| `supertrend` | supertrend14, adx14 | Supertrend + ADX 趋势过滤 |
| `ema_ribbon` | ema9, hma20, ema50 | EMA 多均线排列 |
| `hma_cross` | hma20, ema50 | HMA/EMA 交叉 |
| `roc_momentum` | roc12, adx14 | 变动率动量 |
| `donchian_breakout` | donchian20, adx14 | 唐奇安通道突破 |
| `fake_breakout` | donchian20, atr14 | 假突破识别反手 |
| `squeeze_release` | boll20, keltner20, macd | 布林带收缩释放 |
| `multi_timeframe_confirm` | sma20, ema50 | 跨时间框架确认 |
| `session_momentum` | atr14, supertrend14 | 时段动量偏差 |
| `price_action_reversal` | atr14 | 针形/吞没/内包K线 |
| `trend_triple_confirm` | supertrend14, ema9, hma20, ema50, macd | 三重趋势确认（复合）|

#### Intrabar + Confirmed 策略（7 个）
两条链路都接收，盘中可以提前预警（preview 状态机），收盘时产生确认信号。

| 策略名 | 所需指标（真实名称）| 场景 |
|--------|-------------------|------|
| `rsi_reversion` | rsi14 | RSI 超买超卖回归 |
| `stoch_rsi` | stoch_rsi14 | StochRSI 极值反转 |
| `williams_r` | williamsr14 | Williams %R 超卖买入 |
| `cci_reversion` | cci20 | CCI 极值均值回归 |
| `bollinger_breakout` | boll20 | 布林带触边突破 |
| `keltner_bb_squeeze` | boll20, keltner20 | BB/KC 挤压形态 |
| `breakout_double_confirm` | boll20, donchian20, adx14 | 双通道确认突破（复合：BollingerBreakout + DonchianBreakout）|

### 各类指标的语义分析

不同指标在未收盘的 bar 上计算是否有意义，是策略 `preferred_scopes` 选择的语义依据：

| 指标类别 | 代表指标 | 盘中语义 | 适合 intrabar |
|---------|---------|---------|:------------:|
| 移动均线（MA/EMA/HMA） | sma20, ema9/50, hma20 | 当前 bar 未收盘时"close"是最新 tick 价，均线会随 tick 频繁波动，无收盘意义 | **No** |
| 趋势跟踪（Supertrend） | supertrend14 | 基于 ATR 的价格通道，收盘前方向可频繁翻转，给出假信号 | **No** |
| 动量趋势（MACD/ROC） | macd, macd_fast, roc12 | 以 EMA 为基础，同样受未收盘价格噪声影响；且使用者全为 confirmed-only 策略 | **No** |
| 振荡器（RSI/CCI/Williams/StochRSI） | rsi14, cci20, williamsr14, stoch_rsi14 | 超买超卖是**实时状态**，盘中触极值比收盘才知道更有价值，越早预警越好 | **Yes** |
| 波动率通道（Bollinger/Keltner） | boll20, keltner20 | 价格盘中触及/突破通道边界本身就是信号，无需等待收盘确认 | **Yes** |
| 趋势通道（Donchian） | donchian20 | 最高/最低价通道：当前 bar 只能**扩大**通道（不会收窄），盘中值单调可信 | **Yes** |
| 趋势强度（ADX） | adx14 | ADX 变化缓慢，盘中值与收盘值差距极小，可信；且 breakout_double_confirm 需要 | **Yes** |
| 波动率基准（ATR） | atr14 | 消费方（sizing/fake_breakout）全在 confirmed 时执行；keltner20 内部自行计算 ATR | **No** |

---

## 新增指标/策略的 Intrabar 决策指引

### 核心原则：单一来源，自动推导

Intrabar 指标集合**不需要手动配置**。系统在启动时自动从策略声明推导：

```
策略的 preferred_scopes 包含 "intrabar"
    → 该策略的 required_indicators 全部加入 intrabar 计算集合
    → SignalModule.intrabar_required_indicators() 汇总所有策略
    → build_signal_components() 注入 indicator_manager.set_intrabar_eligible_override()
    → UnifiedIndicatorManager 在 intrabar 链路中仅计算这些指标
```

**因此，新增指标或策略时只需关注一个配置点：策略的 `preferred_scopes`。**

### 新增指标的步骤

1. 在 `src/indicators/core/` 中实现函数（签名：`func(bars, params) → dict`）
2. 在 `config/indicators.json` 中添加条目
3. **`dependencies` 一律填 `[]`**——当前所有指标函数直接接收 bars，不消费其他指标的输出。错误填写依赖会导致 pipeline 递归拉入额外指标进行 intrabar 计算，造成隐性浪费
4. 无需设置任何 intrabar 相关字段——由消费该指标的策略自动决定

### 新增策略的决策树

```
新策略
  │
  ├─ 策略逻辑是否依赖"盘中实时状态"？
  │   （超买超卖 / 通道触边 / 实时动量极值）
  │
  ├─ YES → preferred_scopes = ("intrabar", "confirmed")
  │         系统自动将 required_indicators 加入 intrabar 计算集合
  │         无需修改 indicators.json
  │
  └─ NO  → 策略是否需要"K 线完整形态"才能确认信号？
            （均线交叉 / 趋势方向 / 价格行为形态 / 通道站稳）
            → preferred_scopes = ("confirmed",)
              该策略的指标不会参与 intrabar 计算（除非被其他 intrabar 策略也引用）
```

### 各类策略的经验判断

| 策略类型 | preferred_scopes | 代表指标 | 原因 |
|---------|:---------------:|---------|------|
| 均线交叉（MA Cross） | confirmed | sma/ema/hma | 均线需要收盘价定型，盘中值噪声大 |
| 趋势跟踪（Supertrend/ROC） | confirmed | supertrend14/roc12 | 趋势方向收盘才稳定 |
| MACD 动量 | confirmed | macd | EMA 底层，收盘前频繁变动 |
| 价格行为（K 线形态） | confirmed | atr14 | 形态必须 K 线收盘才能确认完整 |
| 时段动量 | confirmed | atr14/supertrend14 | 基于已收盘 K 线统计规律 |
| **RSI/CCI/Williams/StochRSI 均值回归** | **intrabar + confirmed** | rsi14/cci20/williamsr14/stoch_rsi14 | 超买超卖是实时状态，盘中触值即可预警 |
| **Bollinger 触边突破** | **intrabar + confirmed** | boll20 | 价格触及/突破通道边界不需要等收盘 |
| **Keltner 挤压** | **intrabar + confirmed** | boll20/keltner20 | BB 完全在 KC 内是实时状态 |
| Donchian 突破（需站稳） | confirmed | donchian20 | 需收盘确认已站稳通道外，防假突破 |

### 复合策略的 preferred_scopes

复合策略的 scope 由**最宽松的子策略决定**：
- 所有子策略都是 confirmed-only → 复合策略 `preferred_scopes = ("confirmed",)`
- 有任意一个子策略需要 intrabar → 复合策略 `preferred_scopes = ("intrabar", "confirmed")`
- `breakout_double_confirm`：BollingerBreakout（intrabar） + DonchianBreakout（confirmed） → 取宽松 → `("intrabar", "confirmed")`

### Standalone 模式兼容

当 `UnifiedIndicatorManager` 独立运行（无 `SignalModule`，如纯 API 查询场景）时，`set_intrabar_eligible_override()` 不会被调用，`_get_intrabar_eligible_names()` 返回空集，即不进行 intrabar 指标计算。

---

## Signal Strategies（当前注册列表）

### 趋势跟踪（Trend）— `src/signals/strategies/trend.py`

| 策略名 | 所需指标 | Scope |
|--------|---------|-------|
| `sma_trend` | sma20, ema50 | confirmed |
| `macd_momentum` | macd | confirmed |
| `supertrend` | supertrend14, adx14 | confirmed |
| `ema_ribbon` | ema9, hma20, ema50 | confirmed |
| `hma_cross` | hma20, ema50 | confirmed |
| `roc_momentum` | roc12, adx14 | confirmed |

### 均值回归（Mean Reversion）— `src/signals/strategies/mean_reversion.py`

| 策略名 | 所需指标 | Scope |
|--------|---------|-------|
| `rsi_reversion` | rsi14 | intrabar + confirmed |
| `stoch_rsi` | stoch_rsi14 | intrabar + confirmed |
| `williams_r` | williamsr14 | intrabar + confirmed |
| `cci_reversion` | cci20 | intrabar + confirmed |

### 突破/波动率（Breakout）— `src/signals/strategies/breakout.py`

| 策略名 | 所需指标 | Scope |
|--------|---------|-------|
| `bollinger_breakout` | boll20 | intrabar + confirmed |
| `keltner_bb_squeeze` | boll20, keltner20 | intrabar + confirmed |
| `donchian_breakout` | donchian20, adx14 | confirmed |
| `multi_timeframe_confirm` | sma20, ema50 | confirmed |
| `fake_breakout` | donchian20, atr14 | confirmed |
| `squeeze_release` | boll20, keltner20, macd | confirmed |

### 时段动量（Session）— `src/signals/strategies/session.py`

| 策略名 | 所需指标 | Scope |
|--------|---------|-------|
| `session_momentum` | atr14, supertrend14 | confirmed |

### 价格行为（Price Action）— `src/signals/strategies/price_action.py`

| 策略名 | 所需指标 | Scope |
|--------|---------|-------|
| `price_action_reversal` | atr14 | confirmed |

### 复合策略（Composite）— `src/signals/strategies/composite.py`

| 策略名 | 子策略 | Scope | 模式 |
|--------|--------|-------|------|
| `trend_triple_confirm` | supertrend + ema_ribbon + macd_momentum | confirmed | all_agree |
| `breakout_double_confirm` | bollinger_breakout + donchian_breakout | intrabar + confirmed | all_agree |

---

## Voting Engine Architecture

`StrategyVotingEngine` (`src/signals/orchestration/voting.py`) 支持两种模式：

### 单 consensus 模式（默认向后兼容）
- 所有策略参与投票 → 产生 `strategy="consensus"` 信号
- 通过 `SignalPolicy.voting_enabled=True` 且 `voting_groups=[]` 启用

### 多 voting group 模式
- 每个 group 有独立的成员策略集合和阈值
- 产生以 group.name 命名的信号（如 `strategy="trend_vote"`）
- 通过 `SignalPolicy.voting_groups=[VotingGroupConfig(...)]` 配置
- 多组模式启用时，全局 consensus 自动禁用

**表决算法**：
```
buy_score  = Σ confidence(buy)  / Σ all confidence
sell_score = Σ confidence(sell) / Σ all confidence
→ 达到 consensus_threshold（默认 0.40）时产生信号
→ disagreement_factor 减少置信度，惩罚多空分歧
```

---

## Key Algorithms（XAUUSD 日内优化）

### TimeframeScaler（`src/signals/strategies/base.py`）

按时间框架自动缩放策略阈值和周期，减少 M1/M5 与 H1 参数失配：

```python
SCALE_FACTORS = {"M1": 0.60, "M5": 0.75, "M15": 0.85, "H1": 1.00, "H4": 1.15, "D1": 1.30}
# scaler.scale_period(14) → M1 下返回 8, M5 下返回 10
# scaler.scale_threshold(25.0) → M1 下返回 15.0
```

### Indicator Delta（`src/indicators/result_store.py`）

核心动量指标自动计算 N-bar 变化率（一阶导数），通过 `indicators.json` 的 `delta_bars` 字段配置：
- 覆盖指标：`rsi14`, `macd`, `cci20`, `stochrsi`, `adx14`
- 输出：`{"rsi": 28.5, "rsi_d3": -8.2, "rsi_d5": -12.0}`

### Market Structure Cache（`src/signals/orchestration/runtime.py`）

- confirmed scope：重新计算市场结构分析
- intrabar scope：复用上一次 confirmed 的分析结果（避免重复计算）
- M1 lookback_bars = 120（2 小时），M5+ lookback_bars = 400

---

## Adding New Signal Strategies（SOP）

每个信号策略必须完整声明四类属性才算合格：

```python
class MyNewStrategy:
    # 1. 唯一名称（用于注册、日志、API）
    name = "my_new_strategy"

    # 2. 所需指标（对应 config/indicators.json 中的 name 字段）
    required_indicators = ("adx14", "boll20")

    # 3. 接收快照的 scope
    #    "confirmed" = 仅 bar 收盘（趋势/突破策略）
    #    "intrabar"  = 盘中实时（均值回归类）
    preferred_scopes = ("confirmed",)

    # 4. Regime 亲和度（必填，不可省略）
    regime_affinity = {
        RegimeType.TRENDING:  0.XX,
        RegimeType.RANGING:   0.XX,
        RegimeType.BREAKOUT:  0.XX,
        RegimeType.UNCERTAIN: 0.XX,
    }
```

#### Regime 亲和度设计指南

| 策略类型 | TRENDING | RANGING | BREAKOUT | UNCERTAIN |
|---------|---------|---------|---------|---------|
| 趋势跟踪（MA Cross、Supertrend、EMA Ribbon）| 1.00 | 0.1–0.3 | 0.4–0.6 | 0.5 |
| 均值回归（RSI、StochRSI）| 0.2–0.3 | 1.00 | 0.3–0.4 | 0.6 |
| 突破/波动率（Donchian、BB Squeeze、Keltner）| 0.3–0.9 | 0.15–0.55 | 1.00 | 0.45–0.65 |

#### 完整新增步骤

1. 在对应文件中实现策略类：
   - 趋势跟踪 → `src/signals/strategies/trend.py`
   - 均值回归 → `src/signals/strategies/mean_reversion.py`
   - 突破/波动率 → `src/signals/strategies/breakout.py`
   - 时段动量 → `src/signals/strategies/session.py`
   - 价格行为 → `src/signals/strategies/price_action.py`
   - 组合策略 → `src/signals/strategies/composite.py`
2. 在 `src/signals/strategies/__init__.py` 中添加导出
3. 在 `src/signals/service.py` 的默认策略列表中注册实例
4. 在 `tests/signals/` 中添加单元测试，覆盖四种 Regime 下的输出
5. （可选）在 `config/signal.ini` 中调整参数

> **注意**：
> - `SignalStrategy` Protocol 定义在 `src/signals/strategies/base.py`
> - 策略注册表在 `src/signals/strategies/registry.py`（不通过 `__init__.py` 导出，避免循环导入）
> - 注册时 `SignalModule._validate_strategy_attrs()` 自动校验四个必填属性

#### Regime 分类逻辑参考（`src/signals/evaluation/regime.py`）

**硬分类**（`detect()`）：
```
优先级：
  1. Keltner-Bollinger Squeeze（BB完全在KC内）→ BREAKOUT
  2. ADX ≥ 23 → TRENDING
  3. ADX < 18 且 BB宽度 < 0.8% → BREAKOUT（蓄力盘整）
  4. ADX < 18 → RANGING
  5. 18 ≤ ADX < 23 → UNCERTAIN
  6. 无指标数据 → UNCERTAIN（兜底）
```

**概率化分类**（`detect_soft()` — 通过 `soft_regime_enabled` feature flag 控制）：
```python
SoftRegimeResult:
    primary: RegimeType                    # 向后兼容主分类
    probabilities: dict[RegimeType, float]  # 概率分布，总和=1.0
# 策略 affinity 改为加权平均：effective_affinity = Σ(prob[r] × affinity[r])
```

---

## Confidence Pipeline

每个策略的置信度经过以下修正流程：

```
raw_confidence（策略规则输出）
    × effective_affinity               (结构性过滤)
    → post_affinity_confidence
    × session_performance_multiplier   (日内实时状态) ← StrategyPerformanceTracker
    → ConfidenceCalibrator             (长期统计校准) ← Calibrator
    = final_confidence
```

**effective_affinity 计算**：
- 硬分类模式：`affinity[current_regime]`
- Soft Regime 模式：`Σ(prob[r] × affinity[r])` — 加权平均，消除阈值边界跳变

**日内策略绩效追踪**（`StrategyPerformanceTracker`，`src/signals/evaluation/performance.py`）：
- 纯内存的日内实时反馈层，接收 OutcomeTracker / PositionManager 的结果回调
- 维护 per-strategy 滚动统计（wins, losses, streak, PnL）
- 维护 per-(strategy, regime) 维度统计，支持按当前 regime 查询乘数
- 提供 `get_multiplier(strategy, regime=None) → [min_multiplier, max_multiplier]`
- regime 维度样本充足时：regime 0.7 + 全局 0.3 混合；不足时按比例平滑过渡
- Session 边界自动重置
- 乘数基于三因素加权：session_win_rate vs baseline、streak 连胜/连败、profit_factor（avg_win / avg_loss）
- 当单策略样本不足（< `category_fallback_min_samples`）时，回退到同类策略的聚合绩效（Category 聚合）

**分阶段置信度校准**（`ConfidenceCalibrator`）：
- 0–50 笔交易：`alpha = 0.0`（纯规则，不校准）
- 50–100 笔：`alpha = 0.10`（轻微校准）
- 100+ 笔：`alpha = 0.15`（正常校准）
- `recency_hours = 8`（仅用近 8 小时数据校准）

**Signal Fusion**：同一 bar 内 intrabar + confirmed 同向信号在 VotingEngine 前去重（`_fuse_vote_decisions()`）

- `final_confidence < min_preview_confidence（默认 0.55）`：SignalRuntime 不产生状态转换
- `min_affinity_skip（默认 0.15）`：affinity 低于此值的策略在进入 evaluate() 前直接跳过（节省 CPU）

---

## Signal State Machine

每个 `(symbol, timeframe, strategy)` 三元组独立维护 `RuntimeSignalState`：

**Intrabar 路径（Preview 状态机）**：
```
idle → preview_buy/sell（方向改变）
     → armed_buy/sell（方向稳定 ≥ min_preview_stable_seconds，默认 15s）
     → idle（置信度下降 → cancelled）
```

**Confirmed 路径（K 线收盘）**：
```
任意 → confirmed_buy/sell（收盘时方向为 buy/sell）
      → confirmed_cancelled（上一根有信号，本根转 hold）
      → idle（无信号 hold）
```

---

## Risk Management Layers

交易请求从内到外经过多层校验：

1. **Signal filters** (`src/signals/execution/filters.py`)：经济事件过滤、价差过滤、交易时段过滤、**时段切换冷却期**（`SessionTransitionFilter`）
2. **Pre-trade risk service** (`src/risk/service.py`)：账户级别检查
3. **Risk rules** (`src/risk/rules.py`)：仓位限制、最大手数、SL/TP 要求、**日损失限制**（`DailyLossLimitRule`）
4. **Trade guard** (`src/calendar/economic_calendar/trade_guard.py`)：高风险经济事件窗口内阻止交易
5. **Executor safety** (`src/trading/signal_executor.py`)：**日内头寸限制**（`max_concurrent_positions_per_symbol`）、HTF 软惩罚（冲突 ×0.70 / 对齐 ×1.10）
6. **Position manager** (`src/trading/position_manager.py`)：**日终自动平仓**（UTC 21:00，可配置开关）
7. **Sizing** (`src/trading/sizing.py`)：**时间框架差异化 SL/TP**（M1: 1.0/2.0, M5: 1.2/2.5, M15: 1.3/2.8, H1: 1.5/3.0 ATR 倍数）

---

## API Endpoints

Base URL: `http://<host>:8808`（默认）

Authentication: `X-API-Key` 请求头（在 `config/market.ini` 中配置）

| Router | Prefix | 主要端点 |
|--------|--------|---------|
| market | `/` | `/symbols`, `/quote`, `/ticks`, `/ohlc`, `/stream`（SSE） |
| trade | `/` | `/trade`, `/close`, `/close_all`, `/trade/precheck`, `/estimate_margin` |
| account | `/account` | `/account/info`, `/account/positions`, `/account/orders` |
| economic | `/economic` | `/economic/calendar`, `/economic/calendar/risk-windows`, `/economic/calendar/trade-guard` |
| indicators | `/indicators` | `/indicators/list`, `/indicators/{symbol}/{timeframe}`, `/indicators/compute` |
| monitoring | `/monitoring` | `/monitoring/health`, `/monitoring/startup`, `/monitoring/queues`, `/monitoring/config/effective` |
| signal | `/signal` | 信号查询、诊断、策略列表、投票信息、**`/monitoring/quality/{symbol}/{timeframe}`**（Regime + 信号质量） |

响应通用包装器：
```python
ApiResponse[T]:
    success: bool
    data: T | None
    error: str | None
    error_code: str | None     # AIErrorCode 枚举
    metadata: dict | None
```

---

## Adding New API Routes

1. 在 `src/api/` 中创建路由文件
2. 在 `src/api/schemas.py` 中添加 Pydantic 请求/响应 Schema
3. 在 `src/api/__init__.py` 中注册路由
4. 在 `src/api/error_codes.py` 中添加错误码

---

## Registered Indicators（当前 25 个）

| 名称 | 类型 | delta_bars | 说明 |
|------|------|-----------|------|
| `sma20` | 均线 | — | 20 周期简单均线 |
| `ema9` | 均线 | — | 9 周期指数均线 |
| `ema21` | 均线 | — | 21 周期指数均线 |
| `ema50` | 均线 | — | 50 周期指数均线 |
| `ema55` | 均线 | — | 55 周期指数均线 |
| `hma20` | 均线 | — | 20 周期 Hull MA |
| `wma20` | 均线 | — | 20 周期加权均线（当前无策略依赖，`enabled:false`） |
| `rsi5` | 动量 | — | 5 周期 RSI |
| `rsi14` | 动量 | [3,5] | 14 周期 RSI（含 delta）|
| `macd` | 动量 | [3,5] | 标准 MACD（含 delta）|
| `macd_fast` | 动量 | — | 快速 MACD（短周期）|
| `roc12` | 动量 | — | 12 周期变动率 |
| `adx14` | 趋势强度 | [3,5] | ADX + DI（含 delta）|
| `supertrend14` | 趋势 | — | 14 周期 Supertrend |
| `atr14` | 波动率 | — | 14 周期 ATR |
| `boll20` | 波动率 | — | 20 周期布林带 |
| `keltner20` | 波动率 | — | 20 周期 Keltner 通道 |
| `donchian20` | 通道 | — | 20 周期 Donchian 通道 |
| `cci20` | 震荡 | [3,5] | 20 周期 CCI（含 delta）|
| `stoch14` | 震荡 | — | 14 周期随机指标 |
| `stoch_rsi14` | 震荡 | [3,5] | 14 周期 StochRSI（含 delta）|
| `williamsr14` | 震荡 | — | 14 周期 Williams %R |
| `mfi14` | 成交量 | — | 14 周期 MFI（Demo 账户 `enabled:false`）|
| `obv30` | 成交量 | — | 30 周期 OBV（Demo 账户 `enabled:false`）|
| `vwap30` | 成交量 | — | 30 周期 VWAP（Demo 账户 `enabled:false`） |

> Demo 账户无真实成交量，`mfi14`/`obv30`/`vwap30` 默认 `enabled: false`。`wma20` 因当前无策略依赖也 `enabled: false`。

## Adding New Indicators

1. 在 `src/indicators/core/` 中实现函数（参考 `mean.py`、`momentum.py` 等现有模式）
2. 在 `config/indicators.json` 中添加条目，填写 `func_path`、`params`、`dependencies`、`compute_mode`
   - 可选 `delta_bars` 字段（如 `[3, 5]`）：自动计算指标 N-bar 变化率（一阶导数），输出附加 `{key}_d3`、`{key}_d5` 字段
   - 可选 `enabled: false`：禁用指标但保留配置（如 MFI、OBV 在 Demo 账户下因无真实成交量被禁用）
3. 在 `tests/indicators/` 中添加测试

---

## Code Conventions

### Type Safety

- **mypy strict mode** 强制执行，所有函数必须有类型注解
- 不得在无充分理由的情况下使用 `Any`
- 所有数据边界（API 请求/响应、配置）使用 Pydantic 模型

### Pydantic Version

使用 **Pydantic v2**（`>=2.6.0`）：
- `model_validate()` 而非 `parse_obj()`
- `model_dump()` 而非 `dict()`
- `model_config = ConfigDict(...)` 而非 `class Config:`

### Error Handling

- 使用类型化错误码（`src/api/error_codes.py` 中的 `AIErrorCode` 枚举）
- 路由处理器始终返回 `ApiResponse[T]` 包装器
- 使用 `structlog` 结构化日志，不使用 `logging`（路由层）

### Thread Safety

- `MarketDataService` 使用 `RLock`——读写均须持锁
- `StorageWriter` 队列线程安全——只 put 数据
- `SignalRuntime` 使用分片锁（按 symbol/timeframe 分片）防止全局锁争用
- 不得在无锁的情况下跨线程共享可变状态

### Configuration Access

```python
from src.config import get_trading_config, get_api_config, get_db_config
# 不要直接解析 .ini 文件
```

### Dependency Injection

向 `src/api/deps.py` 添加新服务组件：
- 在 `_Container` 类中添加字段（`Optional[YourService] = None`）
- 在 `src/api/factories/` 中添加对应工厂函数
- 在 `src/api/lifespan.py` 的 `shutdown_components()` 中注册关闭逻辑
- 通过 `FastAPI.Depends()` 在路由处理器中暴露

---

## Testing Conventions

### Test Structure

```
tests/api/          → src/api/
tests/config/       → src/config/
tests/indicators/   → src/indicators/
tests/signals/      → src/signals/
tests/trading/      → src/trading/
tests/integration/  → 端到端流程
tests/smoke/        → 基础冒烟测试
```

### Test Markers

```python
@pytest.mark.unit
@pytest.mark.integration
@pytest.mark.slow
```

---

## Development Workflows

### Running the Service

```bash
python app.py
# 或
uvicorn src.api:app --host 0.0.0.0 --port 8808 --reload
```

### Running Tests

```bash
pytest                            # 全部测试
pytest -m unit                    # 仅单元测试
pytest -m "not slow"              # 跳过慢速测试
pytest --cov=src --cov-report=html
```

### Code Quality

```bash
black src/ tests/
isort src/ tests/
mypy src/
flake8 src/ tests/
```

---

## Known Issues & Notes

- **CORS**：通配符 origin（`*`）与 `allow_credentials=True` 不兼容（已在 `src/api/__init__.py` 修复）
- **MT5 Python 绑定**：仅支持 Windows。需要 MT5 的测试必须在 Windows 上运行或使用 Mock
- **TimescaleDB**：首次启动前需确保 PostgreSQL 已安装 TimescaleDB 扩展
- **`get_signal_config()` 已知 bug**：`src/config/centralized.py` 中调用了不存在的 `ConfigValidator.validate_model()`，直接调用会抛出 `AttributeError`。信号配置通过兼容层加载，暂不受影响
- **Config snapshot at import time**：`src/api/__init__.py` 在导入时一次性读取 API 配置，修改 `market.ini` 后需重启
- **HTF cache TTL 不可配置**：当前固定 4 小时，应从 `signal.ini` 读取
- **Soft Regime feature flag**：`soft_regime_enabled` 默认关闭，启用前建议在 paper trading 验证
- **模块位置注意**：
  - `SignalRuntime` → `src/signals/orchestration/runtime.py`（不在 `src/signals/runtime.py`）
  - `MarketDataService` → `src/market/service.py`（不在 `src/core/market_service.py`）
  - `EconomicCalendarService` → `src/calendar/service.py`（不在 `src/core/economic_calendar_service.py`）
  - `TimeframeScaler` → `src/signals/strategies/base.py`（与 `SignalStrategy` Protocol 同文件）
  - `SessionTransitionFilter` → `src/signals/execution/filters.py`（与 `SignalFilterChain` 同文件）
  - `DailyLossLimitRule` → `src/risk/rules.py`（与其他 Risk Rules 同文件）
  - `StrategyPerformanceTracker` → `src/signals/evaluation/performance.py`（与 Calibrator 平级，不在 trading 模块）
  - `TradeAPIDispatcher` → `src/api/trade_dispatcher.py`（独立文件，不在路由文件中）
  - `UnifiedIndicatorSourceAdapter` → `src/signals/strategies/adapters.py`（解耦信号与指标的适配器）

---

## File Naming Conventions

| 模式 | 规范 |
|------|------|
| 源码模块 | `snake_case.py` |
| 配置文件 | `module_name.ini` |
| 本地覆盖 | `module_name.local.ini`（已被 .gitignore） |
| 测试文件 | `test_<module>.py` |

---

## Git Workflow

- 默认分支：`main`
- 开发分支：`claude/<feature>-<id>` 或描述性功能分支
- 禁止提交密钥——凭据存放在 `*.local.ini`（已被 .gitignore）
- 提交代码变更前运行 `black`、`isort`、`mypy`、`flake8`
