# CLAUDE.md — MT5Services Codebase Guide

This file is the primary reference for AI assistants working on MT5Services. It covers architecture, conventions, workflows, and rules to follow when making changes.

---

## Language Preference

**请始终使用中文回复。** All responses, explanations, code comments suggestions, and conversations must be in Chinese (Simplified). Code itself (variable names, function names, string literals in source files) follows the existing conventions of each file.

---

## Working Principles

You must reason from first principles. Start from the real objective, constraints, and root problem rather than conventions, templates, or the user's proposed path.

Rules:
1. If the user's goal, motivation, or success criteria are unclear, pause and clarify before proposing implementation.
2. If the user's requested path is not the shortest or most effective path, say so directly and propose a better one.
3. Always prefer root-cause analysis over surface-level fixes. Every non-trivial recommendation must answer: why this decision?
4. Keep outputs decision-focused. Include only information that changes action, architecture, risk, or tradeoffs.
5. Do not optimize for agreement. Optimize for correctness, clarity, and leverage.
6. If assumptions are necessary, state them explicitly and minimize them.

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
│   ├── risk.ini              # 风险限制（仓位、SL/TP、保证金、交易频率）
│   ├── cache.ini             # 运行时内存缓存大小（覆盖 app.ini [limits]）
│   ├── signal.ini            # 信号模块配置（含 HTF 缓存、HTF 指标注入、信号质量追踪器参数）
│   ├── indicators.json       # 指标定义与计算流水线
│   └── composites.json       # 复合策略组合定义
├── src/
│   ├── app_runtime/          # 应用运行时（从 deps.py 拆分）
│   │   ├── container.py      # AppContainer：纯组件持有（flat dataclass，无逻辑）
│   │   ├── builder.py        # build_app_container()：构建所有组件（不启动线程）
│   │   └── runtime.py        # AppRuntime：start/stop 生命周期管理
│   ├── api/                  # FastAPI 路由、中间件、Schema、DI 适配层
│   │   ├── admin.py          # 管理面板路由（仪表盘/配置/策略/SSE 事件流）
│   │   ├── admin_schemas.py  # Admin API 响应 Schema
│   │   ├── factories/        # 组件工厂函数（market/storage/trading/signals/indicators）
│   │   └── trade_dispatcher.py # TradeAPIDispatcher 统一交易调度入口
│   ├── calendar/             # 经济日历服务
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
│   │   ├── delta_metrics.py      # N-bar 变化率计算（纯函数，可复用于 backtest）
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
│   ├── market/               # MarketDataService
│   ├── market_structure/     # 市场结构分析器（MarketStructureAnalyzer）
│   │   └── models.py         # MarketStructureContext 数据类
│   ├── monitoring/           # 健康检查
│   │   ├── health_monitor.py # SQLite 指标存储、告警
│   │   ├── health_checks.py  # 健康检查实现
│   │   ├── health_common.py  # 健康检查通用工具
│   │   ├── health_reporting.py # 健康报告生成
│   │   ├── pipeline_event_bus.py # 指标管线事件总线（计算耗时/异常事件分发）
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
│   │   ├── confidence.py     # 置信度管线纯函数（apply_confidence_pipeline）
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
│   │   ├── orchestration/    # SignalRuntime / SignalPolicy / VotingEngine / 纯函数模块
│   │   ├── strategies/       # 策略实现（trend/mean_reversion/breakout/composite/multi_tf）
│   │   │   ├── adapters.py   # UnifiedIndicatorSourceAdapter（解耦信号与指标）
│   │   │   ├── multi_tf_entry.py # HTFTrendM5Entry：H1 定方向 + M5 回调入场联动策略
│   │   │   └── tf_params.py  # TFParamResolver：per-TF 策略参数查表器
│   │   └── tracking/         # SignalRepository（信号持久化与查询）
│   ├── trading/              # TradingModule、TradeExecutor、ExecutionGate、PositionManager、SignalQualityTracker、TradeOutcomeTracker
│   │   ├── execution_gate.py # ExecutionGate：策略域准入检查（voting group / 白名单 / armed）
│   │   ├── pending_entry.py  # PendingEntryManager：价格确认入场（Quote bid/ask 区间监控）
│   │   └── models.py         # TradeOperationRecord 数据类
│   ├── backtesting/          # 回测与参数优化子系统
│   │   ├── economic_provider.py   # 回测用经济日历 TradeGuard 提供者（DB 预加载 + 内存查询）
│   │   ├── engine.py             # BacktestEngine：逐 bar 回放主循环
│   │   ├── optimizer.py          # 网格/随机参数搜索
│   │   ├── walk_forward.py       # Walk-Forward 前推验证（IS/OOS 分割）
│   │   ├── recommendation.py     # 参数推荐引擎 + ConfigApplicator（生成/应用/回滚）
│   │   ├── component_factory.py  # 构建独立 pipeline/SignalModule（绕过 deps.py）
│   │   ├── data_loader.py        # CachedDataLoader：从 DB 加载历史 OHLC
│   │   ├── portfolio.py          # PortfolioTracker：模拟持仓/SL/TP/资金曲线
│   │   ├── filters.py            # 过滤器模拟（直接复用 SignalFilterChain）
│   │   ├── metrics.py            # Sharpe/Sortino/Calmar/最大回撤计算
│   │   ├── models.py             # BacktestResult/Recommendation/ParamChange 数据类
│   │   └── api.py                # 回测 HTTP API（run/optimize/walk-forward/recommendations）
│   ├── readmodels/           # Read-model 投影层（运行时状态聚合）
│   │   └── runtime.py        # RuntimeReadModel：仪表盘/指标/交易/信号运行时摘要投影
│   ├── studio/               # Studio 可观测性层（Anteater 3D 前端实时状态）
│   │   ├── models.py         # Agent 元数据注册表（14 角色）+ build_agent/build_event 辅助函数
│   │   ├── event_buffer.py   # 线程安全环形事件缓冲区（deque + Lock）
│   │   ├── mappers.py        # 14 个纯映射函数（模块 status dict → StudioAgent dict，纯数据不含展示逻辑）
│   │   ├── runtime.py        # StudioRuntime：Studio 事件桥接与生命周期
│   │   └── service.py        # StudioService：Registry 模式 + SSE 订阅管理 + 事件广播
│   └── utils/                # 通用工具、内存管理器
│       └── timezone.py       # 统一时区模块（utc_now/to_display/LocalTimeFormatter）
├── tests/                    # 测试套件（镜像 src/ 结构）
└── docs/                     # 架构文档、内部设计文档
    └── architecture-flow.md  # 全链路流程图、数据流转矩阵、置信度管线路径
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
| `config/risk.ini` | 最大仓位数、SL/TP 要求、保证金安全系数、交易频率限制 |
| `config/cache.ini` | 运行时内存缓存大小（覆盖 app.ini [limits]，优先级更高） |
| `config/signal.ini` | 自动交易、仓位大小、过滤条件、状态机参数、HTF 缓存 TTL、信号质量追踪器、Regime 检测阈值、策略级参数覆盖、Regime 亲和度覆盖、**价格确认入场（Pending Entry）** |
| `config/indicators.json` | 指标定义、参数、依赖关系、流水线配置 |
| `config/composites.json` | 复合策略组合定义 |

### No `.env` Files

`.env` 文件已**废弃**。所有配置均在 `.ini` 文件中。`python-dotenv` 仅作为遗留兼容依赖保留。

### Signal 模块配置化体系（signal.ini 新增 sections）

信号模块的可调优参数通过多层配置覆盖，无需改动策略源码：

| Section | 格式 | 影响范围 | 说明 |
|---------|------|---------|------|
| `[regime_detector]` | `adx_trending_threshold = 23.0` | 全局 | Regime 分类阈值 |
| `[strategy_params]` | `<strategy>__<param> = value` | 单策略（全 TF 兜底） | 全局默认策略参数 |
| `[strategy_params.<TF>]` | `<strategy>__<param> = value` | 单策略 × 单 TF | **per-TF 策略参数覆盖** |
| `[regime_affinity.<name>]` | `trending = 1.00` | 单策略 | 覆盖策略的 Regime 亲和度权重 |

