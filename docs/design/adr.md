# 架构决策记录（ADR）

> **本文件记录已沉淀的设计决策。修改涉及 ADR 的组件前先读对应条目，了解历史上下文。**
> ADR 不是绝对禁令——架构演进时评估清楚后可以变更，但须说明理由并更新本文件。
> 新增决策时复制模板，编号递增。

---

## ADR 索引

| 编号 | 组件 | 决策摘要 | 状态 |
|------|------|---------|------|
| 001 | PipelineEventBus | 保持同步 dispatch，禁止改回异步 | 已确定 |
| 002 | SignalRuntime | warmup/metadata 提取为纯函数模块 | 已确定 |
| 003 | MetadataKey | 魔法字符串必须使用 MetadataKey 常量 | 已确定 |
| 004 | 组件生命周期 | start/stop 安全契约（8 项防护机制） | 已确定 |
| 005 | 后台线程生命周期 | join 超时后不得清空仍存活线程引用 | 已确定 |
| 006 | 跨模块边界 | 装配/API 层禁止读写组件私有属性，必须通过公开端口 | 已确定 |
| 007 | Research / Backtesting 边界 | Research 负责发现（含特征晋升），Backtesting 负责验证，晋升通道待补齐 | 拟定中 |

---

## ADR-001: PipelineEventBus 必须保持同步 dispatch

**状态**：已确定（2026-04-07）

**上下文**：`PipelineEventBus.emit()` 经历了三次同步/异步切换：
1. `f44e0e1` — 从异步队列改为同步内联 dispatch
2. `7b5d1dc` — 回退到异步（`queue.Queue` + 后台 `_dispatch_loop` 线程）
3. `5bb060c` — 再次改回同步

异步版本引入了：队列满 `put_nowait` 丢事件风险、后台线程生命周期管理、测试竞态条件（14 个测试失败）。

**当前决策**：`emit()` 保持 **同步内联 dispatch**。理由：
- 当前所有 listener 都是轻量内存操作（MetricsStore deque 写入、HealthMonitor 计数器更新），无 I/O 阻塞
- 同步语义简单可靠，测试无需 flush/sleep 等待
- 异步队列的丢弃风险与 PipelineEventBus「观测不影响业务」的定位矛盾——观测数据不该因队列满而丢失

**演进方向**：若将来新增慢 listener（如 WebSocket 推送），优先让 listener 自行内部缓冲（自带队列+消费线程），而非改 bus。如果 listener 数量增长到同步 dispatch 成为瓶颈，可重新评估此决策。

---

## ADR-002: SignalRuntime warmup/metadata 提取为纯函数模块

**状态**：已确定（2026-04-07）

**上下文**：`runtime.py` 的 `_on_snapshot()` 包含 ~130 行混合逻辑（warmup 屏障判断 + metadata 组装 + 入队），难以独立测试。

**决策**：
- `runtime_warmup.py`：warmup 屏障逻辑（backfilling/staleness/指标完整性/intrabar 前置），纯判断函数
- `runtime_metadata.py`：snapshot metadata 组装（trace_id/spread/bar_progress），纯函数不依赖 runtime 状态
- `runtime.py` 的 `_on_snapshot()` 退化为 ~20 行编排代码

**约束**：提取的模块不持有状态，副作用（计数器更新、集合操作）仍由 runtime 侧控制。warmup 模块接收 runtime 引用但仅访问明确的公开属性。

**扩展原则**：若 runtime.py 再次膨胀，继续沿此模式提取——纯函数模块 + runtime 编排。不引入新的有状态中间层。

---

## ADR-003: MetadataKey 常量必须覆盖所有 signal metadata 字段

**状态**：已确定（2026-04-07）

**上下文**：signals/trading/backtesting 链路中有 42 个 metadata 魔法字符串（如 `"confidence"`, `"scope"`, `"bar_time"`），分散在 20+ 文件中。拼写错误和不一致曾导致隐性 bug。

