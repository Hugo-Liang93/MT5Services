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
- **8 个策略**（7 结构化策略 + 1 HTF 变体）| **25 个启用指标**（37 个定义，12 个停用）| **16 个 Studio Agent**

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
| `research.ini` | 信号挖掘参数（BH-FDR/排列检验/Rolling IC/特征工程/Regime 分层） | 公开 |

---

## 核心概念（每次对话必须理解的语义定义）

### Intrabar 的准确含义

**intrabar ≠ 每一笔 tick**。正确理解是：**当前未收盘 K 线在一定频率间隔下的 OHLC 快照更新**。

| 属性 | 说明 |
|------|------|
| 触发方式 | BackgroundIngestor 主动轮询 `client.get_ohlc(symbol, tf, 1)` 获取当前未收盘 bar |
| 采集节流 | 每个 `(symbol, tf)` 独立 `next_intrabar_at`；间隔 = per-tf config > global `intrabar_interval` > auto `max(5s, bar时长×5%)` |
| 数据内容 | N-1 根完整收盘 K 线 + 当前 bar（含最新 tick 的 OHLC 值）|
| 去重 | Ingestor 层：OHLC 值与上次相同则跳过；指标层：快照与上次完全相同则不发布 |
| 可靠性 | best-effort：队列满则丢弃，confirmed 积压时延迟处理 |

> 当价格在两次间隔内无实质变化（OHLC 未变 → 指标不变），不触发新事件，策略 preview 状态机保持原状。

### Confirmed vs Intrabar 双链路

```
confirmed 链路（L1 不可丢弃）：
  bar 收盘 → set_ohlc_closed() → events.db 持久化 → 全部 15 个指标
  → scope="confirmed" → _confirmed_events 队列（2048，优先消费）
  → 所有策略评估 → 可触发实际交易

intrabar 链路（L3 best-effort）：
  节流轮询 → set_intrabar() → 仅 8 个自动推导指标
  → scope="intrabar" → _intrabar_events 队列（4096，confirmed 空时才消费）
  → 仅 preferred_scopes 含 "intrabar" 的 8 个策略
  → preview/armed 状态机预热 → 不直接触发交易
```

### 事件持久性三级分类

| 等级 | 要求 | 事件类型 | 实现 |
|------|------|---------|------|
| **L1 (Durable)** | 必须持久化，不可丢失 | confirmed bar close、trade signal、order execution、risk block | SQLite events.db + DB 写入 + backpressure 重试 |
| **L2 (Recoverable)** | 允许丢失但可回放恢复 | indicator snapshot、reconcile、OHLC 持久化 | StorageWriter 队列（block 策略）+ 定时 reconcile 兜底 |
| **L3 (Best-effort)** | 最佳努力，丢失可接受 | intrabar preview、调试快照、部分监控指标 | put_nowait 队列满则丢弃 + 丢弃计数器 |

### 置信度管线（修正链 — 顺序关键）

```
=== 结构化策略管线（统一评分框架） ===
raw_confidence = base(0.50)
    + why_score(0~1) × 0.15            (方向确认质量)
    + when_score(0~1) × 0.15           (时机精度)
    + where_score(0~1) × 0.10          (结构位质量)
    + vol_score(0~1) × 0.05            (量能确认)
    → cap 0.90
    × effective_affinity               (Regime 结构过滤，SoftRegime 加权)
    → max(confidence_floor, result)    (底线保护)
    = final_confidence
    注：PerformanceTracker/Calibrator 待回测通过后通过装饰器接入
```

### 10 层风控堆栈（从信号到执行，有序）

1. **SignalFilterChain** — 时段/冷却期/点差/经济事件/波动率异常/趋势衰竭过滤
2. **Regime 门控** — 结构化策略在 _why() 内部自行判断 regime 适应性（ADX）；regime_affinity 仅在置信度管线中作为乘数使用
3. **HTF direction alignment** — 策略级分层：trend_continuation 硬门控（趋势延续需顺势）；breakout_follow/range_reversion 软门控（冲突时阻止）；session_breakout/trendline_touch 软加分（冲突时降分不拒绝）；sweep_reversal/lowbar_entry 不使用
4. **ExecutionGate** — voting group 保护、require_armed 门控
5. **PendingEntry** — 策略输出 entry_spec（market/limit/stop + 入场价），PendingEntryManager 纯执行
6. **Pre-trade risk service** — DailyLossLimit / AccountSnapshot / MarginAvailability / TradeFrequency
7. **Executor safety** — 技术熔断器 + 持仓数量预检 + spread_to_stop_ratio 检查
8. **ExposureCloseout** — 日终自动平仓 + 挂单撤销
9. **PositionManager + Chandelier Exit** — 统一出场规则（`exit_rules.py`）：初始 SL / Chandelier trailing（current_ATR 动态）/ breakeven + 梯度锁利 / 信号反转 N-bar 确认 / 超时 / 日终平仓。Regime-aware：按 (category × regime) 选择出场 profile
10. **RegimeSizing** — TF 差异化 SL/TP（M5:1.8/2.5 ~ D1:2.5/3.5 ATR）+ TF 差异化风险%（M5:×0.50 ~ D1:×1.50）+ per-TF min_confidence