**Per-TF 策略参数**（`TFParamResolver` 架构）：

策略参数通过 `TFParamResolver` 查表器管理，支持按时间框架独立配置。

查找优先级：`[strategy_params.<TF>]` → `[strategy_params]` → 策略代码 default 参数

```ini
# 全局默认（所有 TF 兜底）
[strategy_params]
rsi_reversion__overbought = 78
supertrend__adx_threshold = 21

# M5 特化（回测优化产出）
[strategy_params.M5]
rsi_reversion__overbought = 72
rsi_reversion__oversold = 25

# H4 特化
[strategy_params.H4]
rsi_reversion__overbought = 75
```

**键格式**：双下划线 `__` 分隔策略名和参数名。
- 例：`supertrend__adx_threshold = 23.0`
- 策略在 `evaluate(context)` 中通过 `get_tf_param(self, "adx_threshold", context.timeframe, default)` 查表

**核心文件**：
- `src/signals/strategies/tf_params.py` — `TFParamResolver` + `build_tf_param_resolver()`
- `src/signals/strategies/base.py` — `get_tf_param()` 便利函数
- `src/config/signal.py` — 加载 `[strategy_params.<TF>]` section
- `src/app_runtime/factories/signals.py` — 构建 resolver 并注入策略实例

**覆盖时机**：`build_signal_components()` 构建 resolver 后注入；回测通过 `apply_param_overrides(strategy_params_per_tf=...)` 热更新。

---

## Architecture

### Startup Sequence

组件初始化分为两个阶段（见 `src/app_runtime/`）：

**构建阶段**（`build_app_container()` — 不启动线程）：
1. **MarketDataService** → **StorageWriter** → 连接 → **BackgroundIngestor** → **UnifiedIndicatorManager**
2. **EconomicCalendarService** → **TradingAccountRegistry** → **TradingModule**
3. **SignalModule** + 策略注册 → **SignalRuntime** → **TradeExecutor** → listener 连接
4. **HealthMonitor** → **MonitoringManager**
5. **StudioService**（聚合层）→ 注册 14 个 agent provider + SignalRuntime 信号 listener

**启动阶段**（`AppRuntime.start()` — 按序启动后台线程）：
1. StorageWriter.start() → IndicatorManager.start() → Ingestor.start()
2. EconomicCalendarService.start() → SignalRuntime.start()
3. Calibrator 热启动 → PerformanceTracker 热启动 → PendingEntryManager.start() → PositionManager.start()
4. MonitoringManager 注册组件 + start()

关闭顺序相反（信号源 → 执行层 → 数据层）。所有组件通过 `AppContainer`（`src/app_runtime/container.py`）持有，`src/api/deps.py` 仅作为 FastAPI DI 适配层暴露 getter 函数。

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
                         FilterChain（点差/时段/经济/波动率异常过滤）
                                       ↓
                         Regime 检测 → 快速全拒绝（所有策略 affinity 不足直接跳过）
                                       ↓
                         strategies 评估（含 HTF 置信度修正）
                                       ↓
                         VotingEngine（consensus / group 投票）
                                       ↓
                         ExecutionGate（准入检查）→ PendingEntryManager（Quote 价格确认）→ TradeExecutor → MT5
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

### Event Durability Levels

系统中所有事件按可靠性要求分为三个等级：

| 等级 | 要求 | 事件类型 | 当前实现 |
|------|------|---------|---------|
| **L1 (Durable)** | 必须持久化，不可丢失 | confirmed bar close、trade signal（state_changed=true）、order execution、risk block | `runtime_data_dir/events.db` SQLite + signal_events/trade_outcomes 表 + backpressure 重试 |
| **L2 (Recoverable)** | 允许丢失但可回放恢复 | indicator snapshot、reconcile 结果、OHLC 持久化 | StorageWriter 队列（block 策略）+ 定时 reconcile 兜底 |
| **L3 (Best-effort)** | 最佳努力，丢失可接受 | intrabar preview、调试快照、部分监控指标 | put_nowait 队列满则丢弃 + 日志记录丢弃数 |

**当前实现状态**：
- L1 事件已通过 SQLite event store（confirmed bar）和 DB 写入（signal/trade outcomes）保障
- L2 事件通过 StorageWriter 的 `block` 溢出策略 + IndicatorManager 的 `_reconcile_all()` 定时兜底保障
- L3 事件通过 `drop_oldest` / `drop_newest` 溢出策略 + 丢弃计数器监控

---

## Key Source Files

| 文件 | 职责 |
|------|------|
| `src/app_runtime/container.py` | `AppContainer`：纯组件持有（flat dataclass，`__slots__`，可多实例） |
| `src/app_runtime/builder.py` | `build_app_container()`：构建所有组件并连接依赖（不启动线程） |
| `src/app_runtime/runtime.py` | `AppRuntime`：start/stop 生命周期管理（启动/关闭所有后台线程） |
| `src/api/deps.py` | FastAPI DI 适配层（getter 函数 + lifespan，委托 `app_runtime`） |
| `src/utils/timezone.py` | 统一时区模块：`utc_now()`/`to_display()`/`LocalTimeFormatter`（日志北京时间） |
| `src/market/event_bus.py` | `MarketEventBus`：事件订阅/分发（从 MarketDataService 提取） |
| `src/signals/orchestration/htf_resolver.py` | HTF 配置解析、指标查询、对齐乘数计算（纯函数） |
| `src/signals/orchestration/state_machine.py` | 状态机转换 preview/armed/confirmed（纯逻辑，可独立测试） |
| `src/signals/orchestration/vote_processor.py` | 投票处理：fusion/emit/process_voting（纯函数） |
| `src/signals/orchestration/affinity.py` | Regime 亲和度计算 + 快速拒绝检查（纯函数） |
| `src/indicators/delta_metrics.py` | N-bar 变化率计算（纯函数，可复用于 backtest） |
| `src/app_runtime/factories/` | 各组件工厂函数（market/storage/trading/signals/indicators） |
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
| `src/signals/execution/filters.py` | SignalFilterChain：点差/时段/经济事件/**时段切换冷却**/**波动率异常**过滤 |
| `src/signals/tracking/repository.py` | SignalRepository：信号持久化与查询 |
| `src/signals/strategies/htf_cache.py` | HTFStateCache：高时间框架状态缓存 |
| `src/signals/strategies/adapters.py` | UnifiedIndicatorSourceAdapter：解耦信号模块与指标管理器 |
| `src/signals/analytics/interfaces.py` | DiagnosticsEngine Protocol 定义 |
| `src/signals/analytics/plugins.py` | AnalyticsPluginRegistry 插件扩展机制 |
| `src/trading/service.py` | TradingModule：账户、持仓、订单生命周期 |
| `src/trading/trading_service.py` | TradingService：底层下单、平仓、保证金计算 |
| `src/trading/signal_executor.py` | TradeExecutor：信号自动下单执行、熔断器、成本检查 |
| `src/trading/execution_gate.py` | ExecutionGate：策略域准入检查（voting group / 白名单 / armed） |
| `src/trading/pending_entry.py` | PendingEntryManager：价格确认入场（信号 → 等 Quote 价格落入区间 → 执行） |
| `src/trading/pending_entry.py` (`_compute_reference_price()`) | 按 category 计算智能入场参考价（trend→均线支撑, reversion→BB middle, breakout→Donchian 关键位） |
| `src/trading/position_manager.py` | PositionManager：持仓监控、止损跟踪、日终自动平仓 |
| `src/trading/sizing.py` | 仓位计算、时间框架差异化 SL/TP（`TimeframeSLTP`） |
| `src/trading/signal_quality_tracker.py` | SignalQualityTracker：信号预测质量追踪（N bars 后评估，供 Calibrator） |
| `src/trading/trade_outcome_tracker.py` | TradeOutcomeTracker：实际交易盈亏追踪（由 PositionManager 关仓触发） |
| `src/trading/registry.py` | TradingAccountRegistry：多账户注册与服务工厂 |
| `src/trading/models.py` | TradeOperationRecord 数据类 |
| `src/risk/rules.py` | 风险规则：AccountSnapshotRule / DailyLossLimitRule / MarginAvailabilityRule / TradeFrequencyRule / ProtectionRule 等 |
| `src/risk/models.py` | TradeIntent / RiskCheckResult / RiskAssessment 数据类 |
| `src/market_structure/models.py` | MarketStructureContext 数据类 |
| `src/monitoring/health_monitor.py` | SQLite 指标存储、告警、健康报告 |
| `src/monitoring/health_checks.py` | 健康检查具体实现（从 health_monitor 拆分） |
| `src/monitoring/health_reporting.py` | 健康报告生成 |
| `src/monitoring/manager.py` | MonitoringManager：定时巡检、组件协调 |
| `src/backtesting/engine.py` | BacktestEngine：逐 bar 回放主循环，复用 evaluate/filters/regime |
| `src/backtesting/optimizer.py` | 网格/随机参数搜索，复用 CachedDataLoader + 预计算指标 |
| `src/backtesting/walk_forward.py` | Walk-Forward 前推验证（IS/OOS 分割 + 过拟合检测） |
| `src/backtesting/recommendation.py` | RecommendationEngine（参数推荐生成）+ ConfigApplicator（应用/回滚） |
| `src/backtesting/component_factory.py` | `build_backtest_components()`：构建独立 pipeline/SignalModule |
| `src/backtesting/data_loader.py` | CachedDataLoader：从 DB 加载历史 OHLC + 指标预计算缓存 |
| `src/backtesting/portfolio.py` | PortfolioTracker：模拟持仓/SL/TP/资金曲线 |
| `src/backtesting/metrics.py` | BacktestMetrics：Sharpe/Sortino/Calmar/最大回撤 |
| `src/backtesting/models.py` | BacktestResult/Recommendation/ParamChange/RecommendationStatus |
| `src/backtesting/api.py` | 回测 HTTP API 路由（run/optimize/walk-forward/recommendations） |
| `src/persistence/repositories/backtest_repo.py` | 回测结果与推荐记录的 DB 仓储 |

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
  ↓ 事件写入 `runtime_data_dir/events.db`（持久化，不丢失）
  ↓ process_closed_bar_events_batch()
  ↓ _load_confirmed_bars() ← N 根完整收盘 K 线
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

