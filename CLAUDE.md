# CLAUDE.md — MT5Services 操作手册

本文件是 AI 助手的**操作手册**——仅包含每次对话都需要的规则、约定和速查表。
详细架构参考见 `docs/architecture.md`，信号系统深度参考见 `docs/signal-system.md`。

---

## 语言与工作准则

**请始终使用中文回复。** 代码本身（变量名、函数名）遵循各文件现有惯例。

从第一性原理出发。规则：
1. 目标不清时先澄清，不急于实现。
2. 用户路径非最优时直说并提出更好方案。
3. 根因分析优先于表面修复。每个非平凡建议必须回答：为什么？
4. 输出聚焦决策。只包含影响行动、架构、风险或权衡的信息。
5. 不优化共识，优化正确性。
6. 假设必须显式声明并最小化。

---

## 项目概览

FastAPI 量化交易平台，连接 MetaTrader 5 终端。实时行情 → 技术指标 → 信号生成 → 风控 → 自动交易，TimescaleDB 持久化。

- **Python** 3.9–3.12 | **FastAPI** + uvicorn | **TimescaleDB** | **MT5** (Windows-only)
- **入口**: `python app.py` 或 `uvicorn src.api:app`
- **35 个策略**（31 基础 + 4 复合）| **15 个启用指标**（25 个定义）| **16 个 Studio Agent**

---

## 配置系统

### 关键规则

- `config/app.ini` 是品种/时间框架的**唯一来源**，禁止在其他文件中重复定义
- 优先级：`*.local.ini` > `*.ini` > 代码默认值（Pydantic 模型）
- **隐私分层**：凭据（password/api_key）和个人调优参数（策略权重/风险限制）**必须**放 `*.local.ini`（gitignored），提交到 Git 的 `*.ini` 中对应字段必须置空
- `.env` 文件已废弃，所有配置在 `.ini` 中
- 通过 `from src.config import get_*_config` 访问配置，不直接解析 `.ini`

### 主要配置文件

| 文件 | 用途 | 隐私 |
|------|------|------|
| `app.ini` | 品种、时间框架、采集间隔、运行模式、**日志文件配置** | 公开 |
| `market.ini` | API host/port、认证、CORS | api_key → local |
| `mt5.ini` | MT5 终端连接与账户 | login/password → local |
| `db.ini` | TimescaleDB 连接、**retention policy** | user/password → local |
| `signal.ini` | 信号模块全部可调参数 | 策略调优值 → local |
| `risk.ini` | 风险限制 | 具体阈值 → local |
| `backtest.ini` | 回测与参数优化 | 公开 |
| `storage.ini` | 8 通道队列持久化 | 公开 |
| `indicators.json` | 指标定义与计算流水线 | 公开 |
| `composites.json` | 复合策略组合定义 | 公开 |

---

## 核心架构速查

### 数据链路

```
MT5 → BackgroundIngestor → MarketDataService(内存缓存) → StorageWriter(8通道异步) → TimescaleDB
                                    ↓
                        UnifiedIndicatorManager → 指标快照
                                    ↓
                        SignalRuntime(confirmed优先+intrabar) → 策略评估 → VotingEngine
                                    ↓
                        TradeExecutor → PendingEntryManager → MT5 下单
```

### 应用运行时（src/app_runtime/）

- `container.py` — AppContainer：纯组件持有（flat dataclass）
- `builder.py` — `build_app_container()`：构建所有组件（不启动线程）
- `runtime.py` — AppRuntime：start/stop 生命周期管理
- `mode_controller.py` — 4 种运行模式：FULL/OBSERVE/RISK_OFF/INGEST_ONLY
- `factories/` — 各组件工厂函数
- `src/api/deps.py` 仅作 FastAPI DI 适配层

### 持久化三层

