# 模块边界与职责定义

> 本文档定义每个模块的**实际职责边界**，标注临时兼容层和计划删除层。
> 目的：新功能放在正确位置，重构时不破坏边界。

---

## 1. 模块总览

```
src/
├── app_runtime/         [运行时] 组件容器、构建、生命周期管理
├── api/                 [API 层] 路由、中间件、DI 适配层、工厂
├── market/              [数据层] 内存行情缓存 + 事件总线
├── ingestion/           [采集层] MT5 数据拉取
├── indicators/          [指标层] 计算流水线与快照发布
├── signals/             [信号层] 策略评估、状态机、投票
├── trading/             [执行层] 下单、持仓、风控执行
├── risk/                [风控层] 规则引擎、风险评估
├── calendar/            [日历层] 经济日历、Trade Guard
├── market_structure/    [结构层] 市场结构分析
├── monitoring/          [监控层] 健康检查、告警
├── persistence/         [持久化] TimescaleDB 写入与查询
├── config/              [配置层] 加载、合并、Pydantic 模型
├── clients/             [客户端] MT5 binding 封装 + 统一时间 API
├── backtesting/         [回测层] 引擎、优化器、Walk-forward
└── utils/               [工具层] 通用工具 + 统一时区模块
```

---

## 2. 各模块详细边界

### 2.1 `src/api/` — API 与应用装配层

| 子模块 | 职责 | 不应做 |
|--------|------|--------|
| `__init__.py` | FastAPI app 创建、CORS、API key、路由注册 | 不应包含业务逻辑 |
| `deps.py` | FastAPI DI 适配层 (getter 函数 + lifespan) | 不应包含组件构建逻辑 |
| `factories/` | 各域组件工厂函数 | 工厂内不应有运行时逻辑 |
| `trade_dispatcher.py` | 统一交易 API 调度 | 不应包含风控判断 |
| `schemas.py` | Pydantic 请求/响应模型 | 不应包含业务校验 |
| 路由文件 | HTTP 端点处理 | 不应直接操作缓存或 DB |

**注意**：
- 组件构建逻辑在 `src/app_runtime/builder.py`
- 生命周期管理在 `src/app_runtime/runtime.py`
- `deps.py` 仅保留 getter 函数 + lifespan 引用
- API 同时挂载 `/v1/` 前缀和无前缀旧路由（向后兼容）

---

### 2.2 `src/market/` — 市场数据缓存

| 职责 | 说明 |
|------|------|
| 内存行情缓存 | Tick/Quote/OHLC/Intrabar 四类缓存 |
| DB fallback 查询 | 缓存不足时回源 TimescaleDB |
| 事件分发 | OHLC close / intrabar listener 注册与广播 |
| 指标附着 | 向 OHLC bar 附加指标值 |

**不应做**：MT5 采集（属于 ingestion）、指标计算（属于 indicators）

**已知问题**：
- 事件总线已提取为 `MarketEventBus`（`src/market/event_bus.py`）
- `get_ohlc()` 是 `get_ohlc_closed()` 的兼容别名

**单一写入者**：BackgroundIngestor（通过 `set_*` 方法）
**多个读取者**：API 路由、IndicatorManager、SignalRuntime

---

### 2.3 `src/ingestion/` — 数据采集

| 职责 | 说明 |
|------|------|
| MT5 数据拉取 | get_quote / copy_ticks / get_ohlc |
| 节流控制 | per-(symbol,tf) 的 next_*_at 时间戳 |
| 写入分发 | → MarketDataService + StorageWriter |

**不应做**：数据缓存（属于 market）、指标计算、信号评估

---

### 2.4 `src/indicators/` — 指标计算

| 子模块 | 职责 | 分解状态 |
|--------|------|---------|
| `manager.py` | 编排器 facade + 生命周期 + 4 个后台线程 | **过重**（1,366 行） |
| `bar_event_handler.py` | closed-bar 事件批处理 | 已分解 |
| `pipeline_runner.py` | 指标管道运行 | 已分解 |
| `result_store.py` | 结果规范化与 LRU 存储 | 已分解 |
| `snapshot_publisher.py` | 快照发布与去重 | 已分解 |
| `core/` | 纯计算函数（mean/momentum/volatility/volume） | 无状态，可复用 |
| `engine/` | 计算引擎（pipeline/dependency/parallel） | 无状态，可复用 |
| `cache/` | 智能缓存、增量计算 | 独立 |
| `monitoring/` | 计算耗时收集 | 独立 |

**已知问题**：
- `manager.py` 中仍有 ~12 个单行转发包装器（可清理）
- 后台线程管理（4 个线程）紧耦合在 manager 中
- Delta 计算、指标选择、reconcile 逻辑仍在 manager 中

**回测复用**：`OptimizedPipeline.compute()` 被回测引擎直接调用（独立实例）

---

### 2.5 `src/signals/` — 信号系统