**推导流程（`src/app_runtime/factories/signals.py` 启动时执行）**：
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

#### Confirmed-Only 策略（17 个）
仅在 K 线收盘时评估，使用完整历史收盘数据，信号稳定，适合趋势确认和突破判断。

| 策略名 | 所需指标（真实名称）| 场景 |
|--------|-------------------|------|
| `sma_trend` | sma20, ema50 | 均线金死叉趋势跟踪 |
| `macd_momentum` | macd | MACD 柱状图动量 |
| `supertrend` | supertrend14, adx14 | Supertrend + ADX 趋势过滤 |
| `ema_ribbon` | ema9, hma20, ema50 | EMA 多均线排列 |
| `hma_cross` | hma20, ema50 | HMA/EMA 交叉 |
| `roc_momentum` | roc12, adx14, atr14 | 变动率动量 |
| `fib_pullback` | supertrend14, atr14 | 斐波那契回调入场 |
| `rsi_divergence` | rsi14 | RSI 背离反转 |
| `donchian_breakout` | donchian20, adx14 | 唐奇安通道突破 |
| `fake_breakout` | donchian20, atr14 | 假突破识别反手 |
| `squeeze_release` | boll20, keltner20, macd | 布林带收缩释放 |
| `multi_timeframe_confirm` | sma20, ema50 | 跨时间框架确认 |
| `session_momentum` | atr14, supertrend14 | 时段动量偏差 |
| `asian_range_breakout` | atr14 | 亚盘区间突破 |
| `price_action_reversal` | atr14 | 针形/吞没/内包K线 |
| `order_block_entry` | atr14 | OB 回踩入场 |
| `trend_triple_confirm` | supertrend14, ema9, hma20, ema50, macd | 三重趋势确认（复合）|

#### Intrabar + Confirmed 策略（8 个）
两条链路都接收，盘中可以提前预警（preview 状态机），收盘时产生确认信号。

| 策略名 | 所需指标（真实名称）| 场景 |
|--------|-------------------|------|
| `rsi_reversion` | rsi14 | RSI 超买超卖回归 |
| `stoch_rsi` | stoch_rsi14 | StochRSI 极值反转 |
| `williams_r` | williamsr14 | Williams %R 超卖买入 |
| `cci_reversion` | cci20 | CCI 极值均值回归 |
| `bollinger_breakout` | boll20 | 布林带触边突破 |
| `keltner_bb_squeeze` | boll20, keltner20 | BB/KC 挤压形态 |
| `reversion_double_confirm` | rsi14, stoch_rsi14 | 双振荡器确认回归（复合：rsi_reversion + stoch_rsi）|
| `vwap_reversion` | vwap30 | VWAP 均值回归（Demo 暂不启用）|

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

复合策略的 scope 由 `config/composites.json` 中的 `preferred_scopes` 字段**显式声明**（不自动推导）：
- `breakout_double_confirm`：`["confirmed"]`（子策略：donchian_breakout + keltner_bb_squeeze + squeeze_release，均为 confirmed-only）
- `reversion_double_confirm`：`["confirmed", "intrabar"]`（RSI + StochRSI 均支持盘中预警）
- 其他复合策略：`["confirmed"]`

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
| `roc_momentum` | roc12, adx14, atr14 | confirmed |
| `fib_pullback` | supertrend14, atr14 | confirmed |

### 均值回归（Mean Reversion）— `src/signals/strategies/mean_reversion.py`

| 策略名 | 所需指标 | Scope |
|--------|---------|-------|
| `rsi_reversion` | rsi14 | intrabar + confirmed |
| `stoch_rsi` | stoch_rsi14 | intrabar + confirmed |
| `williams_r` | williamsr14 | intrabar + confirmed |
| `cci_reversion` | cci20 | intrabar + confirmed |
| `rsi_divergence` | rsi14 | confirmed |
| `vwap_reversion` | vwap30 | intrabar + confirmed |

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
| `asian_range_breakout` | atr14 | confirmed |

### 价格行为（Price Action）— `src/signals/strategies/price_action.py`

| 策略名 | 所需指标 | Scope |
|--------|---------|-------|
| `price_action_reversal` | atr14 | confirmed |
| `order_block_entry` | atr14 | confirmed |

### 复合策略（Composite）— `config/composites.json` + `src/signals/strategies/registry.py`

复合策略由 `composites.json` 声明式配置，通过 `build_composite_strategies()` 动态构建。

| 策略名 | 子策略 | Scope | 模式 |
|--------|--------|-------|------|
| `trend_triple_confirm` | supertrend + macd_momentum + sma_trend | confirmed | majority |
| `breakout_double_confirm` | donchian_breakout + keltner_bb_squeeze + squeeze_release | confirmed | all_agree |
| `reversion_double_confirm` | rsi_reversion + stoch_rsi | intrabar + confirmed | all_agree |
| `breakout_release_confirm` | donchian_breakout + squeeze_release | confirmed | all_agree |
| `reversal_rejection_confirm` | fake_breakout + price_action_reversal | confirmed | all_agree |

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

**当前投票组配置**（2026-04 按方向一致性 + TF 兼容性重组）：
```
momentum_vote  = supertrend, donchian_breakout, roc_momentum, order_block_entry  (M15/M30 顺势动量)
breakout_vote  = keltner_bb_squeeze, squeeze_release, asian_range_breakout       (M15/M30 突破确认)
reversion_vote = rsi_reversion, stoch_rsi, cci_reversion                        (M15 回归)
reversal_vote  = bollinger_breakout, fake_breakout, price_action_reversal, rsi_divergence (M15/M30 反转)
```

**表决算法**：
```
buy_score  = Σ confidence(buy)  / Σ all confidence
sell_score = Σ confidence(sell) / Σ all confidence
→ 达到 consensus_threshold（默认 0.40）时产生信号
→ disagreement_factor 减少置信度，惩罚多空分歧
```

