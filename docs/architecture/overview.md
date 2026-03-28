# 系统架构概览

本文档描述当前 MT5Services 的运行时分层、核心链路和扩展原则。

## 1. 系统定位

MT5Services 是基于 FastAPI 的量化交易运行时，核心链路为：

```text
行情采集 -> 指标计算 -> 信号评估 -> 风控准入 -> 交易执行 -> 持仓管理
```

系统同时提供三类对外能力：

- 交易与行情 API
- 运维与监控 API
- Studio / Admin 前端投影视图

## 2. 运行时分层

### 2.1 领域层

- `src/market/`
  - 运行时内存行情缓存，系统内的市场数据唯一事实源。
- `src/ingestion/`
  - 从 MT5 拉取 quote、tick、OHLC、intrabar，并写入市场缓存与持久化队列。
- `src/indicators/`
  - 处理收盘与盘中事件，生成指标快照。
- `src/signals/`
  - 承载策略注册、信号评估、投票、状态机与信号持久化。
- `src/trading/`
  - 执行下单、价格确认入场、持仓跟踪与交易结果追踪。
- `src/calendar/`
  - 经济日历同步与交易窗口保护。
- `src/persistence/`
  - 异步多通道写入 TimescaleDB。
- `src/monitoring/`
  - 运行健康检查、告警与运行时指标落盘。

### 2.2 运行时装配层

- `src/app_runtime/container.py`
  - 纯组件容器，不包含业务逻辑。
- `src/app_runtime/factories/`
  - 负责创建各领域组件。
- `src/app_runtime/builder.py`
  - 负责装配 `AppContainer`，不启动后台线程。
- `src/app_runtime/runtime.py`
  - 负责启动、关闭、监控注册与运行时生命周期。

### 2.3 读模型与展示层

- `src/readmodels/`
  - 统一运行时读模型，向 Admin、Monitoring 等接口提供稳定投影。
- `src/studio/runtime.py`
  - 负责 Studio 服务的装配、事件桥接和展示层注册。

### 2.4 API 层

- `src/api/`
  - 只负责 HTTP 路由、中间件、Schema 与 DI 适配。
  - 所有业务接口统一挂载到 `/v1/...`。
  - 根路径只保留 `/health`。

## 3. 启动流程

应用启动分为两段：

### 3.1 构建阶段

`build_app_container()` 负责创建并连线组件：

```text
MarketDataService
-> StorageWriter
-> BackgroundIngestor
-> UnifiedIndicatorManager
-> EconomicCalendarService
-> TradingModule
-> SignalModule + SignalRuntime + TradeExecutor
-> HealthMonitor + MonitoringManager
-> RuntimeReadModel
-> StudioService
```

### 3.2 运行阶段

`AppRuntime.start()` 按顺序启动后台任务：

```text
1. StorageWriter
2. UnifiedIndicatorManager
3. BackgroundIngestor
4. EconomicCalendarService
5. SignalRuntime
6. ConfidenceCalibrator
7. StrategyPerformanceTracker warmup
8. PendingEntryManager
9. PositionManager
10. MonitoringManager
```

关闭顺序反向执行，并负责回收：

- 线程
- 文件监视器
- SQLite 连接
- Studio 监听器
- 运行时 shutdown callbacks

## 4. 数据供给原则

### 4.1 运行时数据唯一事实源

- `MarketDataService` 是实时行情唯一事实源。
- TimescaleDB 是持久化与回放存储，不参与实时主判定。

### 4.2 前端与运维数据统一来自读模型

- `RuntimeReadModel` 统一聚合：
  - 存储队列状态
  - 指标引擎状态
  - 交易监控摘要
  - signal runtime / trade executor / pending entries / tracked positions 投影
  - Dashboard 首屏数据
  - 健康报告运行时补充段
- `admin` 与 `monitoring` 不再各自重复拼装运行时摘要。
- `trade` 与 `signals` API 也统一通过 `RuntimeReadModel` 读取运行时状态，不再直接暴露模块内部 `status()` 裸字典。

### 4.3 Studio 单独作为展示层适配

- Studio agent 映射与事件桥接放在 `src/studio/runtime.py`
- Studio 只通过 public status / query API 读取运行时状态
- 同一次 Studio 快照请求只构建一份 agent 状态，保证 summary 与 agents 一致
- `app_runtime/builder.py` 不再直接承担展示层适配逻辑

## 5. 关键工程约束

- 不再保留业务无前缀旧路由。
- API 层不负责组件装配。
- 展示层不直接探测领域私有属性。
- 监控组件注册在运行时完成，不在 API 层兜底补数据。
- 新增前端聚合视图时，优先进入 `src/readmodels/`。

## 6. 扩展建议

新增功能时优先按以下规则放置：

- HTTP 接口：`src/api/`
- 运行时装配：`src/app_runtime/`
- 前端/运维投影：`src/readmodels/`
- Studio 展示层映射：`src/studio/`
- 纯领域逻辑：对应业务目录