| 子模块 | 职责 |
|--------|------|
| `service.py` | SignalModule：策略注册、evaluate()、诊断 |
| `models.py` | SignalEvent / SignalContext / SignalDecision / SignalRecord |
| `orchestration/runtime.py` | SignalRuntime：事件驱动评估主循环（**1,744 行**） |
| `orchestration/voting.py` | StrategyVotingEngine：多策略加权投票 |
| `orchestration/policy.py` | SignalPolicy / RuntimeSignalState |
| `evaluation/regime.py` | MarketRegimeDetector（硬/软分类） |
| `evaluation/calibrator.py` | ConfidenceCalibrator（历史胜率校准） |
| `evaluation/performance.py` | StrategyPerformanceTracker（日内实时反馈） |
| `execution/filters.py` | SignalFilterChain（点差/时段/经济/波动率） |
| `strategies/` | 20 个策略实现 + adapters + htf_cache |
| `tracking/` | SignalRepository（信号持久化） |
| `analytics/` | DiagnosticsEngine + 插件 |

**注意**：
- 状态机已提取为 `state_machine.py`（纯逻辑）
- 投票处理已提取为 `vote_processor.py`（纯函数）
- 亲和度已提取为 `affinity.py`（纯函数）
- HTF 解析已提取为 `htf_resolver.py`（纯函数）

**回测复用**：
- `SignalModule.evaluate()` — 直接复用
- `SignalFilterChain.should_evaluate()` — 直接复用（注入 bar.time）
- `MarketRegimeDetector` — 独立实例复用

---

### 2.6 `src/trading/` — 交易执行

| 子模块 | 职责 |
|--------|------|
| `service.py` | TradingModule：账户/持仓/订单生命周期 |
| `trading_service.py` | TradingService：底层下单/平仓/保证金 |
| `signal_executor.py` | TradeExecutor：信号自动下单（异步队列 + daemon） |
| `execution_gate.py` | ExecutionGate：准入检查 |
| `pending_entry.py` | PendingEntryManager：价格确认入场 |
| `position_manager.py` | PositionManager：持仓监控/止损跟踪 |
| `sizing.py` | 仓位计算、时间框架差异化 SL/TP |
| `signal_quality_tracker.py` | SignalQualityTracker：信号预测质量追踪 |
| `trade_outcome_tracker.py` | TradeOutcomeTracker：实际交易盈亏追踪 |

**回测复用**：`sizing.compute_trade_params()`、`position_rules.*` 纯函数直接复用

---

### 2.7 `src/risk/` — 风控规则

| 子模块 | 职责 |
|--------|------|
| `service.py` | PreTradeRiskService：交易前风控聚合 |
| `rules.py` | 具体规则：DailyLossLimit / AccountSnapshot / MarginAvailability / TradeFrequency |
| `models.py` | TradeIntent / RiskCheckResult / RiskAssessment |

**边界**：纯规则判断，不操作缓存或下单

---

### 2.8 `src/persistence/` — 持久化

| 子模块 | 职责 |
|--------|------|
| `storage_writer.py` | 多通道异步队列写入 |
| `db.py` | DB 连接管理 |
| `validator.py` | 数据校验 |
| `schema/` | 各通道数据表 Schema |
| `repositories/` | 按领域分离的数据仓储（trade/signal/economic/market/runtime/backtest） |

---

### 2.9 `src/backtesting/` — 回测与参数优化

| 子模块 | 职责 | 与实盘的关系 |
|--------|------|------------|
| `component_factory.py` | 构建独立 pipeline/SignalModule | **绕过** deps.py，每次独立创建 |
| `engine.py` | 逐 bar 回放主循环 | 复用 evaluate/filters/regime |
| `data_loader.py` | 从 DB 加载历史 OHLC + 指标预计算缓存 | 独立（不经 MarketDataService） |
| `portfolio.py` | 模拟持仓/SL/TP/资金曲线 | 复用 position_rules 纯函数 |
| `optimizer.py` | 网格/随机参数搜索 | 复用 CachedDataLoader + 预计算指标 |
| `walk_forward.py` | 前推验证 IS/OOS | 复用 optimizer + engine |
| `recommendation.py` | 参数推荐引擎 + ConfigApplicator | 写入 signal.local.ini + 热更新 SignalModule |
| `filters.py` | 过滤器模拟 | **直接复用** SignalFilterChain |
| `api.py` | HTTP API（run/optimize/walk-forward/recommendations） | 在进程内异步执行 |
| `config.py` | BacktestConfig（从 backtest.ini 加载，含成本参数） | 独立 |
| `metrics.py` | Sharpe/Sortino/Calmar 计算 | 独立 |
| `models.py` | BacktestResult/Recommendation/ParamChange/BacktestJob | 独立 |

**回测请求支持**：
- `strategy_params`：全局策略参数覆盖
- `strategy_params_per_tf`：按时间框架独立覆盖（与实盘 `[strategy_params.<TF>]` 等效）
- `BacktestRequestBase`：所有请求类型（run/optimize/walk-forward）共用基类

**真实交易成本**（`config/backtest.ini [backtest]`）：