**决策**：
- 所有 metadata key 必须通过 `src/signals/metadata_keys.py` 的 `MetadataKey` 常量引用
- 新增 metadata 字段时，**先在 MetadataKey 中定义常量，再使用**
- 避免在代码中直接使用 metadata 字符串字面量

---

## ADR-004: 组件生命周期安全契约

**状态**：已确定（2026-04-06）

**上下文**：长期运行中发现多个组件的 start/stop 存在竞态、资源泄漏和状态断档问题。经过系统修复后沉淀为安全契约。

**当前决策**：以下防护机制在重构时应保留，移除前须评估影响：

| 组件 | 防护机制 | 为什么不能删 |
|------|---------|------------|
| RuntimeComponentRegistry | `apply_mode()` 每个组件 start/stop 独立 try/except | 部分失败不应导致整体模式切换失败 |
| RuntimeModeAutoTransitionPolicy | `resolve_session_start()` EOD 跨日自动恢复 `initial_mode` | 仅对 EOD 自动降级生效，手动切换不受影响 |
| TradeExecutor | `stop()` 超时后保留线程引用 + `start()` 等待僵尸线程退出 | 防止双线程并发消费 |
| WAL Queue | `close()` 后 `reopen()` reset in-flight + `_get_conn()` 检查 `_closed` | 防止静默重连导致数据丢失 |
| IndicatorManager | `_any_thread_alive()` 检查全部 4 线程 + `stop()` 清引用 | 防止僵尸线程 |
| PositionManager | `start()` 先执行一次 `_reconcile_with_mt5()` | 修复 stop 期间 peak_price 状态断档 |
| StorageWriter | `_cleanup_stale_dlq()` 启动时清理 >7 天失败文件 | 防止 DLQ 无限积累 |
| SignalRuntime | `remove_signal_listener()` 同步清理 `listener_fail_counts` | 防止内存泄漏 |

---

## ADR-005: 后台线程 join 超时后必须保留仍存活线程引用

**状态**：已确定（2026-04-10）

**上下文**：审查发现多个后台组件在 `stop()` / `shutdown()` 中执行 `thread.join(timeout)` 后，无论线程是否仍存活都会把线程引用置为 `None`。如果线程因为 I/O、队列等待、MT5 调用或外部依赖卡住，后续 `start()` 可能创建新线程，造成双线程消费、重复 listener、重复状态恢复或竞态写入。

**决策**：
- `join(timeout)` 之后必须检查 `thread.is_alive()`。
- 只有线程已退出时才清空引用。
- 线程仍存活时必须保留引用、记录 warning，并让 `is_running()` 反映真实存活状态。
- 新增后台线程组件应优先复用统一 lifecycle helper，避免各组件自行实现不一致的 stop 语义。

**当前待整改对象**：详见 `docs/codebase-review.md` 的生命周期章节，重点检查 `SignalRuntime`、`PositionManager`、`PendingEntryManager`、`UnifiedIndicatorManager`。

**演进方向**：将 `TradeExecutor` 已采用的僵尸线程防护收敛为通用契约，并为每个后台线程组件补 stop 超时回归测试。

---

## ADR-006: 跨模块边界禁止读写私有属性

**状态**：已确定（2026-04-10）

**上下文**：审查发现装配层（`builder.py`、`factories/signals.py`）和 API 层大量直接读写组件内部的 `_` 私有属性。这使得组件的内部实现细节泄漏到外部，后续重构（如重命名内部字段、修改初始化顺序）极易引发隐性 breakage。

典型反模式：
```python
# ❌ 装配层直接写入私有属性
signal_runtime._pipeline_event_bus = bus
calibrator._recency_hours_by_tf.update(config)
trade_executor._intrabar_guard = guard

# ❌ API 层直接读取私有属性
runtime._confirmed_events
read_model._storage_writer.db
```

**决策**：