| 层 | 技术 | 用途 |
|---|---|---|
| **SQLite** | events.db (460KB) + signal_queue.db (WAL) | OHLC 事件队列 + confirmed 信号持久化队列 |
| **TimescaleDB** | 26 表, 14 hypertable | 全量业务数据 + 三级 retention policy |
| **内存** | MetricsStore (deque 环形缓冲) | 健康指标（重启后重新采集）|
| **告警** | health_alerts.db (SQLite, 极小) | 告警历史 |
| **日志** | RotatingFileHandler | data/logs/ (100MB×10 + errors.log) |

### 关键模块路径

| 组件 | 路径 |
|------|------|
| AppContainer / AppRuntime | `src/app_runtime/container.py` / `runtime.py` |
| MarketDataService | `src/market/service.py` |
| UnifiedIndicatorManager | `src/indicators/manager.py` |
| SignalModule | `src/signals/service.py` |
| SignalRuntime | `src/signals/orchestration/runtime.py` |
| TradingModule | `src/trading/application/module.py` |
| TradeExecutor | `src/trading/execution/executor.py` |
| PositionManager | `src/trading/positions/manager.py` |
| PendingOrderManager | `src/trading/pending/manager.py` |
| HealthMonitor (内存环形缓冲) | `src/monitoring/health/monitor.py` |
| MetricsStore | `src/monitoring/health/metrics_store.py` |
| PipelineEventBus | `src/monitoring/pipeline/event_bus.py` |
| StorageWriter | `src/persistence/storage_writer.py` |
| TimescaleWriter | `src/persistence/db.py` |
| Retention Policy | `src/persistence/retention.py` |
| SQLite 连接工厂 | `src/utils/sqlite_conn.py` |
| 策略目录 | `src/signals/strategies/catalog.py` |
| 回测引擎 | `src/backtesting/engine/runner.py` |
| Paper Trading | `src/backtesting/paper_trading/bridge.py` |
| RuntimeReadModel | `src/readmodels/runtime.py` |
| StudioService | `src/studio/service.py` |
| 回测 API | `src/api/backtest.py` + `src/api/backtest_routes/` |

---

## 代码规范

### 类型安全
- **mypy strict mode**，所有函数必须有类型注解
- 无充分理由不使用 `Any`
- 数据边界（API 请求/响应、配置）使用 Pydantic v2 模型

### Pydantic v2
- `model_validate()` 而非 `parse_obj()`
- `model_dump()` 而非 `dict()`
- `model_config = ConfigDict(...)` 而非 `class Config:`

### 线程安全
- `MarketDataService` 使用 `RLock`——读写均须持锁
- `StorageWriter` 队列线程安全——只 put 数据
- `SignalRuntime` 使用分片锁防止全局锁争用
- 不得在无锁的情况下跨线程共享可变状态

### 错误处理
- 使用 `AIErrorCode` 枚举（`src/api/error_codes.py`）
- 路由处理器返回 `ApiResponse[T]` 包装器
- 使用 `structlog` 结构化日志（路由层），`logging` 模块（其他）

### DI 流程（新增服务组件）
1. `src/app_runtime/container.py` 的 `AppContainer` 添加字段
2. `src/app_runtime/factories/` 添加工厂函数
3. `src/app_runtime/builder.py` 的 `build_app_container()` 构建
4. `src/app_runtime/runtime.py` 的 `AppRuntime.stop()` 注册关闭
5. `src/api/deps.py` 添加 getter 函数

---

## SOP：常见操作

### 新增指标
1. `src/indicators/core/` 实现函数（`func(bars, params) → dict`）
2. `config/indicators.json` 添加条目（`dependencies` 一律 `[]`）
3. `tests/indicators/` 添加测试

### 新增策略
1. 对应文件实现策略类（必填：`name` / `required_indicators` / `preferred_scopes` / `regime_affinity`）
2. `src/signals/strategies/catalog.py` 注册
3. `src/signals/strategies/__init__.py` 导出
4. `tests/signals/` 添加测试
5. 详细 scope 决策树见 `docs/signal-system.md`