| 参数 | 值 | 说明 |
|------|-----|------|
| `commission_per_lot` | `7.0` | 单边手续费（进出合计 $14/手） |
| `slippage_points` | `15.0` | 典型滑点（XAUUSD 范围 10-30 点）|

**参数推荐工作流**：
```
Walk-Forward 验证 → WalkForwardResult（内存缓存）
    → /recommendations/generate → Recommendation（PENDING）
    → /recommendations/{id}/approve → APPROVED（人工审核）
    → /recommendations/{id}/apply → ConfigApplicator → signal.local.ini + 热更新
    → /recommendations/{id}/rollback → 恢复备份 + 热更新
```

**安全约束**：overfitting_ratio > 1.5 或 consistency_rate < 50% 时拒绝生成；单参数变更裁剪 ±30%

---

## 3. 跨模块依赖矩阵

```
                market  ingestion  indicators  signals  trading  risk  persistence  backtesting
market            -                                                        R
ingestion         W        -                                               W
indicators        RW                  -                                    W
signals           R                   R           -                        W
trading           R                               R        -      R       W
risk                                                       R      -
persistence                                                                -
backtesting                           R(独立)     R(独立)  R(纯函)          R

W = 写入, R = 读取, RW = 读写
"独立" = 创建独立实例，不共享全局单例
"纯函" = 仅使用纯函数，不依赖运行时状态
```

---

## 4. 构建方式对比

| 场景 | 入口 | 使用 deps.py? | 组件来源 |
|------|------|:------------:|---------|
| **生产 API** | `app.py` → lifespan | 是 | `AppContainer` (via `AppRuntime`) |
| **回测 API** | `/v1/backtest/run` | 否 | `build_backtest_components()` 独立创建 |
| **回测 CLI** | `python -m src.backtesting` | 否 | 同上 |
| **参数优化** | `/v1/backtest/optimize` | 否 | 每组参数独立创建 SignalModule |
| **Walk-Forward** | `/v1/backtest/walk-forward` | 否 | optimizer + engine 滚动窗口 |
| **参数推荐应用** | `/v1/backtest/recommendations/{id}/apply` | 部分 | 写 signal.local.ini + 可选热更新 SignalModule |
| **单元测试** | pytest | 否 | Mock 对象 |
| **集成测试** | pytest | 部分 | 混合 mock + 真实组件 |

---

### 2.10 `src/monitoring/` — 健康监控（已优化）

| 特性 | 说明 |
|------|------|
| 持久化连接 | `_get_conn()` 返回单例 SQLite 连接，消除 `record_metric` 每次 connect/close 开销 |
| WAL 模式 | `PRAGMA journal_mode=WAL` + `synchronous=NORMAL` + `busy_timeout=30000` |
| 写缓冲 + 后台刷盘 | `_write_buffer` 积攒指标行，后台线程每 5s 批量 INSERT；缓冲满 500 条立即刷盘 |
| 告警同步写 | `_record_alert()` 绕过缓冲，同步执行，确保不丢失 |
| `NullHealthMonitor` | 回测/批量优化场景使用，满足完整接口但零 I/O |

### 2.11 `src/utils/event_store.py` — 指标事件 Durable Store（已优化）

| 特性 | 说明 |
|------|------|
| 持久化连接 | `_get_conn()` 消除每次操作的 connect/close 开销 |
| WAL + cache | 8MB 页缓存，busy_timeout 30s |
| Stats 延迟写 | 每 20 次操作才落盘（`_STATS_FLUSH_EVERY = 20`），降低写放大 |
| 原子领取 | `claim_next_events()` 用 `UPDATE…RETURNING` CTE，一条 SQL 完成原子操作 |
| 显式 close | `close()` 方法刷新 pending stats 并关闭连接 |

---

## 5. 临时兼容层清单

| 位置 | 兼容内容 | 建议 |
|------|---------|------|
| `src/api/__init__.py` | 无前缀旧路由（与 `/v1/` 重复） | 设定废弃窗口，后续移除 |
| `src/market/service.py` | `get_ohlc()` 别名 | 统一到 `get_ohlc_closed()` |

---

## 6. 模块放置决策树

```
新功能放在哪里？

Q1: 是否是 HTTP 端点？
  → 是 → src/api/ 路由文件

Q2: 是否是纯数据计算（给定输入→输出，无副作用）？
  → 指标计算 → src/indicators/core/
  → 风控规则 → src/risk/rules.py
  → 统计指标 → src/backtesting/metrics.py

Q3: 是否需要 MT5 终端连接？
  → 数据采集 → src/ingestion/
  → 下单执行 → src/trading/

Q4: 是否是信号策略？
  → 策略实现 → src/signals/strategies/
  → 过滤器 → src/signals/execution/filters.py
  → 评估逻辑 → src/signals/evaluation/

Q5: 是否是配置相关？
  → Pydantic 模型 → src/config/models/
  → 加载器 → src/config/

Q6: 是否是数据库交互？
  → 写入 → src/persistence/storage_writer.py（队列）
  → 查询 → src/persistence/repositories/
```