---

## Key Algorithms（XAUUSD 日内优化）

### TFParamResolver — Per-TF 策略参数（`src/signals/strategies/tf_params.py`）

策略参数按时间框架独立配置，替代旧的全局 `setattr` + `TimeframeScaler` 线性缩放方案：

```python
# 策略 evaluate 中通过 get_tf_param 查表
from .base import get_tf_param

overbought = get_tf_param(self, "overbought", context.timeframe, self._overbought)
# M5 → 72.0 (per-TF)   H1 → 78.0 (全局)   D1 → 78.0 (fallback 全局)
```

查找优先级：`[strategy_params.<TF>]` → `[strategy_params]` → default 参数

### TimeframeScaler（`src/signals/strategies/base.py`）

按时间框架缩放指标**周期**（如 lookback_bars），仍可用于非参数化的周期缩放场景：

```python
SCALE_FACTORS = {"M1": 0.60, "M5": 0.75, "M15": 0.85, "H1": 1.00, "H4": 1.15, "D1": 1.30}
# scaler.scale_period(14) → M1 下返回 8, M5 下返回 10
```

> **注意**：策略阈值参数（overbought/adx_threshold 等）不再使用 TimeframeScaler 缩放，改为通过 `TFParamResolver` 精确配置。

### Indicator Delta（`src/indicators/result_store.py`）

核心动量指标自动计算 N-bar 变化率（一阶导数），通过 `indicators.json` 的 `delta_bars` 字段配置：
- 覆盖指标：`rsi14`, `macd`, `cci20`, `stochrsi`, `adx14`
- 输出：`{"rsi": 28.5, "rsi_d3": -8.2, "rsi_d5": -12.0}`

### Market Structure Cache（`src/market_structure/analyzer.py`）

`MarketStructureAnalyzer.analyze_cached()` 内置 scope-aware 缓存：
- confirmed scope：重新计算市场结构分析并写入缓存
- intrabar scope：复用上一次 confirmed 的分析结果（避免重复计算）
- TTL 过期自动失效（默认 5 分钟）
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

    # 5. HTF 指标 — 无需在策略代码中声明
    #    通过 signal.ini [strategy_htf] 配置：策略名.指标名 = 来源TF
    #    策略 evaluate() 中通过 context.htf_indicators.get("H1", {}) 按需访问
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

## HTF 指标上下文注入

HTF 指标注入完全由 `signal.ini [strategy_htf]` 驱动。策略代码**不声明任何 HTF 属性**，只在 `evaluate()` 中按需消费 `context.htf_indicators`。

### 配置方式

```ini
# signal.ini [strategy_htf]
# 格式：策略名.指标名 = 来源TF
supertrend.supertrend14 = H1
supertrend.adx14 = H1
sma_trend.sma20 = D1
sma_trend.ema50 = D1
rsi_reversion.rsi14 = M15
```

### 策略消费

```python
class MyStrategy:
    name = "my_strategy"
    required_indicators = ("rsi14",)
    # 无 htf_indicators 属性 — HTF 完全由 INI 控制

    def evaluate(self, context: SignalContext) -> SignalDecision:
        rsi = context.indicators.get("rsi14", {}).get("rsi")
        # 按 INI 配置的 TF 访问 HTF 数据（未配置时为空 dict，安全跳过）
        h1 = context.htf_indicators.get("H1", {})
        d1 = context.htf_indicators.get("D1", {})
        h1_adx = h1.get("adx14", {}).get("adx")
        d1_ema = d1.get("ema50", {}).get("ema")
```

### 工作原理

- HTF 指标**不需要额外计算**——`IndicatorManager` 已经为所有配置的 `(symbol, timeframe)` 在 bar 收盘时计算了全量指标
- `SignalRuntime._evaluate_strategies()` 在调用 `service.evaluate()` 前，从 `IndicatorManager.get_indicator()` 查询 HTF 数据
- INI 中未配置的策略不触发任何 HTF 查询（零开销）
- HTF 数据天然低频更新（H4 每 4 小时才变），查询命中的是内存缓存

### 与 HTFStateCache 的关系

| HTFStateCache | HTF 指标注入 |
|---------------|-------------|
| 缓存信号方向（buy/sell） | 缓存指标数值（adx, ema...） |
| 消费者：MultiTimeframeConfirm、SignalRuntime（HTF 方向对齐修正） | 消费者：任何在 INI [strategy_htf] 中配置的策略 |

两者互补，独立工作。

### 配置

`config/signal.ini` 提供两个相关 section：

```ini
[htf_indicators]
enabled = true   # false 时所有 HTF 指标注入被禁用

[strategy_htf]
# 格式：策略名.运行TF = 目标TF
# 未配置的组合自动用下一个更高的已配置 TF
supertrend.M5 = H1
supertrend.H1 = D1
sma_trend.H1 = D1
```

### 边界情况

- 目标 TF 未在 `app.ini` 配置 → 运行时跳过
- HTF 指标尚未计算（刚启动）→ warmup 机制会在启动后 30s 内每 2s 重试 reconcile，一旦 OHLC 缓存就绪即触发计算；策略需安全访问（`.get()` 链）
- 目标 TF = 当前 TF → 自动跳过（数据已在 `context.indicators` 中）
- 未配置 `[strategy_htf]` 且无更高 TF → 不注入
- HTF 指标仅在 HTF bar 收盘时更新 → `get_indicator()` 返回 `_bar_time` 字段，标识数据所属的 bar 收盘时间

---

## Signal Listener 架构

`SignalRuntime._publish_signal_event()` 将 `SignalEvent` 广播给所有已注册的 listener。
注册方式：`runtime.add_signal_listener(callback)`。当前有三个 listener：

```
SignalRuntime._publish_signal_event(event: SignalEvent)
    │  每次策略评估产生状态转换时触发（idle→idle 不发布）
    │
    ├─→ TradeExecutor.on_signal_event()      ← 自动下单
    │     仅处理 confirmed_buy / confirmed_sell
    │     ExecutionGate 准入检查 → min_confidence → 持仓限制 → 成本检查
    │     通过后调用 TradingModule 下单
    │
    ├─→ SignalQualityTracker.on_signal_event()  ← 信号质量追踪
    │     仅处理 scope="confirmed" 的事件
    │     ① _advance_pending_evaluations：推进同 (symbol, tf) 下所有策略的待评估计数
    │     ② _register_signal_for_evaluation：confirmed_buy/sell 时登记新的待评估信号
    │     bars_elapsed 到期 → 写入 signal_outcomes 表 + 回调 PerformanceTracker(source="signal")
    │
    └─→ HTFStateCache.on_signal_event()      ← 高时间框架方向缓存
          仅处理 confirmed_buy / confirmed_sell / confirmed_cancelled
          缓存方向 + 时间戳，供 SignalRuntime 做 HTF 方向对齐修正
```

### SignalEvent 的类型（signal_state）

| signal_state | 触发时机 | 语义 |
|---|---|---|
| `confirmed_buy` | confirmed scope 下策略输出 buy | K 线收盘确认做多 |
| `confirmed_sell` | confirmed scope 下策略输出 sell | K 线收盘确认做空 |
| `confirmed_cancelled` | 上根有信号，本根转 hold | 信号取消 |
| `preview_buy` / `preview_sell` | intrabar scope 下方向初次出现 | 盘中预览 |
| `armed_buy` / `armed_sell` | preview 方向稳定 ≥ min_preview_stable_seconds | 已蓄力 |

> **注意**：`idle→idle`（策略持续 hold 且之前也是 idle）不产生 SignalEvent——
> `_transition_confirmed()` 在 `previous_state == "idle"` 且 action 非 buy/sell 时返回 `None`。

### 信号质量 vs 交易结果：两个独立追踪器

