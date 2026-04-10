# 代码库审查报告

> 审查日期：2026-04-10
> 范围：当前工作区全量源码、配置与主要文档。
> 结论定位：风险台账与后续整改入口，不代表已修复代码问题。

---

## 1. 总体结论

当前系统已经完成从 legacy 策略到结构化策略的主体迁移，领域目录、运行时装配、信号链路、交易执行、持久化、回测与研究系统都已形成清晰分层。主要问题不在“缺模块”，而在迁移后的工程一致性：

1. **本机 local 配置仍会覆盖到 legacy 策略语义**，导致运行态策略投票与当前结构化策略目录不一致。
2. **部分长期运行组件的 stop/start 生命周期保护不一致**，线程 join 超时后仍清空线程引用，存在重复启动后台线程的风险。
3. **装配层和 API/展示层仍访问若干私有属性/方法**，说明正式端口还不完整。
4. **千行级协调器仍然集中多种职责**，后续性能与并发问题会优先在这些文件中出现。
5. **策略有效性仍处在验证阶段**，当前样本量不足以支持实盘放大或过度调参。

---

## 2. P0 风险

### 2.1 `signal.local.ini` 中的 legacy 投票组正在影响当前运行语义

**证据**：
- 当前代码注册策略：`structured_trend_continuation`、`structured_trend_h4`、`structured_sweep_reversal`、`structured_breakout_follow`、`structured_range_reversion`、`structured_session_breakout`、`structured_trendline_touch`、`structured_lowbar_entry`。
- 当前工作区存在被 `.gitignore` 忽略但会生效的 `config/signal.local.ini`。
- 该 local 文件中的 4 个 voting group 仍指向 `supertrend`、`rsi_reversion`、`keltner_bb_squeeze`、`range_box_breakout` 等已移除 legacy 策略。
- 实测配置加载后：`unknown_group_members` 覆盖 15 个旧策略，`structured_in_groups = []`，`unknown_strategy_timeframes` 共有 37 个旧策略键。

**影响**：
- `policy.voting_groups` 非空会关闭全局 consensus engine。
- 旧投票组又没有任何已注册结构化策略成员，因此组内投票不会产生有效结构化共识信号。
- `HTFStateCache` 在多投票组模式下会把 source strategies 切到 group name；如果组信号为空，HTF 方向缓存可能拿不到预期来源。

**建议**：
- 清理本机 `config/signal.local.ini` 中所有 legacy 策略名、旧投票组和旧 `[regime_affinity.*]`。
- 在启动阶段增加配置校验：`strategy_sessions`、`strategy_timeframes`、`voting_groups`、`regime_affinity.*` 的策略名必须属于 `SignalModule.list_strategies()` 或明确标记为已禁用。
- 对 ignored local 配置提供只读诊断端点或 preflight 检查，避免“代码已清理但本机覆盖仍生效”。

### 2.2 生命周期 stop 语义不一致，join 超时后仍可能丢失线程引用

**涉及位置**：
- `src/signals/orchestration/runtime.py`：`stop()` 在 `join(timeout)` 后直接 `self._thread = None`。
- `src/trading/positions/manager.py`：`stop()` 在 `join(timeout=5.0)` 后直接清空 `_reconcile_thread`。
- `src/trading/pending/manager.py`：`shutdown()` 在 monitor/fill worker join 后直接清空线程引用。
- `src/indicators/manager.py`：`stop()` 会记录未退出线程 warning，但随后仍清空线程引用。

**影响**：
- 如果后台线程因 I/O、队列阻塞或外部 MT5 调用未及时退出，引用被清空后 `start()` 可能再创建新线程，形成双线程消费、重复 listener 或状态竞争。
- 这与 `TradeExecutor` 已经沉淀的“超时后保留线程引用，下一次 start 等待僵尸线程退出”的生命周期契约不一致。