1. **装配层（builder / factories）不得读写组件的 `_` 前缀属性。** 需要后置注入的依赖，组件必须提供以下公开端口之一：
   - 构造函数参数（首选）
   - `set_xxx()` setter 方法（后置注入）
   - 公开属性（`self.xxx`，无下划线，在 `__init__` 中初始化为 `None`）

2. **API 层不得读取组件的 `_` 前缀属性。** 诊断/监控数据通过以下方式暴露：
   - `describe_xxx()` 方法（返回结构化字典）
   - `status()` 方法（已有模式）
   - 公开只读 property

3. **热重载（hot reload）不得直接修改组件私有字段。** 必须通过 `update_xxx()` 公开方法，由组件自行处理内部状态一致性。

4. **同包内部模块**（如 `indicators/bar_event_handler.py` 访问 `IndicatorManager`）允许通过公开属性交互，但仍禁止 `_` 前缀访问。公开属性以 `getattr(obj, "attr", None)` 模式兼容可选依赖。

**已整改组件清单**：

| 组件 | 新增公开端口 |
|------|------------|
| SignalRuntime | `pipeline_event_bus` 属性, `set_warmup_ready_fn()`, `set_intrabar_trade_coordinator()` |
| UnifiedIndicatorManager | `pipeline_event_bus` 属性, `current_trace_id` 属性 |
| SignalModule | `strategies` property, `set_tf_param_resolver()` |
| ConfidenceCalibrator | `update_recency_config()` |
| MarketRegimeDetector | `update_thresholds()` |
| StrategyPerformanceTracker | `update_config()` |
| PositionManager | `set_sl_tp_history_writer()` |
| TradeExecutor | `set_intrabar_guard()`, `update_execution_gate_config()` |
| PaperTradingBridge | `session` property |
| RuntimeReadModel | `db_writer` 构造参数 |
| ConfigFileWatcher | `iter_config_files()` (renamed), `is_running()` |

**演进方向**：新增组件如需后置注入，优先在构造函数中声明 Optional 参数。`set_xxx()` 仅用于真正的循环依赖或生命周期时序约束。长期目标：收敛到纯构造函数注入，消除所有 setter。

---

## ADR-007: Research 与 Backtesting 的职责边界 + 特征晋升通道

**状态**：拟定中（2026-04-17）

**上下文**：Step 2.1 M15/M30 基线挖掘暴露出一个结构性问题——挖掘输出的"高胜率 sell rules"在 XAUUSD 12 个月 +45% 牛市中不能直接作为入场策略使用（会系统性做空趋势）。深入核查发现两类边界不清：

1. **Research 与 Backtesting 的职责没有明文划分**。挖掘产出的 descriptive findings 被错当成 actionable 策略候选，跳过了"可交易性验证"环节。
2. **特征晋升通道（`decision=promote_indicator`）只生成建议报告，没有执行路径**：
   - `src/research/features/promotion.py` 只构建 `FeaturePromotionReport` dataclass，不触及文件系统
   - `mining_runner` 只打印 decision，不调用任何晋升动作
   - 没有写入 `indicators.json` / 生成指标代码 / 注册 registry 的任何代码路径

**决策**：固化两大模块的职责边界，并明确特征晋升通道的设计意图（即使部分能力尚未实现）。

### Research（挖掘）模块职责

| 代号 | 职责 | 输出 |
|------|------|------|
| A1 | 发现新策略候选 | 结构完整的策略 spec 雏形（entry + exit + filter + HTF policy），非裸"特征 → 方向" |
| A2 | 发现新特征 | Feature Providers 原始产物 |
| A3 | 特征晋升为指标 | 将 Robust + 多窗口稳定特征推入 indicator 管道 |
| A4 | 市场统计特性描绘 | Regime 分布、session 特性、尾部事件等"市场地图" |

共同特征：全部是 **descriptive + exploratory**，产出未经实盘/Paper 验证的候选。

### Backtesting（回测）模块职责