### Warmup 屏障

- **回补期间**：`BackgroundIngestor.is_backfilling=True` 时，所有 confirmed/intrabar 快照不入队、不评估、不产生信号
- **Staleness 校验**：回补结束后，`bar_time` 距当前时间不超过 `2×tf_seconds + 30s` 才解除屏障
- **指标完整性**：`SignalPolicy.warmup_required_indicators = ("atr14",)`，ATR 尚未计算时仍跳过
- 回补期间指标正常计算（API 可查询），仅信号评估被屏蔽

---

## 核心架构速查

### 数据链路

```
MT5 → BackgroundIngestor → MarketDataService(内存缓存) → StorageWriter(8通道异步) → TimescaleDB
                                    ↓
                        UnifiedIndicatorManager → 指标快照（含 volume 指标）
                                    ↓
                        MarketStructureAnalyzer → 结构上下文（7 种状态）
                                    ↓
                        SignalRuntime(confirmed优先+intrabar)
                          → 结构化策略评估（Why/When/Where 三层 + signal_grade A/B/C）
                                    ↓
                        TradeExecutor → entry_type=market? 直接下单 : PendingEntryManager 挂单
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
| 统一出场规则（Chandelier Exit） | `src/trading/positions/exit_rules.py` |
| PendingOrderManager | `src/trading/pending/manager.py` |
| HealthMonitor (内存环形缓冲) | `src/monitoring/health/monitor.py` |
| MetricsStore | `src/monitoring/health/metrics_store.py` |
| PipelineEventBus | `src/monitoring/pipeline/event_bus.py` |
| StorageWriter（含 StorageChannel 枚举） | `src/persistence/storage_writer.py` |
| TimescaleWriter | `src/persistence/db.py` |
| Retention Policy | `src/persistence/retention.py` |
| SQLite 连接工厂 | `src/utils/sqlite_conn.py` |
| 策略目录 | `src/signals/strategies/catalog.py` |
| MetadataKey 常量 | `src/signals/metadata_keys.py` |
| 结构化策略基类 | `src/signals/strategies/structured/base.py` |
| 结构化策略（7 策略） | `src/signals/strategies/structured/` |
| MarketStructureAnalyzer | `src/market_structure/analyzer.py` |
| 回测引擎 | `src/backtesting/engine/runner.py` |
| 回测信号评估 | `src/backtesting/engine/signals.py` |
| Paper Trading | `src/backtesting/paper_trading/bridge.py` |
| Paper Trading 验证对比 | `src/backtesting/paper_trading/validation.py` |
| Volume 指标（vwap/obv/mfi/ratio） | `src/indicators/core/volume.py` |
| Bar 统计指标 | `src/indicators/core/bar_stats.py` |
| 信号挖掘 Runner | `src/research/runner.py` |
| DataMatrix | `src/research/data_matrix.py` |
| 挖掘分析器（预测力/阈值/规则） | `src/research/analyzers/` |
| 特征工程框架 | `src/research/feature_engineer.py` |
| 过拟合防护（BH-FDR/排列检验） | `src/research/overfitting.py` |
| 跨 TF 一致性分析 | `src/research/cross_tf_analyzer.py` |
| 实验追踪仓储 | `src/persistence/repositories/experiment_repo.py` |
| Research 持久化仓储 | `src/persistence/repositories/research_repo.py` |

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

### 新增策略（结构化策略）
1. `src/signals/strategies/structured/` 新建文件，继承 `StructuredStrategyBase`
2. 声明 `htf_policy = HtfPolicy.XXX`（HARD_GATE/SOFT_GATE/SOFT_BONUS/NONE，注册时校验）
3. 实现 `_why()` + `_when()`（硬门控），可选 `_where()` + `_volume_bonus()`（软门控）
4. 实现 `_entry_spec()`（入场规格：market/limit/stop + 入场价 + zone_atr）
5. `src/signals/strategies/structured/__init__.py` 导出
6. `src/signals/strategies/catalog.py` 注册
7. `signal.ini` + `signal.local.ini` 的 `[strategy_timeframes]` **必须同时添加**
8. `tests/signals/` 添加测试
9. 横切关注点（performance/calibrator）通过装饰器接入，**不在策略内部重复实现**

### 策略生命周期管理

策略有三种状态：**活跃 → 冻结 → 废弃**。

**冻结（Freeze）**：回测表现不佳 → `signal.local.ini` 中 `[regime_affinity.<name>]` 全设 0.0
**废弃（Deprecate）**：从 `[strategy_timeframes]` 移除所有 TF 条目 → 不加载

> 2026-04-06: Legacy 策略（35 个）和复合策略（4 个）已全部移除，代码历史保留在 git。
> 当前仅保留 7 个结构化策略（8 个注册实例含 trend_h4 变体）。

### 新增 API 路由
1. `src/api/<domain>_routes/` 创建路由文件
2. `src/api/<domain>.py` 组合根注册
3. `src/api/error_codes.py` 添加错误码
4. 业务逻辑下沉到服务层或 readmodels，路由层只做协议适配

### 信号挖掘准则
1. **必须多 TF 挖掘** — 不得在单 TF 上得出结论。最少跑 M15/M30/H1 三个 TF
2. **跨 TF 一致性分类**：
   - **Robust**（2+ TF 方向一致）：可纳入策略开发，推荐 IC 最强的 TF
   - **Divergent**（2+ TF 方向翻转）：各 TF 含义不同，需独立评估
   - **TF-specific**（仅 1 TF）：需谨慎，可能是统计噪音
3. **最佳 TF 推荐公式**：`score = |IC| × (1 + IR_bonus) × perm_bonus × sample_bonus`
4. **CLI 用法**：`python tools/mining_runner.py --tf M15,M30,H1 --compare`
5. **跨 TF 分析器**：`src/research/cross_tf_analyzer.py`

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

- **Config snapshot at import time**：`src/api/__init__.py` 在导入时一次性读取 API 配置（`@lru_cache`），修改 `market.ini` 后需调用 `/v1/admin/reload-config` 或重启
- **回测进程内执行**：当前回测任务通过 FastAPI `BackgroundTasks` 在进程内异步执行，API 重启会丢失正在运行的任务
- **WF 结果内存缓存**：`WalkForwardResult` 对象缓存在内存中（上限 50），API 重启后丢失
- **generate_report(hours=24)**：内存环形缓冲 ring_size=2400 仅覆盖 ~3.3h，24h 报告窗口内早期数据为空
- **Flaky 集成测试风险**：`test_signal_trade_chain.py` 中的测试依赖 `deps` 模块全局状态隔离，若新增 API 测试触发 `_ensure_initialized()` 可能污染后续测试

### 代码质量改进（2026-04-07）

- **MetadataKey 中心化**：42 个 metadata 魔法字符串统一为 `MetadataKey` 常量（`src/signals/metadata_keys.py`），覆盖 signals/trading/backtesting 全链路（20+ 文件）
- **PipelineEventBus 同步化**：`emit()` 从异步队列+后台线程改为同步分发，匹配 docstring 设计意图，消除测试竞态
- **SignalRuntime 拆分**：warmup 屏障逻辑提取至 `runtime_warmup.py`（150 行），metadata 组装提取至 `runtime_metadata.py`（96 行），runtime.py 从 1148 → 1036 行
- **indicators.json 对齐**：mfi14/obv30 标记 enabled=false（无策略依赖），定义 37 / 启用 25

### 长期运行稳定性（2026-04-06 修复）

以下问题已在本轮修复中解决，记录防护机制供后续维护参考：

- **EOD 跨日自动恢复**：`RuntimeModeAutoTransitionPolicy.resolve_session_start()` 检测新交易日自动恢复 `initial_mode`，仅对 EOD 自动降级生效（手动切换不受影响）
- **apply_mode 异常保护**：`RuntimeComponentRegistry.apply_mode()` 每个组件 start/stop 独立 try/except，部分失败仍更新模式状态，错误记入 `last_error`
- **TradeExecutor 双线程防护**：`stop()` 超时后保留线程引用，`start()` 等待僵尸线程退出后才 clear `_stop_event`，`_start_worker()` 有存活检查
- **WAL Queue 重入**：`close()` 后 `reopen()` 重建连接并 reset in-flight 事件；`_get_conn()` 检查 `_closed` 标志防止静默重连
- **IndicatorManager 线程守卫**：`_any_thread_alive()` 检查全部 4 线程；`stop()` 清引用并记录超时警告
- **PositionManager 启动同步**：`start()` 中先执行一次 `_reconcile_with_mt5()`，修复 stop 期间 peak_price 等状态断档
- **DLQ 文件清理**：`_cleanup_stale_dlq()` 在启动时清理超过 7 天的失败文件
- **listener_fail_counts 清理**：`remove_signal_listener()` 中同步清理对应 key

---

## 深度参考文档（按需读取）

| 文档 | 内容 | 何时读取 |
|------|------|---------|
| `docs/architecture.md` | 完整架构：启动流程、数据流、模块边界、布局规范 | 架构性重构时 |
| `docs/signal-system.md` | 信号系统：策略列表、Intrabar 链路、投票引擎、置信度管线、状态机 | 新增/修改策略时 |
| `docs/design/pending-entry.md` | PendingEntry 设计方案（A7 已将入场职责回归策略层） | 修改入场逻辑时 |
| `docs/design/risk-enhancement.md` | PnL 熔断器 + PerformanceTracker 恢复设计（已实现） | 修改风控逻辑时 |
| `docs/design/next-plan.md` | 下一阶段开发规划 | 规划新功能时 |
| `docs/research-system.md` | 挖掘系统：三大分析器、过拟合防护、多 TF 准则、指标三层架构 | 新增/修改挖掘逻辑时 |