### 新增 API 路由
1. `src/api/<domain>_routes/` 创建路由文件
2. `src/api/<domain>.py` 组合根注册
3. `src/api/error_codes.py` 添加错误码
4. 业务逻辑下沉到服务层或 readmodels，路由层只做协议适配

### 回测调参写入
- **只写 `signal.local.ini`**，不修改 `signal.ini`
- `ConfigApplicator.apply()` 自动备份 + 原子写入 + 内存热更新

---

## 文档同步准则

**提交代码时必须同步更新相关文档。** 每次 commit 前检查：

| 变更类型 | 需要更新的文档 |
|---------|-------------|
| 新增/删除模块或文件 | CLAUDE.md（模块路径表）、`docs/architecture.md` |
| 策略/指标数量变化 | CLAUDE.md（项目概览行）、`docs/signal-system.md` |
| 配置文件结构变化 | CLAUDE.md（配置表）、README.md |
| 架构性重构 | `docs/architecture.md`、CLAUDE.md（Known Issues） |
| 完成 TODO 项 | TODO.md（归档） |
| 新增设计方案 | `docs/design/` 新建文件 |

---

## 测试规范

```
tests/api/          → src/api/
tests/config/       → src/config/
tests/indicators/   → src/indicators/
tests/signals/      → src/signals/
tests/trading/      → src/trading/
tests/integration/  → 端到端流程
```

标记：`@pytest.mark.unit` / `@pytest.mark.integration` / `@pytest.mark.slow`

运行：
```bash
pytest                    # 全部
pytest -m unit            # 仅单元测试
pytest -m "not slow"      # 跳过慢速测试
black src/ tests/ && isort src/ tests/ && mypy src/ && flake8 src/ tests/
```

---

## Git 工作流

- 默认分支：`main`
- 禁止提交密钥——凭据在 `*.local.ini`（gitignored）
- 提交前运行 `black`、`isort`、`mypy`、`flake8`
- **遇 bug 即修**：测试中发现任何失败（含已有 bug）必须立即修复

---

## Known Issues

> 仅列出仍在生效的、影响日常开发的问题。已修复的历史问题见 git log。

- **Hot Reload 限制**：`[regime_detector]` 和 `[strategy_params]` 暂不支持热加载，需重启服务生效
- **Config snapshot at import time**：`src/api/__init__.py` 在导入时一次性读取 API 配置，修改 `market.ini` 后需重启
- **deps.py 初始化无失败标记**：`_ensure_initialized()` 在 `build_app_container()` 抛异常后不设 init_failed 标志，后续请求反复重试
- **回测进程内执行**：当前回测任务通过 FastAPI `BackgroundTasks` 在进程内异步执行，API 重启会丢失正在运行的任务
- **WF 结果内存缓存**：`WalkForwardResult` 对象缓存在内存中，API 重启后丢失
- **generate_report(hours=24)**：内存环形缓冲 ring_size=2400 仅覆盖 ~3.3h，24h 报告窗口内早期数据为空
- **Flaky 集成测试风险**：`test_signal_trade_chain.py` 中的测试依赖 `deps` 模块全局状态隔离，若新增 API 测试触发 `_ensure_initialized()` 可能污染后续测试

---

## 深度参考文档（按需读取）

| 文档 | 内容 | 何时读取 |
|------|------|---------|
| `docs/architecture.md` | 完整架构：启动流程、数据流、模块边界、布局规范 | 架构性重构时 |
| `docs/signal-system.md` | 信号系统：策略列表、Intrabar 链路、投票引擎、置信度管线、状态机 | 新增/修改策略时 |
| `docs/design/pending-entry.md` | PendingEntry 价格确认入场设计方案（已实现） | 修改入场逻辑时 |
| `docs/design/risk-enhancement.md` | PnL 熔断器 + PerformanceTracker 恢复设计（已实现） | 修改风控逻辑时 |
| `docs/design/next-plan.md` | 下一阶段开发规划 | 规划新功能时 |