| 代号 | 职责 | 输出 |
|------|------|------|
| B1 | 已知策略参数优化 | Grid/Bayesian 最优参数组合 |
| B2 | 策略整体验证 | PF / Sharpe / Walk-Forward 稳定性 |
| B3 | 策略组合一致性 | 多策略相关性、叠加效应 |
| B4 | 实盘可执行性模拟 | execution_feasibility mode + 动态点差/滑点 |

共同特征：全部是 **actionable + validation**，输入"策略 spec"，输出"是否值得上 Paper"。

### 模块间协作契约

```
Research 终点 = 未验证但结构完整的策略 spec（含 exit/filter/HTF policy）
Backtesting 起点 = 该 spec + 参数网格
```

**禁止的跨界模式**：
- ❌ Research 输出 descriptive finding 直接当 actionable 策略上 Paper（Step 2.1 教训）
- ❌ Backtesting 硬编码只能由 Research 发现的特征组合
- ❌ 晋升决策（promote_indicator）靠人记忆跟踪，不形成可审计记录

### 特征晋升通道（A3 展开）

**两类特征必须区分对待**：

| 类型 | 定义 | 晋升成本 | 当前支持 |
|------|------|---------|---------|
| **组合型** | 现有指标字段的条件组合（如 `body_ratio > 0.5 AND adx > 23`） | 零——策略直接引用字段 | ✅ 可用 |
| **计算型** | 需要独立 Python 计算函数的新指标（如动态 z-score、复合统计量） | 四步：写 `src/indicators/core/`、加 `indicators.json`、写测试、触发 pipeline 重算 | ❌ 仅手工 |

`mining_runner --emit-feature-candidates` 输出应标注特征类型——`promote_indicator` 决策对计算型有意义，对组合型是冗余标签。

### 当前实现缺口

1. `promotion.py` 只生成 `FeaturePromotionReport`，**没有晋升执行器**
2. 没有 `src/ops/cli/promote_feature.py` 之类的半自动化工具
3. `FeatureCandidateSpec` **未标注"组合型 vs 计算型"**，决策语义模糊
4. 没有晋升历史记录持久化机制

**演进方向**：

- **短期（1 周内）**：在 `FeatureCandidateSpec` 加 `feature_kind: "derived" | "computed"` 字段；补充 `docs/sop/feature-to-indicator-promotion.md` 手工流程文档
- **中期（1-2 月）**：实现 `src/ops/cli/promote_feature.py`，半自动生成指标代码骨架 + 配置 diff，由人审 PR
- **长期**：挖掘 → 策略 spec → 自动回测 → CI 合并的端到端管线

---

## ADR-008: 持久化 pool 单例 + 关键运行时线程 fail-fast + /health 只读契约

**状态**：已确定（2026-04-21）

**上下文**：2026-04-20 生产事故：服务启动约 1h20m 后 `connection pool exhausted`，22:44 短时间内连接池被 8 次重建（每次 1-2 连接）。约 23 分钟后（23:07）`indicator writer thread` 在 DB 异常中静默死亡，主进程继续跑 8.5 小时"僵尸状态"——ingestor 在推，但 confirmed bar 完全不处理，supervisor 无感知。早晨重新流量回来时 intrabar queue 秒满，/health 因同步调 `mt5.initialize/login` 被堵 → 所有 HTTP 端点 2s 超时返回 HTTP 000。追根溯源有 3 个独立 bug：

1. **API 路由每请求 `new TimescaleWriter(1,2)` + `ensure_schema()`**——`src/api/{research,experiment,backtest}_routes/*.py` 里 `_get_xxx_repo()` 函数为每个请求独立起 pg 连接池和 DDL，吃光 pg 连接后诱发连锁故障。
2. **indicator runtime 4 个主循环无顶层异常捕获**——`src/indicators/runtime/event_loops.py` 的 `run_event_loop / run_intrabar_loop / run_event_writer_loop / run_reload_loop` 一旦抛未捕获异常，daemon thread 静默退出，主进程继续跑（ADR-004 的 `_any_thread_alive()` 检查只在 stop 流程里调，运行时不检测）。
3. **`client.health()` 默认 `attempt_initialize=True, attempt_login=True`**——MT5 假死时 HTTP 线程池（40）会被全部堵在 `mt5.initialize()`（timeout 60s），观测性完全失效。

