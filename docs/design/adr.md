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

## ADR 模板

```markdown
## ADR-NNN: 标题

**状态**：已确定 / 已废弃（YYYY-MM-DD）

**上下文**：为什么需要做这个决策？之前遇到了什么问题？

**决策**：具体选择了什么方案，以及理由。

**演进方向**：什么场景下需要重新评估？届时的推荐方向是什么？
```