| 维度 | SignalQualityTracker | TradeOutcomeTracker |
|------|---------------------|---------------------|
| **衡量什么** | 信号预测质量（N bars 后方向对不对） | 实际交易盈亏 |
| **entry_price** | 信号 bar 的 close（理论入场） | 实际成交价（含滑点） |
| **exit_price** | N bars 后的 close（理论退出） | 实际平仓价 |
| **评估对象** | 所有 confirmed 信号（含未执行的） | 仅实际执行的交易 |
| **触发方式** | SignalRuntime 信号监听 | TradeExecutor 登记 + PositionManager 关仓回调 |
| **写入表** | `signal_outcomes` | `trade_outcomes` |
| **主要消费者** | ConfidenceCalibrator（长期统计校准） | StrategyPerformanceTracker（日内实时反馈） |

### SignalQualityTracker 评估路径

每收到一个 scope="confirmed" 的 SignalEvent → `_advance_pending_evaluations` 推进同 (symbol, tf) 下**所有策略**的 `bars_elapsed` → 达到 `bars_to_evaluate`（默认 5）后用最新收盘价评估。

### TradeOutcomeTracker 评估路径

1. `TradeExecutor` 下单成功 → `on_trade_opened(signal_id, fill_price, ...)` 登记活跃交易
2. `PositionManager` 检测仓位关闭 → `on_position_closed(pos, close_price)` 触发评估
3. 用实际成交价计算盈亏 → 回调 PerformanceTracker(source="trade") + 写入 trade_outcomes 表

### 绩效反馈的两条独立链路

```
SignalQualityTracker（信号质量评估完成）
    │
    ├─→ on_quality_fn(strategy, won, pnl, regime=)
    │     → StrategyPerformanceTracker(source="signal")
    │
    └─→ write_fn(rows) → signal_outcomes 表（DB 持久化）
          → ConfidenceCalibrator 后台线程每小时查询 DB
            按 (strategy, direction, regime) 聚合最近 7 天胜率
            分阶段校准（<50笔不校准，50-100轻微，100+正常）

TradeOutcomeTracker（实际交易评估完成）
    │
    ├─→ on_outcome_fn(strategy, won, pnl, regime=, source="trade")
    │     → StrategyPerformanceTracker(source="trade")
    │       _trade_stats 单独记录，用于实际交易维度分析
    │
    └─→ write_fn(rows) → trade_outcomes 表（DB 持久化）
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
    → max(confidence_floor, result)    (底线保护)
    × intrabar_confidence_factor        (scope=intrabar 时 × 0.85)
    × htf_alignment_multiplier         (对齐 ×1.10 / 冲突 ×0.70 / 无数据 ×1.0)
    = final_confidence
    ★ 持久化的 confidence = 此最终值 = TradeExecutor 比较的值
```

**effective_affinity 计算**：
- 硬分类模式：`affinity[current_regime]`
- Soft Regime 模式：`Σ(prob[r] × affinity[r])` — 加权平均，消除阈值边界跳变

**日内策略绩效追踪**（`StrategyPerformanceTracker`，`src/signals/evaluation/performance.py`）：
- 纯内存的日内实时反馈层，接收 SignalQualityTracker(source="signal") 和 TradeOutcomeTracker(source="trade") 的结果回调
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

## Backtesting & Parameter Optimization

### 模块概览

`src/backtesting/` 是独立于实盘运行时的回测子系统，通过 `build_backtest_components()` 构建独立的指标 pipeline 和 SignalModule 实例，**不共享全局单例**。

### 核心流程

```
历史 OHLC（DB）
    → CachedDataLoader（预加载 + 指标预计算）
    → BacktestEngine（逐 bar 回放）
        → SignalModule.evaluate()（复用实盘策略代码）
        → SignalFilterChain.should_evaluate()（复用实盘过滤器）
        → PortfolioTracker（模拟 SL/TP/持仓）
    → BacktestResult + BacktestMetrics
```

### 参数优化流程

```
ParameterSpace（参数搜索空间）
    → Optimizer（网格/随机搜索）
        → 每组参数独立创建 SignalModule
        → BacktestEngine 评估
    → 按 Sharpe/胜率/Calmar 排序
    → 最优参数集合
```

### Walk-Forward 前推验证

```
历史数据 ──┬── IS 窗口（训练）→ Optimizer → best_params
           └── OOS 窗口（验证）→ Engine(best_params) → OOS 指标
           ↓ 滚动 N 次
WalkForwardResult:
    splits[]         各窗口 IS/OOS 结果
    overfitting_ratio  IS Sharpe / OOS Sharpe（越接近 1.0 越好）
    consistency_rate   OOS 盈利窗口占比
```

### 参数推荐系统

Walk-Forward 验证完成后，`RecommendationEngine` 自动生成参数调整建议：

```
WalkForwardResult
    → RecommendationEngine.generate()
        → _aggregate_strategy_params()   多窗口 best_params 中位数聚合
        → _recommend_regime_affinities() 基于 OOS regime 胜率调整亲和度
        → _clip_changes()               裁剪极端变更（±30%）
        → _rank_and_limit()             按变更幅度排序，限制数量
    → Recommendation（PENDING 状态）
```

**推荐生命周期**：
```
PENDING → APPROVED → APPLIED → ROLLED_BACK（可选）
           ↑ 人工审核    ↑ ConfigApplicator     ↑ ConfigApplicator
```

**ConfigApplicator 应用流程**：
1. 创建 `signal.local.ini` 备份
2. 原子写入新参数到 `signal.local.ini`（tempfile + os.replace）
3. 调用 `SignalModule.apply_param_overrides()` 热更新内存
4. 更新推荐状态为 APPLIED

**安全约束**：
- `overfitting_ratio > 1.5` → 拒绝生成（过拟合）
- `consistency_rate < 50%` → 拒绝生成（不稳定）
- OOS 总交易数 < 10 → 拒绝（样本不足）
- 单参数变更裁剪到 ±30%
- 亲和度调整步幅 ±15%
- Regime-specific 胜率优先于全局胜率

### 回测 API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/backtest/run` | POST | 单次回测 |
| `/v1/backtest/optimize` | POST | 参数优化（后台任务） |
| `/v1/backtest/walk-forward` | POST | Walk-Forward 前推验证（后台任务） |
| `/v1/backtest/results/{run_id}` | GET | 查询回测/优化结果 |
| `/v1/backtest/recommendations/generate` | POST | 从 WF 结果生成参数推荐 |
| `/v1/backtest/recommendations/{rec_id}` | GET | 查询推荐详情 |
| `/v1/backtest/recommendations/{rec_id}/approve` | POST | 人工审批推荐 |
| `/v1/backtest/recommendations/{rec_id}/apply` | POST | 应用推荐到 signal.local.ini |
| `/v1/backtest/recommendations/{rec_id}/rollback` | POST | 回滚已应用的推荐 |

### 与实盘的复用关系

| 能力 | 复用方式 |
|------|---------|
| 指标计算 | `OptimizedPipeline.compute()` 独立实例 |
| 策略评估 | `SignalModule.evaluate()` 独立实例 |
| 过滤器 | `SignalFilterChain` 直接复用 |
| Regime 检测 | `MarketRegimeDetector` 独立实例 |
| SL/TP 计算 | `sizing.compute_trade_params()` 纯函数 |
| 价格确认区间 | `pending_entry.compute_entry_zone()` 纯函数 |

---

## Risk Management Layers

交易请求从内到外经过多层校验：