**建议**：
- 抽取统一的 lifecycle helper：`join_and_clear_if_stopped(component_name, thread, timeout)`。
- join 后仅在线程已退出时清空引用；仍 alive 时保留引用并让 `is_running()` 返回真实状态。
- 将该约束加入 ADR，避免后续组件重复实现不一致逻辑。

### 2.3 装配层和展示层仍存在私有属性依赖

**证据示例**：
- `src/api/admin_routes/config.py` 通过 `getattr(indicator_mgr, "_get_intrabar_eligible_names", None)` 读取指标内部方法。
- `src/app_runtime/builder.py` 直接写入 `indicator_manager._pipeline_event_bus`、`indicator_manager._current_trace_id`、`signal_runtime._pipeline_event_bus`、`signal_runtime._warmup_ready_fn`。
- `src/app_runtime/factories/signals.py` 直接写入 `signal_runtime._intrabar_trade_coordinator`、`trade_executor._intrabar_guard`。
- `src/app_runtime/builder.py` 停止 paper trading 时访问 `bridge._session`。

**影响**：
- 私有字段变更会绕过类型检查和契约测试，尤其容易在热重载、运行模式切换和 Studio 展示路径中引入隐性回归。

**建议**：
- 为这些行为补正式端口：`IndicatorManager.intrabar_eligible_names()`、`set_pipeline_event_bus()`、`SignalRuntime.set_pipeline_event_bus()`、`set_warmup_ready_fn()`、`enable_intrabar_trading()`、`PaperTradingBridge.current_session()`。
- API/Studio/readmodel 只依赖公开只读方法，不再探测私有实现。

---

## 3. P1 架构与性能问题

### 3.1 协调器仍偏大，职责边界需要继续下沉

热点文件（按当前行数）：

| 文件 | 行数 | 主要风险 |
|------|-----:|----------|
| `src/indicators/manager.py` | 1424 | 4 个后台线程、事件写入、intrabar、reconcile、状态投影混在同一门面 |
| `src/trading/positions/manager.py` | 1279 | 持仓恢复、对账、出场执行、SL/TP 修改、状态投影集中 |
| `src/signals/orchestration/runtime.py` | 1076 | 队列、生命周期、投票、状态、status 投影仍在同一类 |
| `src/signals/service.py` | 992 | 策略注册、评估、持久化、查询与诊断入口混合 |
| `src/trading/execution/executor.py` | 983 | confirmed/intrabar 执行、过滤、熔断、参数计算、状态汇总集中 |
| `src/trading/pending/manager.py` | 981 | 价格监控、MT5 order 管理、fill worker、超时降级集中 |

**建议拆分顺序**：
1. 先拆生命周期/线程/队列 runner，不动领域算法。
2. 再拆只读投影 builder，避免 `status()` 继续读取内部散落字段。
3. 最后拆业务策略，如 PendingEntry 的超时降级、PositionManager 的 SL/TP 修改端口。

### 3.2 Intrabar 已具备链路，但还不是交易级 SLO 能力

**现状**：
- `IndicatorManager._intrabar_queue` 满时会丢事件。
- `SignalRuntime._intrabar_events` 在交易协调器存在时会短暂等待或替换旧事件；失败仍会丢弃。
- TODO 中已有 intrabar drop rate、queue age、degrade ladder、trace_id 贯通等待办。

**建议**：
- `intrabar_trading.enabled=true` 前必须先完成 SLO 与降级矩阵。
- 状态接口暴露 `intrabar_drop_rate_1m`、`intrabar_queue_age_p95`、`intrabar_to_decision_latency_p95`。
- 从“队列满随机丢弃/替换”升级为明确的 L0-L3 降级策略：全量 intrabar → 白名单策略 → 核心 TF → confirmed-only。

### 3.3 Walk-Forward 结果持久化仍不完整