**决策**：

1. **持久化 repo 必须走 TimescaleWriter @property 单例**：`research_repo / experiment_repo / backtest_repo` 在 `src/persistence/db.py` 暴露为 lazy `@property`（等同既有 `market_repo / signal_repo` 等）。`StorageWriter.ensure_schema_ready()` 启动时一次性调所有 repo `ensure_schema()`。API 路由通过 `deps.get_research_repo / get_experiment_repo / get_backtest_repo` 复用 `container.storage_writer.db.<repo>`。**禁止在请求路径 `new TimescaleWriter`**，禁止在请求路径触发 `ensure_schema` DDL。违反此规则的 PR 必须被 block。
2. **关键运行时主循环必须顶层 fail-fast**：`event_loops.py` 四个 run_*_loop 函数体必须 `try: _body(manager) except BaseException as exc: _on_thread_crash(name, exc)`；`_on_thread_crash` 生产下 `os._exit(1)` 让 supervisor 拉起整个进程。**守恒式"except 后重入循环"是禁止的**——静默吞异常 + 不可观测破坏是此次事故的核心成因。测试通过 `monkeypatch` 替换 `_on_thread_crash` 收集异常，避免 pytest 被 `os._exit` 杀掉。
3. **`/health` 调用链禁止触发 MT5 重连**：`src/clients/base.py` 的 `health()` 默认参数 `attempt_mt5_reconnect=False` → `inspect_session_state(attempt_initialize=False, attempt_login=False)`。`readmodels/runtime.py` 的 `inspect(...)` 同步改 `False`。重连职责归还给 `BackgroundIngestor` 的错误恢复循环（它本来就有）和 `src/ops/mt5_session_gate.py` 启动 preflight（这两处保留 `True`）。

**覆盖文件**：
- `src/persistence/db.py`：新增 3 个 repo @property（research/experiment 延迟 import 以避免循环依赖）
- `src/persistence/storage_writer.py`：`ensure_schema_ready()` 追加调用 3 个 repo `ensure_schema()`
- `src/persistence/repositories/__init__.py`：`BacktestRepository` re-export；research/experiment 故意不 re-export（循环依赖）
- `src/api/deps.py`：+3 getter（`get_research_repo / get_experiment_repo / get_backtest_repo`）
- `src/api/{research,experiment,backtest}_routes/*.py`：`_get_xxx_repo` / `get_backtest_repo` 改为转发到 deps，不再 `new TimescaleWriter`
- `src/indicators/runtime/event_loops.py`：新增 `_on_thread_crash` + 4 个主循环拆成 `run_*_loop` 外壳 + `_run_*_loop_body` 实现
- `src/clients/base.py`：`health()` 加 `attempt_mt5_reconnect=False` 默认
- `src/readmodels/runtime.py`：`inspect_session_state(...)` 改 False

**演进方向**：
- 如果未来 fail-fast 的 `os._exit(1)` 在 SIGTERM/graceful shutdown 里误触发（例如 `stop_event.set()` 后残余异常），应改成"先检查 `manager.state.stop_event.is_set()` → 如为 True 则 return（正常退出），否则 crash"。
- 关键线程 liveness check（ADR-004 的 `_any_thread_alive`）目前只在 stop 流程用；若 fail-fast 路径被证明不够（比如进程不退出只是线程死），可补 monitor 运行时 liveness check + 主动升级告警。

---

## ADR 模板

```markdown
## ADR-NNN: 标题

**状态**：已确定 / 已废弃（YYYY-MM-DD）

**上下文**：为什么需要做这个决策？之前遇到了什么问题？

**决策**：具体选择了什么方案，以及理由。

**演进方向**：什么场景下需要重新评估？届时的推荐方向是什么？
```