1. **Signal filters** (`src/signals/execution/filters.py`)：时段/冷却期/价差/经济事件/**波动率异常**过滤（`SignalFilterChain` 统一入口）
2. **Regime fast-reject** (`SignalRuntime._any_strategy_eligible()`)：所有策略 affinity 不足时跳过全部后续计算
3. **HTF direction alignment** (`SignalRuntime._evaluate_strategies()`)：HTF 方向冲突 ×0.70 / 对齐 ×1.10（在置信度管线内，持久化前）
4. **Execution gate** (`src/trading/execution_gate.py`)：voting group 保护、交易触发白名单、require_armed 门控
5. **Pending entry** (`src/trading/pending_entry.py`)：**价格确认入场** — 信号产生后不立即下单，等 Quote bid/ask 落入 ATR 计算的入场区间后执行（按策略类型自动选择 pullback/momentum/symmetric 三种模式）。入场区间参考价按策略 category 自动从指标数据计算（trend→最近均线, reversion→BB middle, breakout→Donchian 关键位），不再统一使用 bar close
6. **Pre-trade risk service** (`src/risk/service.py`)：`DailyLossLimitRule`（日损失限制）、`AccountSnapshotRule`（仓位限制）、`MarginAvailabilityRule`（保证金动态检查）、`TradeFrequencyRule`（交易频率限制）、`BrokerConstraintRule` 等
7. **Executor safety** (`src/trading/signal_executor.py`)：熔断器、**持仓数量预检**、成本检查（spread_to_stop_ratio）
8. **Position manager** (`src/trading/position_manager.py`)：**日终自动平仓**（UTC 21:00，可配置开关）
9. **Sizing** (`src/trading/sizing.py`)：**时间框架差异化 SL/TP**（M5: 1.8/2.5, M15: 1.8/2.5, M30: 2.0/2.5, H1: 2.0/2.5, H4: 2.2/3.0, D1: 2.5/3.5 ATR 倍数）+ **时间框架差异化风险百分比**（M5: ×0.50, M15: ×0.75, M30: ×0.90, H1: ×1.00, H4: ×1.20, D1: ×1.50）+ **Per-TF min_confidence**（`signal.ini [timeframe_min_confidence]`：M5=0.78, H1=0.70, D1=0.60）+ **HTF 方向冲突阻止**（`[htf_conflict_block]`：M5/M15/M30 趋势策略逆大周期方向直接拒绝，均值回归策略豁免）

---

## API Endpoints

Base URL: `http://<host>:8808`（默认）

**API 版本化**：所有业务路由同时挂载在 `/v1/` 前缀和根路径下（向后兼容）。新客户端应使用 `/v1/` 前缀。`/health` 始终在根路径。

Authentication: `X-API-Key` 请求头（在 `config/market.ini` 中配置）

| Router | Prefix | 主要端点 |
|--------|--------|---------|
| market | `/` | `/symbols`, `/quote`, `/ticks`, `/ohlc`, `/stream`（SSE） |
| trade | `/` | `/trade`, `/close`, `/close_all`, `/trade/precheck`, `/estimate_margin` |
| account | `/account` | `/account/info`, `/account/positions`, `/account/orders` |
| economic | `/economic` | `/economic/calendar`, `/economic/calendar/risk-windows`, `/economic/calendar/trade-guard` |
| indicators | `/indicators` | `/indicators/list`, `/indicators/{symbol}/{timeframe}`, `/indicators/compute` |
| monitoring | `/monitoring` | `/monitoring/health`, `/monitoring/startup`, `/monitoring/queues`, `/monitoring/config/effective` |
| signal | `/signals` | 信号查询、诊断、策略列表、投票信息、**`/signals/monitoring/quality/{symbol}/{timeframe}`**（Regime + 信号质量） |
| backtest | `/backtest` | `/backtest/run`, `/backtest/optimize`, `/backtest/walk-forward`, `/backtest/results/{run_id}`, `/backtest/recommendations/*`（生成/审批/应用/回滚） |
| studio | `/studio` | `/studio/agents`（14 个 agent 实时状态）, `/studio/agents/{id}`, `/studio/events`, `/studio/summary`, `/studio/stream`（SSE 实时推送） |
| admin | `/admin` | `/admin/dashboard`, `/admin/config`, `/admin/config/signal`, `/admin/config/risk`, `/admin/config/indicators`, `/admin/config/composites`, `/admin/performance/strategies`, `/admin/strategies`, `/admin/events/stream`（SSE）, `/admin/pipeline/stream`（SSE） |

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
| `macd` | 动量 | — | 标准 MACD |
| `macd_fast` | 动量 | — | 快速 MACD（短周期）|
| `roc12` | 动量 | — | 12 周期变动率 |
| `adx14` | 趋势强度 | — | ADX + DI |
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

向系统添加新服务组件：
- 在 `src/app_runtime/container.py` 的 `AppContainer` 中添加字段
- 在 `src/app_runtime/factories/` 中添加对应工厂函数
- 在 `src/app_runtime/builder.py` 的 `build_app_container()` 中构建组件
- 在 `src/app_runtime/runtime.py` 的 `AppRuntime.stop()` 中注册关闭逻辑
- 在 `src/api/deps.py` 中添加 getter 函数，通过 `FastAPI.Depends()` 暴露

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
- **`get_signal_config()` 已知 bug**：~~`src/config/centralized.py` 中调用了不存在的 `ConfigValidator.validate_model()`~~（已修复：信号配置通过 `src/config/signal.py` 的 `SignalConfig.model_validate()` 正确加载，不再依赖 `ConfigValidator`）
- **Config snapshot at import time**：`src/api/__init__.py` 在导入时一次性读取 API 配置，修改 `market.ini` 后需重启
- **HTF cache TTL**：~~当前固定 4 小时~~（已修复：通过 `signal.ini` 的 `[htf_cache] max_age_seconds` 配置，默认 14400 秒）
- **SignalQualityTracker 参数**：~~bars_to_evaluate / max_pending 硬编码~~（已修复：通过 `signal.ini` 的 `[signal_quality]` section 配置）
- **Soft Regime feature flag**：~~`soft_regime_enabled` 默认关闭~~（已修改：默认 `true`，概率化 Regime 分类已启用）
- **策略参数 per-TF 配置化**：所有可调策略参数（RSI/CCI/WR/StochRSI 阈值、ADX 门槛、ATR 阈值）通过 `TFParamResolver` 支持按时间框架独立配置。`[strategy_params]` 为全局默认，`[strategy_params.M5]` 等为 per-TF 覆盖。策略通过 `get_tf_param()` 在 evaluate 时按 `context.timeframe` 查表。旧的 `setattr` 直接修改策略属性的方式已废弃。
- **Hot Reload 限制**：`[regime_detector]` 和 `[strategy_params]` 暂不支持热加载，需重启服务生效
- **日损失限制统一**：~~TradeExecutor 中有独立的 daily_loss_check_fn~~（已移除：统一到 `risk.ini` 的 `DailyLossLimitRule`，由 `PreTradeRiskService` 执行）
- **HTF 置信度修正位置**：~~TradeExecutor 在执行前篡改 event.confidence~~（已修复：HTF 方向对齐修正移入 `SignalRuntime._evaluate_strategies()` 置信度管线，持久化的 confidence = 最终执行值）
- **SignalPolicy 精简**：~~包含 `auto_trade_enabled`/`auto_trade_min_confidence`/`auto_trade_require_armed`~~（已移除：这些参数仅在 `ExecutorConfig` / `ExecutionGateConfig` 中使用）
- **市场结构缓存**：~~由 SignalRuntime 在外部管理 `_market_structure_cache`~~（已修复：缓存内置到 `MarketStructureAnalyzer.analyze_cached()`，scope-aware 自动管理）
- **波动率异常过滤**：~~内联在 SignalRuntime._evaluate_strategies() 中~~（已移入 `SignalFilterChain` 的 `VolatilitySpikeFilter`）
- **OutcomeTracker Dead Code**：~~`src/trading/outcome_tracker.py` 中的 `OutcomeTracker` 类（419 行）完全未使用~~（已删除：功能由 `SignalQualityTracker` 替代）
- **D1 Sizing 缺失**：~~`TIMEFRAME_SL_TP` 和 `TIMEFRAME_RISK_MULTIPLIER` 仅覆盖 M1/M5/M15/H1~~（已修复：添加 D1 配置 sl=2.0 ATR, tp=4.0 ATR, risk=×1.50）
- **Ingestor 退避阈值**：~~退避参数硬编码~~（已修复：通过 `ingest.ini [error_recovery]` section 配置化）
- **HTF 对齐魔数**：~~strength/stability/intrabar_ratio 硬编码~~（已修复：通过 `signal.ini [htf_alignment]` section 配置化）
- **TradeExecutor 队列溢出**：~~put_nowait 满则直接丢弃~~（已修复：confirmed 信号有 1s backpressure 重试 + 溢出计数器）
- **PositionManager 恢复日志**：~~恢复失败仅 debug 级别~~（已修复：改为 warning 级别）
- **event_store 自动清理**：~~依赖手工调用 cleanup_old_events()~~（已修复：_event_loop 中每日自动清理 7 天前数据）
- **Intrabar listener 超时防护**：set_intrabar() listener 调用超过 100ms 时记录 warning 日志
- **策略 category 枚举化**：`StrategyCategory(str, Enum)` 在 `base.py` 中定义，`_validate_strategy_attrs()` 在注册时校验合法性
- **API 风控错误码细化**：`MARGIN_INSUFFICIENT_PRE` / `TRADE_FREQUENCY_LIMITED` / `POSITION_LIMIT_REACHED` / `SESSION_WINDOW_BLOCKED` 独立错误码，按 `rule_name` 映射
- **Intrabar 衰减策略级差异化**：`strategy_params` 支持 `策略名__intrabar_decay = 0.90`，策略级覆盖全局默认值
- **HTF 指标 staleness 检查**：`_resolve_htf_indicators()` 检查 `_bar_time`，超过 2×HTF 周期的陈旧数据自动跳过注入
- **应用运行时三层架构**：`src/api/deps.py` 已拆分为 `src/app_runtime/`（container/builder/runtime），`deps.py` 仅保留 FastAPI getter 适配层。旧 `src/api/lifespan.py` 已删除，功能由 `AppRuntime` 接管。
- **lifespan shutdown bug 已修复**：旧 `lifespan.py` 中 `container.market_data`（属性不存在）已在新 `AppRuntime.stop()` 中修正为 `c.market_service`
- **TradeFrequencyRule 线程安全**：`record_trade()` 和 `evaluate()` 通过 `threading.Lock` 保护，修复并发丢失时间戳的 bug
- **MarketEventBus shutdown 防护**：`_shutdown_flag` + `RuntimeError` 捕获，防止 shutdown 后 dispatch 调用崩溃
- **market_impact 表 DDL 修复**：PRIMARY KEY 加入 `recorded_at` 分区列，符合 TimescaleDB 要求
- **OHLC 价格修复**：`get_ohlc_from()` 请求参数和响应时间戳统一走 `_market_time_to_request` / `_parse_server_timestamp`，修复 UTC+3 偏移导致的价格错误
- **trade_outcomes close_price 修复**：`history_deals_get()` 时间参数统一走 `_server_now()`，修复平仓价获取失败
- **Warmup 显式屏障**：~~旧方案通过 `bar_age > max(10×tf, 15min)` 间接推断回补数据~~（已重构）。新方案：`BackgroundIngestor.is_backfilling` 属性 + `SignalRuntime._warmup_ready_fn` 回调，回补期间所有 confirmed/intrabar 快照不入队、不评估、不产生信号。回补结束后各 `(symbol, tf)` 需收到 **bar_time 经过 staleness 校验的** 首个实时 confirmed bar 才解除 intrabar 屏障（`bar_time` 距当前时间不超过 `2×tf_seconds + 30s`，防止异步管道残留的历史快照被误认为实时数据）。IndicatorManager 在此期间正常计算指标填充缓存（API 可查询），仅信号评估被屏蔽。无 `warmup_ready_fn` 时（standalone/测试）跳过 staleness 检查，行为与旧版兼容
- **Warmup 指标完整性检查**：`SignalPolicy.warmup_required_indicators = ("atr14",)`，与 warmup 屏障互补——回补完成但 ATR 尚未计算时仍跳过，避免浪费首次 `state_changed=true` 转换
- **Signal 持久化优化**：`state_changed=false` 的重复信号不再写 DB，减少 ~95% signal_events 写入量。listener 仍收到所有事件
- **Voting Group 成员信号抑制**：属于 voting group 的策略不再单独发布/持久化信号事件，只贡献 `snapshot_decisions` 给投票。`standalone_override` 白名单内的策略例外。投票结果（`*_vote`/`consensus`）正常发布。`ExecutionGate` 的 `voting_group_member` 拦截逻辑保留为防御层
- **Studio 前后端分工标准化**：后端 mapper 只返回结构化数据（`status`/`alertLevel`/`metrics`），`task` 字段不含数值；前端负责所有展示渲染（标签、颜色、格式化）
- **Studio 14 Agent 架构**：从 10 个扩展为 14 个 agent。新增：`filter_guard`（过滤员）、`regime_guard`（研判员）、`live_analyst`（实时分析员，intrabar 指标计算+迷你K线）、`live_strategist`（实时策略员，preview/armed 信号）。分析和策略各拆为 confirmed/intrabar 双链路
- **IndicatorManager scope 维度统计**：`_scope_stats` 按 confirmed/intrabar/reconcile 三维度独立计数，避免 reconcile 重算混入 bar close 统计
- **FilterChain 按 scope 分维度计数**：`_filter_by_scope` 分别统计 confirmed/intrabar 的通过/拦截次数，1h 滑动窗口也按 scope 分维度
- **Ingestor intrabar 去重统计**：`queue_stats()` 新增 `intrabar.polls/deduped/updated` 字段，追踪 OHLC 未变化时的去重跳过比例
- **统一时区模块**：`src/utils/timezone.py` 提供 `utc_now()`/`to_display()`/`LocalTimeFormatter`，日志时间戳统一为 `app.ini [system] timezone` 配置的显示时区
- **MT5 统一时间 API**：`MT5BaseClient` 新增 `_server_now()`/`_parse_server_timestamp()`/`_parse_server_timestamp_msc()`/`_request_time_range()`，所有 MT5 API 调用统一走这些方法，消除散落各处的手动时区转换
- **MarketEventBus**：从 `MarketDataService` 提取事件订阅/分发逻辑，`service.event_bus` 属性访问
- **HTF Resolver**：`src/signals/orchestration/htf_resolver.py`，HTF 配置解析/指标查询/对齐乘数计算独立为纯函数模块
- **State Machine**：`src/signals/orchestration/state_machine.py`，preview/armed/confirmed 状态转换纯逻辑，可独立测试
- **Vote Processor**：`src/signals/orchestration/vote_processor.py`，投票 fusion/emit/dispatch 纯函数模块
- **Affinity**：`src/signals/orchestration/affinity.py`，regime 亲和度计算 + 快速拒绝检查纯函数
- **Delta Metrics**：`src/indicators/delta_metrics.py`，N-bar 变化率计算纯函数（可被 backtesting 直接复用）
- **回测进程内执行**：当前回测任务通过 FastAPI `BackgroundTasks` 在进程内异步执行，非独立 worker。API 重启会丢失正在运行的任务
- **WF 结果内存缓存**：`WalkForwardResult` 对象缓存在 `_wf_result_cache` 中，API 重启后丢失。生成推荐前须确保 WF 结果仍在内存中
- **参数推荐 Hot Reload**：`ConfigApplicator.apply()` 通过 `SignalModule.apply_param_overrides()` 热更新内存参数；若 SignalModule 不可用（standalone 回测模式），仅写入文件
- **推荐 INI 原子写入**：`_write_local_ini()` 使用 `tempfile.mkstemp()` + `os.replace()` 确保写入原子性，避免部分写入导致配置损坏
- **推荐 DB 降级**：`apply`/`rollback` 端点在 DB 写入失败时降级为 warning 日志，不影响已成功的文件/内存操作
- **复合策略 JSON 配置驱动**：复合策略定义完全由 `config/composites.json` 声明式配置驱动，通过 `src/signals/strategies/registry.py` 的 `build_composite_strategies()` 动态构建。`composite.py` 中的硬编码 `build_*()` 函数已删除。JSON 文件不存在时 warning + 空列表，JSON 格式错误时 RuntimeError
- **composites.json 与硬编码不一致**：~~`breakout_double_confirm` 在 JSON 中有 3 个子策略 + `preferred_scopes=["confirmed"]`，但硬编码回退中只有 2 个子策略 + `("intrabar", "confirmed")`。正常运行时以 JSON 为准~~（已修复：子策略更新为 donchian_breakout + keltner_bb_squeeze + squeeze_release）
- **策略库优化（2026-03-30）**：禁用 5 个高频低效策略（hma_cross/ema_ribbon/williams_r/session_momentum/multi_timeframe_confirm），通过 `signal.ini [regime_affinity.*]` 全设 0.0。策略代码保留，改 INI 即可恢复
- **策略体系重构（2026-04-01）**：
  - `htf_trend_m5_entry` 重命名为 `htf_trend_pullback`（HTF 定向 + LTF RSI 回调，支持任意 HTF/LTF 组合）
  - 新增 `htf_h4_pullback`（H4 定向 + M30/H1 回调）、`htf_m30_pullback`（M30 定向 + M15 回调）
  - 新增 `dual_tf_momentum`（H1+LTF 双 TF supertrend 共振）、`dual_h4_momentum`（H4+LTF 共振）
  - 新增 M5 专用 intrabar 策略：`m5_scalp_rsi`（M30 定向 + M5 RSI 极值）、`m5_scalp_rsi_h1`（H1 定向）、`m5_momentum_burst`（M30 + M5 ADX/RSI 动量突发）
  - 投票组按方向一致性重组：`momentum_vote`（顺势动量 M15/M30）、`breakout_vote`（突破 M15/M30）、`reversion_vote`（回归 M15）、`reversal_vote`（反转 M15/M30）
  - 移除 trade_trigger 白名单机制（冗余，由置信度管线 + 投票组自然过滤）
  - 同向再入场冷却机制：`reentry_cooldown_bars = 3`（signal.ini [execution_costs]）
  - 回测引擎新增 per-strategy session 过滤（与生产一致）、voting_group_engines 传参修复
  - 全局 uncertain regime affinity 压制至 0.0~0.20
  - M30 回测 PnL 从基线 -667 → +2471（PF=1.22, WR=73.8%）
- **breakout_double_confirm 方向冲突已修复**：BollingerBreakout（均值回归）替换为 SqueezeReleaseFollow（突破跟随），三个子策略方向一致
- **VWAP 策略暂不启用**：Demo 账户无真实成交量，vwap30 指标 enabled=false，策略通过 regime_affinity 全设 0.0 禁用
- **Trailing TP 已启用**：`trailing_tp_enabled = true`，激活阈值 1.2×ATR，回撤容忍 0.6×ATR。配合收紧的固定 TP（2.5×ATR），多数盈利交易在 1.2~2.5×ATR 间被 trailing TP 关闭
- **Indicator Exit 已禁用**：`indicator_exit_enabled = false`（2026-04 A/B 实验结论：4 个子规则在正常波动中频繁误触发，关闭后 M30 WR 55%→71% 转盈利。代码保留但默认关闭）
- **回测 signal_exit 反手已移除**：回测引擎不再在策略方向翻转时强制平仓旧持仓，与实盘 TradeExecutor 行为一致（旧仓由 SL/TP/Trailing 自然出场）
- **fake_breakout category 修正**：从 `breakout` 改为 `reversion`（假突破后反转入场，本质是回归策略），影响入场 zone_mode 从 momentum 变为 symmetric
- **智能入场参考价**：`compute_entry_zone()` 新增 `_compute_reference_price()`，按策略 category 从指标数据计算合理入场锚点（trend→均线支撑, reversion→BB middle, breakout→Donchian 关键位），替代之前统一使用 bar close 作为区间中心
- **deps.py 初始化无失败标记**：`_ensure_initialized()` 在 `build_app_container()` 抛异常后不设 init_failed 标志，后续请求会反复重试构建并产生重复异常日志
- **模块位置注意**：
  - `AppContainer` → `src/app_runtime/container.py`（纯组件持有，可多实例）
  - `AppBuilder` → `src/app_runtime/builder.py`（构建组件，不启动线程）
  - `AppRuntime` → `src/app_runtime/runtime.py`（生命周期管理）
  - `SignalRuntime` → `src/signals/orchestration/runtime.py`（不在 `src/signals/runtime.py`）
  - `MarketDataService` → `src/market/service.py`（不在 `src/core/market_service.py`）
  - `EconomicCalendarService` → `src/calendar/service.py`（不在 `src/core/economic_calendar_service.py`）
  - `ExecutionGate` → `src/trading/execution_gate.py`（策略域准入检查，从 TradeExecutor 分离）
  - `VolatilitySpikeFilter` → `src/signals/execution/filters.py`（与 `SignalFilterChain` 同文件）
  - `TimeframeScaler` → `src/signals/strategies/base.py`（与 `SignalStrategy` Protocol 同文件）
  - `TFParamResolver` → `src/signals/strategies/tf_params.py`（per-TF 策略参数查表器）
  - `get_tf_param()` → `src/signals/strategies/base.py`（策略 evaluate 中查表的便利函数）
  - `SessionTransitionFilter` → `src/signals/execution/filters.py`（与 `SignalFilterChain` 同文件）
  - `DailyLossLimitRule` → `src/risk/rules.py`（与其他 Risk Rules 同文件）
  - `StrategyPerformanceTracker` → `src/signals/evaluation/performance.py`（与 Calibrator 平级，不在 trading 模块）
  - `TradeAPIDispatcher` → `src/api/trade_dispatcher.py`（独立文件，不在路由文件中）
  - `PendingEntryManager` → `src/trading/pending_entry.py`（价格确认入场，由 TradeExecutor 调用）
  - `MarginAvailabilityRule` / `TradeFrequencyRule` → `src/risk/rules.py`（与其他 Risk Rules 同文件）
  - `UnifiedIndicatorSourceAdapter` → `src/signals/strategies/adapters.py`（解耦信号与指标的适配器）
  - `RecommendationEngine` / `ConfigApplicator` → `src/backtesting/recommendation.py`（参数推荐生成与应用）
  - `BacktestRepository` → `src/persistence/repositories/backtest_repo.py`（回测结果与推荐记录仓储）
  - `BacktestEngine` → `src/backtesting/engine.py`（逐 bar 回放，不在 optimizer.py 中）
  - `WalkForwardValidator` → `src/backtesting/walk_forward.py`（前推验证，不在 optimizer.py 中）
  - `StudioService` → `src/studio/service.py`（Registry 模式 + SSE 订阅管理，零业务模块导入）
  - `StudioRuntime` → `src/studio/runtime.py`（Studio 事件桥接与生命周期管理）
  - Studio mappers → `src/studio/mappers.py`（14 个纯函数，只返回结构化数据，不含展示文本；前端负责渲染）
  - Studio 装配 → `src/app_runtime/builder.py` `_build_studio_service()`（唯一知道两边的绑定点）
  - Studio API → `src/api/studio.py`（REST 4 端点 + SSE `/studio/stream`）
  - Admin API → `src/api/admin.py`（仪表盘/配置/策略/绩效/SSE 事件流）
  - `RuntimeReadModel` → `src/readmodels/runtime.py`（运行时状态聚合投影，供 admin/monitoring API 使用）
  - `PipelineEventBus` → `src/monitoring/pipeline_event_bus.py`（指标管线事件总线）
  - `confidence_pipeline` → `src/signals/confidence.py`（置信度管线纯函数）
  - `build_composite_strategies` → `src/signals/strategies/registry.py`（从 composites.json 构建复合策略）
  - `HTFTrendPullback` / `DualTFMomentum` → `src/signals/strategies/multi_tf_entry.py`（HTF 定方向 + LTF 回调/共振入场策略，支持多 HTF 实例）
  - `M5ScalpRSI` / `M5MomentumBurst` → `src/signals/strategies/m5_scalp.py`（M5 专用 intrabar 快速策略）
  - `OrderBlockEntryStrategy` → `src/signals/strategies/price_action.py`（OB 回踩入场策略）
  - `VwapReversionStrategy` → `src/signals/strategies/mean_reversion.py`（VWAP 均值回归策略，Demo 暂不启用）
  - `BacktestTradeGuardProvider` → `src/backtesting/economic_provider.py`（回测用经济日历 TradeGuard，DB 预加载到内存）

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