**证据**：
- `execute_walk_forward()` 仅把 `wf_result` 存入 `backtest_runtime_store.store_walk_forward_result()`。
- 代码会把每个 split 的 OOS `BacktestResult` 单独 `persist_result()`，但没有 WF run/split 的整体 DB 实体与查询恢复路径。
- `BacktestRepository` 目前只有普通 run/trades/evaluations/recommendations 的 fetch/save 方法。

**影响**：
- API 重启后无法按一次 WF 任务完整还原 splits、IS/OOS 对照、best_params、overfitting_ratio、consistency_rate。

**建议**：
- 增加 `backtest_walk_forward_runs` 与 `backtest_walk_forward_splits`。
- Repository 增加 `save_walk_forward_result()`、`fetch_walk_forward_result()`、`list_walk_forward_runs()`。
- API 的 `/results` 和 `/results/{run_id}` 应区分普通 backtest、optimization、walk_forward。

---

## 4. 策略层问题

### 4.1 当前策略样本不足，不应作为实盘放大的依据

`TODO.md` 中 2026-04-08 基线显示：

| TF | 笔数 | 结果 |
|----|----:|------|
| M30 | 16 | WR 68.8%，PnL +271.20，MC p=0.057 |
| H1 | 3 | WR 100%，PnL +276.51 |
| 合计 | 19 | 3 个月约 1.5 笔/周 |

**判断**：
- M30 的 p=0.057 接近但未通过 0.05 显著性门槛。
- H1 只有 3 笔，不能支持任何稳健结论。
- 当前最重要的策略任务不是继续堆功能，而是扩大样本、做 WF、跑 Paper Trading 对比。

### 4.2 local 配置可能冻结或改变结构化策略，但文档没有提示

`config/signal.local.ini` 中对 `structured_session_breakout`、`structured_lowbar_entry` 有全 0 affinity 覆盖，实际等同冻结。这可能是有意实验结果，但目前没有运行前摘要把“哪些策略被 local 覆盖冻结”暴露出来。

**建议**：
- 增加策略运行态摘要：`enabled_timeframes`、`effective_affinity`、`is_frozen_by_affinity`、`in_voting_group`。
- Paper Trading/Backtest 输出中记录 active strategy set 的配置快照。

---

## 5. 代码质量问题

### 5.1 broad `except Exception` 分布广，需要区分“可降级”和“必须失败”

代码中大量 `except Exception` 是长期运行系统的防御手段，但部分路径会静默吞掉配置或展示错误，例如 API config 视图、builder 可选组件、回测 DB fallback。建议按语义分层：

- 启动必需组件：失败应阻断启动或进入明确 degraded mode。
- 可选观测组件：可以不中断，但必须在 health/startup status 中可见。
- API 展示补充字段：可以缺省，但应记录 debug/metric，不应完全 `pass`。

### 5.2 文档与代码仍有迁移后残留

本次审查发现 README 曾描述“35 个内置策略”“composites.json”“Regime 亲和度直接跳过”等旧语义；实际代码已经切到结构化策略目录和新的评分/风控语义。本轮已同步 README，后续若策略注册或评分链路变化，应继续同步 README、`docs/signal-system.md` 与本风险台账。

---

## 6. 后续整改顺序

1. **先清配置**：清理 local legacy 策略覆盖，并增加启动校验。
2. **再补生命周期契约**：统一 stop/start helper，修复 join 超时清引用问题。
3. **再补公开端口**：替换 API/装配层私有属性访问。
4. **再做 WF 持久化**：把验证结果从内存缓存提升到 DB 事实源。
5. **最后优化 intrabar**：只有 SLO、降级与 trace 完成后，才把 intrabar 作为真实交易入口。

---

## 7. 验证记录

本次为审查与文档更新，未修改业务代码。已执行：

- `git ls-files`：盘点仓库文件。
- PowerShell `Select-String -Encoding utf8` / `Get-Content -Encoding utf8`：审查关键源码与文档。
- Python 只读配置检查：确认当前注册策略、active voting groups、unknown strategy keys。

未执行完整测试套件；原因是本次只落文档与风险台账，未触发生产代码变更。
