# 系统架构概览

> 面向开发者的项目架构参考。阅读后应能理解系统组成、数据流转和扩展方式。

---

## 1. 系统定位

MT5Services 是生产级 FastAPI 量化交易平台，核心能力链路：

```
行情采集 → 指标计算 → 信号生成 → 风险控制 → 交易执行 → 持仓管理
```

## 2. 领域边界

系统按业务域拆分，每层只做自己的事：

| 模块 | 职责 | 不应做 |
|------|------|--------|
| `src/market/` | 内存行情缓存 + 事件总线 | MT5 采集、指标计算 |
| `src/ingestion/` | MT5 数据拉取、节流控制 | 数据缓存、信号评估 |
| `src/indicators/` | 指标计算流水线与快照发布 | 交易执行 |
| `src/signals/` | 策略评估、状态机、投票 | 直接下单 |
| `src/risk/` | 规则判断与解释 | 制造信号 |
| `src/trading/` | 下单执行与持仓生命周期 | 回写信号逻辑 |
| `src/calendar/` | 经济日历同步、Trade Guard | 策略评估 |
| `src/market_structure/` | 市场结构分析 (支撑/阻力) | 直接下单 |
| `src/monitoring/` | 健康检查、告警 | 业务逻辑 |
| `src/persistence/` | 异步队列写入 + 仓储查询 | 业务计算 |
| `src/api/` | HTTP 服务化暴露 | 领域计算 |
| `src/backtesting/` | 回测引擎与参数优化 | 共享全局单例 |

## 3. 启动流程

组件初始化分两个阶段（`src/app_runtime/`）：

### 构建阶段 (`build_app_container()`)

不启动线程，仅创建对象并连接依赖：

```
MarketDataService → StorageWriter → BackgroundIngestor → UnifiedIndicatorManager
    → EconomicCalendarService → TradingAccountRegistry → TradingModule
    → SignalModule + 策略注册 → SignalRuntime → TradeExecutor
    → HealthMonitor → MonitoringManager
```

### 启动阶段 (`AppRuntime.start()`)

按序启动后台线程：

```
1. StorageWriter        → DB 队列刷写
2. IndicatorManager     → 注册 listener + 后台线程
3. BackgroundIngestor   → MT5 采集主循环
4. EconomicCalendar     → 日历同步
5. SignalRuntime        → 信号评估事件循环
6. Calibrator           → 置信度校准热启动
7. PendingEntryManager  → 价格确认监控
8. PositionManager      → 持仓监控 + sync
9. MonitoringManager    → 定时巡检
```

关闭顺序相反。所有组件通过 `AppContainer` 持有，`deps.py` 仅作 FastAPI DI 适配层。

## 4. 数据流

### 4.1 行情采集

```
MT5 Terminal
    ↓ BackgroundIngestor (poll_interval=0.5s)
    ├─ get_quote()       → MarketDataService.set_quote()
    ├─ copy_ticks_from() → MarketDataService.extend_ticks()
    ├─ copy_rates()      → MarketDataService.set_ohlc_closed()  (按 ohlc_interval 节流)
    ├─ get_ohlc(count=1) → MarketDataService.set_intrabar()     (按 intrabar_interval 节流)
    └─ 同时写入 StorageWriter → TimescaleDB
```

`MarketDataService` 是运行时数据的**唯一权威**，TimescaleDB 是持久化备份。

### 4.2 信号生成

```
OHLC 收盘事件 → IndicatorManager → 全量指标计算 → 快照发布
    ↓
SignalRuntime (双队列: confirmed 优先消费)
    ├─ SignalFilterChain (点差/时段/经济/波动率)
    ├─ Regime 快速拒绝 (所有策略 affinity 不足则跳过)
    ├─ 策略评估 × Regime 亲和度 × Calibrator
    ├─ VotingEngine (多组投票)
    ├─ ExecutionGate (准入检查)
    └─ PendingEntryManager → TradeExecutor → MT5
```

### 4.3 事件可靠性分级

| 等级 | 要求 | 事件类型 |
|------|------|---------|
| **L1 (Durable)** | 必须持久化 | confirmed bar close, trade signal, order execution |
| **L2 (Recoverable)** | 允许丢失可回放 | indicator snapshot, reconcile |
| **L3 (Best-effort)** | 丢失可接受 | intrabar preview, 调试快照 |

## 5. 线程模型

系统运行时约 16-20 个活跃后台线程：

| 线程 | 组件 | 职责 |
|------|------|------|
| ingestor-main | BackgroundIngestor | MT5 行情采集主循环 |
| indicator-event | IndicatorManager | durable closed-bar 事件处理 |
| indicator-intrabar | IndicatorManager | 盘中预览指标计算 |
| signal-runtime | SignalRuntime | 信号评估事件循环 |
| pending-entry | PendingEntryManager | Quote 价格确认监控 |
| position-manager | PositionManager | 持仓监控、止损跟踪 |
| calibrator-refresh | ConfidenceCalibrator | 定时刷新校准数据 |
| economic-sync | EconomicCalendar | 日历数据同步 |
| monitoring | MonitoringManager | 定时健康巡检 |
| storage-* × N | StorageWriter | 每通道一个刷写线程 |

## 6. 外部依赖

| 系统 | 用途 | 连接方式 |
|------|------|---------|
| MT5 终端 | 行情采集 + 交易执行 | MT5 Python binding (进程内) |
| TimescaleDB | 历史数据持久化 | psycopg2 连接池 |
| SQLite (events.db) | 指标事件 durable store | 本地文件 |
| SQLite (health_monitor.db) | 健康指标存储 | 本地文件 |

## 7. 开发规则

- 不引入 `compat` / `fallback` 作为主路径
- 新能力先建清晰模块，再接入 runtime
- 配置、持久化、诊断、测试必须与功能一起落地
- 新交易逻辑必须说明风险与验证方式
- 对外接口优先 `precheck / preview / execute` 分层

## 8. 扩展决策树

```
新功能放在哪里？

HTTP 端点           → src/api/ 路由文件
纯数据计算          → src/indicators/core/ 或 src/risk/rules.py
MT5 终端交互        → src/ingestion/ (采集) 或 src/trading/ (执行)
信号策略            → src/signals/strategies/
过滤器              → src/signals/execution/filters.py
评估逻辑            → src/signals/evaluation/
配置 Pydantic 模型  → src/config/models/
DB 写入             → src/persistence/storage_writer.py
DB 查询             → src/persistence/repositories/
```
