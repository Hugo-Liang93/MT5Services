# 代码库审查报告

> 首次审查日期：2026-04-10
> 最近更新：2026-04-12
> 范围：当前工作区全量源码、配置与主要文档。
> 结论定位：风险台账与后续整改入口，不代表已修复代码问题。

---

## 0. 2026-04-11 ~ 2026-04-12 修复更新

本轮已直接处理并验证以下启动阻塞项：

1. **指标 durable event 消费断链已修复**  
   `IndicatorEventLoop` 已改为调用 `event_store.claim_next_events(...)`，真实启动时事件循环线程可正常存活，不再因接口名不匹配崩溃。

2. **`runtime_task_status` 持久化契约已统一并补库表迁移**  
   新增统一状态集合，覆盖 `ready / failed / idle / disabled / ok / partial / error / stopped` 等当前真实生产者语义；`TimescaleWriter.init_schema()` 现在会在启动时重建相关 check constraint，现有数据库不需要手工删表。

3. **经济日历 `session_bucket/status` 与 schema 漂移已修复并补库表迁移**  
   现已允许 `all_day / asia_europe_overlap / europe_us_overlap`，同时允许 `imminent / pending_release` 状态；真实启动后不再出现该表 check constraint 失败和因此进入 DLQ 的问题。

4. **`StorageWriter.stop()` 生命周期语义已修正**  
   线程 `join(timeout)` 后若仍存活，将保留线程引用并保持 `is_running()` 为真，避免误判组件已停止。

5. **`/health` 已扩展为链路级组件视图**  
   除市场与交易摘要外，现在还会返回 `storage_writer / indicator_engine / signal_runtime / trade_executor / pending_entry_manager / position_manager / economic_calendar` 运行状态，便于直接判断“采集 → 指标 → 信号 → 执行”链路是否在线。

6. **日志目录已统一回到项目根目录 `data/`**  
   `src.entrypoint.web` 的相对 `log_dir` 解析现已锚定仓库根目录，不再向 `src/entrypoint/data/logs` 写入；本轮也已把历史遗留日志迁移到根目录 `data/logs/` 下的 `legacy-src-entrypoint-*.log`。

7. **系统 readiness 盲区已补齐：采集线程现在纳入就绪探针**  
   `/monitoring/health/ready` 过去只看 `storage_writer` 与 `indicator_engine`，即使 `BackgroundIngestor` 已退出也可能误报 `ready`。本轮已把 `ingestion` 纳入同一探针，`RuntimeReadModel.build_storage_summary()` 也同步把 `ingest_alive=false` 视为 `critical`。

8. **`PipelineTraceRecorder` 生命周期语义已修正**  
   该组件此前在 `stop()` 的 `join(timeout)` 后会无条件清空线程引用，且监听器注册失败时仍会假装启动成功。本轮已改为：监听器挂接失败立即抛错；线程未退出时保留引用并维持真实运行态，避免 observability 组件假死但状态看起来正常。

9. **`calendar` 包顶层导入导致的循环依赖已修复**  
   `src.persistence.schema.economic_calendar` 引入 contract 时会先执行 `src.calendar.__init__`，此前这里立即导入 `service`，从而形成 `db -> schema -> calendar -> service -> db` 的循环。现已改为懒加载导出，不再阻塞采集/持久化相关测试与运行时装配。

10. **测试资产已做一轮去噪和契约收口**  
   已删除 7 个只有 `pytest.mark.skip`、实际不产生任何覆盖的历史空文件；同时移除了 2 个“打印 + 吞异常”的脚本式伪测试。`tests/data/test_data_layer.py` 也已改造成无外部 DB 依赖的真实单元测试，`tests/api/test_monitoring_config_reload.py` 则同步对齐当前 404 契约，避免旧口径误报失败。

11. **`SignalRuntime` 测试已从私有子方法绑定收口到行为契约**  
   原 `tests/signals/test_runtime_submethods.py` 直接断言 `_dequeue_event / _is_stale_intrabar / _apply_filter_chain / _detect_regime` 等私有实现细节，重构成本高而行为保护有限。本轮已删除该文件，并把仍有价值的覆盖迁移为 `tests/signals/test_signal_runtime.py` 中的行为测试，直接校验队列反饥饿、stale intrabar 丢弃、filter 统计和 stop 后队列清理。

12. **`docs/` 已完成一次“运行时真相 / 审计治理 / 方案规划”分层审计，并新增单一导航入口**  
   已新增 `docs/README.md` 作为文档入口，`architecture / full-runtime-dataflow / signals-dataflow-overview / intrabar-data-flow / entrypoint-map` 已明确标注为当前实现真相文档；`signal-system / research-system / next-plan / r-based-position-management` 等文档已补充“设计参考/规划”定位说明，避免再把方案稿直接当作当前运行结论。`architecture.md` 也已把本应属于专题设计的参数表和方案细节收口到对应专题文档。与此同时，`docs/` 下运行时流图已统一为 Claude 风格 ASCII，不再保留 Mermaid 图。

13. **系统启动巡检与 live canary 已收口成独立 runbook**  
   已新增 `docs/runbooks/system-startup-and-live-canary.md`，把 `live_preflight`、启动后 5 分钟巡检、休盘/开盘日志判读、以及开盘窗口的系统级 live canary 步骤统一成一套固定流程。`entrypoint-map.md` 与 `docs/README.md` 已同步改为链接到该 runbook，避免启动入口文档和运行时文档再各自维护一套巡检说明。

本轮验证结果：

- `pytest` 相关回归测试已通过，新增了事件循环接口、schema 初始化迁移、StorageWriter 生命周期、日志路径、经济日历时间上下文等测试。
- 测试资产清理后，全量测试树已移除 skip-only 僵尸文件、脚本式伪测试，以及一组过度绑定 `SignalRuntime` 私有子方法的低价值测试，`passed` 数字更接近真实覆盖。
- 真实运行时启动已验证 `startup_ready=True`，指标事件循环、信号运行时、经济日历后台线程和存储写线程均能启动。
- 系统链路专项回归已通过 `56` 项，覆盖 `采集 -> 缓存 -> closed-bar 事件 -> durable event store -> readiness/health/readmodel -> trace recorder`。
- `docs/` 已完成一次分层审计，核心文档已明确“当前实现真相”与“规划/历史方案”的边界。
- 已新增系统启动与 live canary runbook，文档入口、启动入口和巡检流程现在有统一落点。
- 真实启动后的系统探针已验证返回：
  - `storage_writer=ok`
  - `ingestion=ok`
  - `indicator_engine=ok`
- 当前休盘场景下，系统线程存活与探针状态正常；仍存在 `market_data data latency critical` 告警，这属于休盘/无新鲜行情环境下的预期观测，不应与链路断裂混淆。

仍需单独关注但不属于本轮代码阻塞项：

1. **外部经济数据源仍可能超时**  
   真实启动时 Jin10 请求出现过 `read operation timed out`，这是外部依赖可用性问题，不是当前 schema/线程契约问题。

2. **市场数据延迟告警需要结合交易时段判断**  
   当前环境在监控中仍会报 `data latency critical`，更像是运行时没有收到足够新鲜行情或目标市场不活跃，需要结合 MT5 连接状态和交易时段继续看，不建议把它和本轮启动故障混为一类。

## 1. 总体结论

当前系统已经完成从 legacy 策略到结构化策略的主体迁移，领域目录、运行时装配、信号链路、交易执行、持久化、回测与研究系统都已形成清晰分层。主要问题不在“缺模块”，而在迁移后的工程一致性：

1. **本机 local 配置会优先生效，当前已不再保留 legacy 投票组，但仍会改变结构化策略有效集合**。
2. **部分长期运行组件的 stop/start 生命周期保护不一致**，线程 join 超时后仍清空线程引用，存在重复启动后台线程的风险。
3. **装配层和 API/展示层仍访问若干私有属性/方法**，说明正式端口还不完整。
4. **千行级协调器仍然集中多种职责**，后续性能与并发问题会优先在这些文件中出现。
5. **策略有效性仍处在验证阶段**，当前样本量不足以支持实盘放大或过度调参。

---

## 2. P0 风险

### 2.1 `signal.local.ini` 仍是高优先级事实源，当前会冻结部分结构化策略

**证据**：
- 当前代码注册策略：`structured_trend_continuation`、`structured_trend_h4`、`structured_sweep_reversal`、`structured_breakout_follow`、`structured_range_reversion`、`structured_session_breakout`、`structured_trendline_touch`、`structured_lowbar_entry`。
- 当前工作区存在被 `.gitignore` 忽略但会生效的 `config/signal.local.ini`。
- 当前 local 文件已显式注明 `[voting_groups]` 清空，运行时 `cfg.voting_group_configs == []`。
- 同时，`[regime_affinity.structured_session_breakout]` 与 `[regime_affinity.structured_lowbar_entry]` 被全部覆盖为 `0.0`，实际等同冻结。

**影响**：
- local 覆盖不会出现在仓库默认配置中，但会直接改变本机运行时的有效策略集合。
- 当前缺少“启动摘要/状态端点”来明确标出哪些策略被 local 配置冻结，人工排查时容易把“无交易”误判成策略逻辑问题。

**建议**：
- 在启动阶段输出 effective strategy summary：启用 TF、effective affinity、是否被冻结、是否支持 intrabar。
- 对 ignored local 配置提供只读诊断端点或 preflight 检查，避免“本机覆盖改变行为但 API 不可见”。
- 若冻结是长期决策，应把结果同步进运行文档，而不是只留在本机 local 文件里。

### 2.2 生命周期防护已有改进，但仍是分散实现，缺统一约束

**涉及位置**：
- `src/signals/orchestration/runtime.py`：`stop()` 在 `join(timeout)` 后直接 `self._thread = None`。
- `src/trading/positions/manager.py`：`stop()` 在 `join(timeout=5.0)` 后直接清空 `_reconcile_thread`。
- `src/trading/pending/manager.py`：`shutdown()` 在 monitor/fill worker join 后直接清空线程引用。
- `src/indicators/manager.py`：`stop()` 会记录未退出线程 warning，但随后仍清空线程引用。

**现状判断**：
- 当前这些组件大多已经改成“线程仍存活则保留引用并在下次 start() 再次等待”，明显优于早期实现。
- 问题不再是单个显式 bug，而是这套策略仍分散在多个类里手写，缺统一 helper、统一测试模板和统一状态契约。

**建议**：
- 抽取统一的 lifecycle helper：`join_and_clear_if_stopped(component_name, thread, timeout)`。
- join 后仅在线程已退出时清空引用；仍 alive 时保留引用并让 `is_running()` 返回真实状态。
- 将该约束加入 ADR，避免后续组件重复实现不一致逻辑。

### 2.3 私有属性依赖已收敛，但还没有完全消失

**证据示例**：
- `src/api/admin_routes/config.py` 已改为直接调用 `UnifiedIndicatorManager.get_intrabar_eligible_names()`，并在 `bar_event_handler.py` 同步移除了对私有 `_get_intrabar_eligible_names` 的兼容回退逻辑。
- 运行时装配层已把部分私有写入替换为正式 setter，方向正确，边界收口正在持续推进。
- `src/indicators/query_services/state_view.py` 已去掉 `Legacy` 回退桥接，`query_services/runtime.py` 与 `runtime/bar_event_handler.py` 统一通过 `manager.state` 获取 `pipeline_event_bus` 与状态数据；测试点同步更新为状态容器契约验证，未再依赖 `_xxx` 字段。

**影响**：
- 私有字段变更会绕过类型检查和契约测试，尤其容易在热重载、运行模式切换和 Studio 展示路径中引入隐性回归。

**建议**：
- 为这些行为补正式端口：`IndicatorManager.get_intrabar_eligible_names()`、`set_pipeline_event_bus()`、`SignalRuntime.set_pipeline_event_bus()`、`set_warmup_ready_fn()`、`enable_intrabar_trading()`、`PaperTradingBridge.current_session()`。
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
| `src/trading/pending/manager.py` | 620~680 | 价格监控与 MT5 order 管理已下沉，status 聚合已下沉到 `PendingEntrySnapshotService`，`PendingEntryManager` 更偏门面职责 |

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

### 4.2 local 配置会冻结结构化策略，但运行时可见性不足

`config/signal.local.ini` 中对 `structured_session_breakout`、`structured_lowbar_entry` 有全 `0.0` affinity 覆盖，实际等同冻结。这可能是有意实验结果，但目前没有运行前摘要把“哪些策略被 local 覆盖冻结”暴露出来。

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
3. **已完成：公开端口收口（基本完成）**：指标链路已移除 `_get_intrabar_eligible_names` 的外部访问，统一走 `get_intrabar_eligible_names()`；其余接口持续补齐。
4. **阶段 A 进行中（2026-04-10）**：`SignalRuntime` 已将队列/状态/状态机职责切到 `runtime_components.py`，并修复队列清理与 `staticmethod` 越权引用问题。
5. **阶段 A 基本完成（2026-04-10）**：`TradeExecutor` 已补齐 `PreTradePipeline / ExecutionDecisionEngine / ExecutionResultRecorder` 三类组件，并完成队列收口 + 溢出记录统一；`PreTradePipeline` 已下沉 intrabar 门禁分支，`ExecutionDecisionEngine` 接管执行动作分发（市价/挂单）。当前阶段目标已转为“结果可观测性与能力契约”。
5. **阶段 A 增量（2026-04-11，IndicatorManager 最后一轮收口）**：
  - `src/indicators/query_services/runtime.py`：计算/事件/回写/重算链路；
  - `src/indicators/query_services/read.py`：查询/快照/监听器/观测链路；
  - `src/indicators/query_services/storage.py`：快照标准化与内存结果序列化缓存；
  - `src/indicators/runtime/registry_runtime.py`：注册加载与重初始化；
  - `src/indicators/runtime/registry_mutation.py`：配置变更（add/update/remove）入口；
  - `src/indicators/runtime/bar_event_handler.py`：批次处理与单事件处理的事件编排；
  - `src/indicators/runtime/event_loops.py`：事件循环与重算调度；
  - `src/indicators/runtime/event_io.py`：bar close 入库入队与 snapshot 落盘触发；
  - `src/indicators/runtime/lifecycle.py`：启动/停止/运行状态与 listener 生命周期；
  - `src/indicators/runtime/intrabar_queue.py`：intrabar 入队与溢出降级策略；
  - `src/indicators/runtime/pipeline_runner.py`：pipeline 计算分层与优先级结果合并；
  - `src/indicators/manager.py` 保持门面职责，不再承担细粒度查询、注册与循环入口实现；
  - `src/indicators/manager_bindings.py`：统一方法绑定映射；
  - `src/indicators/runtime/loop_adapter.py` 已移除，事件循环与循环入口直接通过 `lifecycle/event_loops` 的组合边界进入。
  - 兼容分支清理：`src/indicators/query_services/` 与 `src/indicators/runtime/` 的 observer/event-store/snapshot 发布路径统一走正式端口（明确契约，不做兼容分支兜底）。
6. **阶段 B 收敛（2026-04-10）**：`SignalModule` 已有能力索引（`strategy_capability_catalog`）并用于回测引擎 confirmed 能力门控；`SignalRuntime` 已同步通过 `SignalPolicy` 注入能力快照，消费字段改为 `valid_scopes`、`needed_indicators`、`needs_intrabar`、`needs_htf`。  
  - 已完成增量（2026-04-10）：  
    - `SignalPolicy` 增加 `strategy_capability_contract()` 作为运行时对账口。  
    - `runtime_warmup.py`/`runtime_status.py` 已改为能力口读取，避免分散兼容回退。  
    - 回测 `BacktestEngine` 已改为按 `strategy_capability_catalog(voting_group_policy=...)` 初始化能力快照，策略能力契约与 voting 组配置对齐。  
    - 回测策略评估加一层 `capability.valid_scopes` 门禁，确认与 `SignalRuntime` 的 scope 消费语义一致。  
  - 下一步：把能力快照治理点进一步上移为统一回放一致性检查口（voting/intrabar/丢弃率），并通过 admin 入口 `GET /admin/strategies/capability-reconciliation` 与 `GET /admin/strategies/capability-contract` 固化风险红线。  
  - 当前状态：能力对账已对齐 `voting_group_policy / regime_affinity / htf_requirements`，并在 runtime 快照与 readmodel 两端都可见。  
  - 已完成增量（2026-04-11）：
    - runtime/readmodel 新增 `strategy_capability_execution_plan`，明确 `configured/scheduled/filtered` 调度边界、scope 覆盖与指标需求并集。
    - 回测 `BacktestResult` 新增同语义 `strategy_capability_execution_plan`，用于与实盘 status 直接对账。
    - 新增 `GET /admin/strategies/capability-execution-plan`，对齐 module/runtime/backtest 三方执行计划差异。
  - 已完成增量（2026-04-11，端口收敛）：
    - `SignalRuntime` 已移除 `_legacy_strategy_capability_contract`，能力加载仅消费 `SignalModule.strategy_capability_catalog(voting_group_policy=...)`。
    - `BacktestEngine` 已移除 `_legacy_strategy_capability`，能力加载仅消费 `SignalModule.strategy_capability_catalog(voting_group_policy=...)` 并对目标策略做缺失即失败校验。
    - `runtime_status.py` 与 `service_diagnostics.py` 已移除能力契约 `hasattr/getattr` 兼容探测，统一依赖 `strategy_capability_contract()` 正式端口。
    - `runtime_status.py` 与 `backtesting/engine/runner.py` 的 execution plan 已收敛到共享构建器 `src/signals/contracts/execution_plan.py`，`scope/needs/required_indicators` 语义同源。
    - `service_diagnostics.py` 的 module/runtime 对账规范化已收敛到 `normalize_capability_contract(...)`，避免对账口与执行计划口出现字段形态分叉。
    - `api/admin_routes/strategies.py` 的 `module_plan` 已复用共享构建器；`module/runtime/backtest` 对账统一读取 `scheduled_strategies` 语义。
    - 两侧均改为“能力契约非法项/缺失策略直接抛错”，不再通过兼容分支静默降级。
  - 建议验收：新增策略只需通过能力声明/配置驱动，不新增 runtime/private 分散推断。
6. **再做 WF 持久化**：把验证结果从内存缓存提升到 DB 事实源。
7. **最后优化 intrabar**：只有 SLO、降级与 trace 完成后，才把 intrabar 作为真实交易入口。

### 6.A Indicators 目录职责边界巡检清单（2026-04-11）

- [x] `src/indicators/manager.py` 保持门面职责，不承接细粒度计算/查询实现。
- [x] 查询与计算入口集中在 `src/indicators/query_services/`，按职责分离为 runtime 计算/查询与查询服务。
- [x] 运行时细粒度职责集中在 `src/indicators/runtime/`，并按环节拆分：
  - 注册与重初始化：`registry_runtime.py`、`registry_mutation.py`、`lifecycle.py`
  - 事件编排：`bar_event_handler.py`
  - 事件循环：`event_loops.py`、`intrabar_queue.py`
- [x] `src/indicators/runtime/event_io.py` 统一处理 bar close 入队与批量落库流程。
- [x] `src/indicators/runtime/pipeline_runner.py` 统一承载 pipeline 计算分层，不由 `manager.py` 或 `query_services.runtime` 直接承担入口。
- [x] `src/indicators/runtime/loop_adapter.py` 已移除，不保留重复循环适配。
- [x] `manager_bindings.py` 维持显式端口映射，调用走统一绑定方法，避免私有属性直接穿透。
- [x] 已拆除跨模块的双向循环导入：  
  - `query_services.runtime` 与 `runtime.bar_event_handler` 改为懒加载边界；  
  - `runtime.pipeline_runner` 不再顶层反向 import query services。
- [x] 已补齐冒烟测试：`tests/indicators/test_core_functions.py`、`tests/indicators/test_flush_event_batch.py`、`tests/indicators/test_manager_intrabar.py`。
- [ ] 建议后续：补 `tests/indicators/` 层面的职责契约测试（`manager` 与 `runtime/query_services` 的输入输出不变量）。

---

### 6.B Indicators 输入输出与状态归属图（2026-04-11）

#### 文字化边界图（Claude 风格）

```
上游输入
  ├─ api/admin_routes / orchestration caller
  ├─ market_service
  ├─ event_store
  └─ storage_writer
        │
        ▼
UnifiedIndicatorManager（门面）
  ├─ state（唯一运行态，单一所有权）
  ├─ QueryBindingMixin（query_services 端口）
  └─ RegistryBindingMixin（registry 端口）
        │
        ├─ query_services/
        │   ├─ runtime.py
        │   │   ├─ compute / reconcile / eligibility / write-back
        │   │   └─ 对外绑定到 manager 公开方法
        │   ├─ storage.py
        │   │   └─ 快照标准化、持久化输入结构构造
        │   └─ read.py
        │       └─ 读模型 / listeners / 快照读取
        │
        └─ runtime/
            ├─ lifecycle.py：启动/停止/状态机 + listener 生命周期
            ├─ event_loops.py：closed-bar / intrabar / event_writer 的循环
            ├─ event_io.py：enqueue 与批量落盘
            ├─ bar_event_handler.py：事件批次与单事件编排
            ├─ pipeline_runner.py：pipeline 计算分层与优先组合并
            ├─ registry_runtime.py：注册加载与重初始化
            ├─ registry_mutation.py：add/update/remove 配置入口
            └─ intrabar_queue.py：intrabar 入队与回压策略
        │
        └─ market_service cache（指标回写与消费方）
```

边界规则：
- 输入拥有方：`market_data` / `config` / `event_store` / `storage_writer` 仅由外部注入；`runtime` 不重复定义同类输入源。
- 状态所有权：`manager.state` 为单点写入点；`runtime` 与 `query_services` 仅通过方法参数或返回值读取与更新。
  - 调用方向：上游只调用 `UnifiedIndicatorManager`，内部按端口走 `QueryBindingMixin / RegistryBindingMixin`，不直接访问私有字段。
  - 持久化与可观测：事件入库、snapshot 落盘、重算触发通过 `runtime.event_io` + `runtime.event_loops` 的统一链路执行。

### 6.C Trading 输入输出与状态归属图（2026-04-11）

- `position state` 全量写入点是 `PositionManager._positions` + `_tracking`（内存运行态），其职责边界不跨越到 execution/策略评估模块。
- `execution 触发` 由 `TradeExecutor` 与 `TradeExecution*` 组件串联完成，不直接读取 `PositionManager` 的运行态状态；仅消费 `SignalRuntime` 已确认事件。
- `sl/tp 出场` 的唯一计算口是 `src/trading/positions/exit_rules.py`，`PositionManager` 只负责编排评估（`_evaluate_chandelier_exit`）与 MT5 API 执行（`_apply_chandelier_action`），不承载策略语义。
- `pending 入场` 的唯一协调口是 `src/trading/pending/manager.py` + `monitoring.py`，执行入口保持单向入链：`PendingEntryManager -> execute_fn`。
- `trade outcome` 的统一追踪口是 `TradeOutcomeTracker`，下游仅通过 `TradeOutcomeTracker` 回写/消费，不在 `positions` 或 `execution` 中各自重复落库。

边界规则：
- 输入拥有方：
  - 信号事件：`SignalRuntime`（已确认的 signal 事件）
  - 市场快照：`indicator_manager` / `market_service`
  - MT5 交互：`trade_module`（交易网关端口）
  - 共享配置：`SignalConfig`（只读注入）
- 状态写入方：
  - 持仓生命周期：`PositionManager`（reconcile、入场追踪、出场状态）
  - 执行队列：`TradeExecutor` 与 execution 门禁组件
  - 挂单等待：`PendingEntryManager`
- 调用方向（禁止反向）：
  - API / orchestration 不应直接读/改 `_positions`、`_reconcile_thread`、`_fill_monitor_thread` 等私有字段。
  - execution / strategy 模块不应跨过 `exit_rules` 复用 Chandelier 规则细节。
- 可观测性闭环：
  - `PositionManager._build_status`/`SignalRuntime/status` 与 `execution` 需要输出一致的 `decision_id + position_ticket + reason`，用于链路回放定位“是哪个策略/何时被哪个规则阻断或触发”。

### 6.D Trading 冒烟清单（2026-04-11）

- [x] Chandelier profile 命名与注入路径收口为默认语义（`fallback` -> `default`）：
  - `src/config/models/signal.py`
  - `src/app_runtime/factories/signals.py`
  - `src/backtesting/engine/runner.py`
  - `src/trading/positions/exit_rules.py`
  - `config/signal.ini`
- [x] 回测/实盘共用同一 `ChandelierConfig` 字段集合，减少分叉映射语义。
- [ ] 后续建议（不在本轮实现中）：
  - 运行时巡检：打印/上报 `ChandelierConfig` 的 effective profile 解析结果（`regime_aware`、`default_profile`、`aggression_overrides`、`tf_trail_scale`）供 `startup check` 对账。
  - 完整回归：在交易冒烟时补齐 `position_manager` stop/start 与信号出场路径的 trace 断言。

### 6.E 风险模块输入/输出与状态归属清单（2026-04-11）

- `src/risk/service.py`
  - `PreTradeRiskService` 是对外服务口，单一承担“下单前风控评估”语义。
  - 风险规则(`rules.py`)与领域数据模型(`models.py`)不直接持有交易器实例；仅通过 `RuleContext` 读取 `account_service` 与 `economic_provider` 提供的标准化能力。
  - 风险判定只输出 `RiskAssessment`/`checks` 结构，业务方以 `verdict`、`reason`、`blocked`、`checks` 进行决策。
- `src/risk/runtime.py`
  - `wire_margin_guard` 仅承担 margin_guard 的生命周期挂接，不参与规则判定。
  - MarginGuard 实例由 `PositionManager` 与 `TradeExecutor` 消费，遵循“服务注入而非反向读取内部态”的单向边界。
- `src/risk/margin_guard.py`
  - `MarginGuard` 负责“数据→阈值→动作决议”纯计算 + 动作派发，状态归属于 `last_snapshot` 与 action counters。
  - `load_margin_guard_config` 只做 `risk.ini`（含 local 叠加）字段到 dataclass 的映射。
- `src/trading/execution/pre_trade_checks.py`
  - 风控拦截位于执行前门禁列表（步骤 ⑥），“是否 block_new_trades”通过 `MarginGuard.should_block_new_trades()` 显示注入。
- 状态边界与来源：
  - 运行态输入源：
    - 账户状态：`account_service`（来自 MT5 会话）
    - 经济事件：`economic_calendar_service`
    - 风控策略：`RiskConfig` / `EconomicConfig`
  - `position_manager` 仅写入 `PositionManager._margin_guard` 引用；不反向注入规则内部状态。
  - `trade_executor` 仅读 margin guard 快照与决策结果，不直接改写 margin guard 行为状态。

- 冒烟清单（本轮）
- [x] `risk.runtime` 不再直接裸读 `config/risk.ini`，改为 `get_merged_config("risk")` + `load_margin_guard_config`，保证 `risk.local.ini` 覆盖链路。
- [x] `wire_margin_guard` 在 `risk` 配置缺失 `margin_guard` 段时回退到安全默认；当配置源类型异常时抛出结构化配置错误并中断启动，避免 silent fail。
- [x] 风险规则链路保持单向依赖：服务入口 -> rules -> models；execution 侧只消费 `PreTradeRiskService`/`MarginGuard` 接口，不探测私有字段。
- [x] 新增 `MarginGuard` 启动快照日志，`risk.runtime` 统一打印生效的 `margin_guard` 阈值与动作参数，用于 startup 可观测。
- [x] 风险码映射收口：`resolve_risk_failure_key` 改为优先返回 `verdict=block` 的检查项，避免 warning 与 block 混在一起时错误码退化。

### 6.F app_runtime 输入/输出与状态归属清单（2026-04-11）

- `src/app_runtime/container.py`
  - 职责：运行时组件的纯数据承载，所有字段默认 `None`，不承载生命周期动作与域逻辑。
  - 输入来源：`builder.py` 写入；其他层只读。
  - 状态所有权：组件对象内部状态仅归组件自己管理。
- `src/app_runtime/builder.py`
  - 职责：按依赖顺序构建 runtime 所有组件，并完成跨组件连接。
  - 边界：不直接执行业务规则计算，不做回退式兼容分支；装配流程按阶段拆分到 `src/app_runtime/builder_phases/`，保留监控/热更新/可观测连接在装配侧集中处理。
  - 阶段化拆分（本轮完成）：`market.py`、`trading.py`、`signal.py`、`paper_trading.py`、`runtime_controls.py`、`monitoring.py`、`read_models.py`、`studio.py`。
- `src/app_runtime/runtime.py`
  - 职责：启动、停止、错误回退、状态快照的集中编排；不应承担领域参数变换。
  - 状态所有权：`_status` 与停止回调队列由 runtime 本体持有。
- `src/app_runtime/lifecycle.py`
  - 职责：`RuntimeComponentRegistry` 的 `start/stop/is_running` 统一执行模型，提供模式变更顺序化行为。
  - 风险边界：`apply_mode` 对每个组件继续尝试启动，但仍存在部分失败后模式更新为目标状态的行为，依赖上层观测发现。
- `src/app_runtime/mode_controller.py`
  - 职责：运行模式状态机 + 守护线程；不直接操作交易模块运行态，只通过 `TransitionGuard` 和组件注册表做决策。
  - 本轮修复：`stop()` 在 `join(timeout)` 后仅在线程退出时清空引用，线程未退出时保留引用便于被动观测。
- `src/app_runtime/mode_policy.py`
  - 职责：模式策略（策略常量、守卫、EOD 动作）配置化，禁止在控制器中硬编码模式语义。
- `src/app_runtime/factories/*`
  - 职责：对象构造（市场/存储/指标/信号/交易）分离，不应再嵌入 runtime 生命周期判断。

#### 文字化边界图（Claude 风格）

```
上游配置 / 服务
  ├─ config（get_merged_config/专有 model）
  ├─ api/admin（启动入口）
  ├─ monitoring（健康组件）
  └─ persistence（数据库写入口）
        │
        ▼
builder（装配）
  ├─ factories 逐层构造对象
  ├─ runtime/read-models 及可观测链接
  └─ AppContainer（组件图）
        │
        ▼
runtime（编排）
  ├─ 生命周期：start/stop
  ├─ 模式控制：mode_controller + mode_policy + lifecycle registry
  └─ 运行时状态快照 + shutdown callback
        │
        ▼
domain 组件
  ├─ indicators / signals / trading / storage / monitoring
  └─ 各组件只对外提供正式端口
```

边界规则：
- 数据源/配置源不在 domain 内重复定义，统一向内注入。
- 生命周期动作只通过 `RuntimeComponentRegistry` + `RuntimeModeController` 触发；`AppRuntime` 仅做系统级编排与全局清理。
- 运行态状态快照来源保持单向：`AppRuntime` 暴露运行状态，组件内部状态保持私有与封闭。
- 禁止隐式回退补丁：高优先复用失败应向 `status` 和日志暴露，避免通过条件分支静默吞掉模式/组件失败。

#### 冒烟清单（本轮）
- [x] 修复 `RuntimeModeController.stop()` 的线程引用回收：线程未退出时不清空 `_monitor_thread`，避免 “is_running 反映不实”。
- [x] 增补测试：`RuntimeModeController.stop()` 在线程未退出时保留引用（已补充），并覆盖线程退出后清理引用的闭环。
- [x] `AppRuntime.start()` 与 `RuntimeComponentRegistry` 的启动入口已收口：storage/性能预热不再由 runtime 直接触发，统一由 registry 在模式切换链路内执行（保留幂等与一次性 warmup 标志）。

### 6.G 其余包巡检（2026-04-11）

按“职责边界/依赖方向/异常边界/历史兼容”四项复核 `src` 下除 `trading`、`risk` 外的其余包：

- `backtesting`：  
  - 结果：分层还算清晰（data / engine / filtering / optimization / paper_trading / runtime 数据对象），但 `src/backtesting/cli.py` 曾存在入口函数断裂问题（未定义引用、缺少子命令解析与持久化分支）。已完成修复：补齐 `_parse_param`、`_build_components`、`_persist_result`、`_build_parser`、`main()` 分发逻辑，当前 `python -m src.backtesting --help` 可正常给出可执行入口。  
  - 剩余建议：把 `--output`、`--param`、`--timeframes` 等通用参数抽到统一构建器，避免命令与默认行为语义再次发散（当前默认运行时为 `run`）。

- `clients`：  
  - 结果：基本职责清晰，按 MT5 外部系统封装，内部大量 `getattr`/`hasattr` 主要用于跨平台常量兼容与容错，属边界内“适配器策略”而非跨域探测（`clients/base.py` 与 `clients/mt5_trading.py` 明确通过 `_normalize_market_time`、`_get_field` 保证兼容）。  
  - 建议：将常量探测统一到 `clients/base.py` 的常量适配层，逐步在上层策略中改为显式能力字段，减少重复 `getattr` 分散。

- `api`：  
  - 结果：依赖链路完整，负责输入校验与序列化是合理的；监控路由异常防御已收敛为统一入口（`_execute_monitored_call` / `_execute_health_call`），避免散落 `try/except`。  
  - 建议：继续沿用“必须失败/可降级”分层，持续将通用边界策略下沉到业务路由层。

- `ingestion`：  
  - 结果：与 `market`/`persistence` 职责耦合度低，职责边界清晰，`BackgroundIngestor` 与存储写入方向单向。  
  - 建议：保持现状。

- `calendar` / `market_structure`：  
  - 结果：各自以领域能力边界存在，`calendar` 主要负责事件同步与风控告警前置、`market_structure` 负责形态计算，边界清晰。  
  - 建议：保持现状，补充一次策略级契约测试（事件窗口与交易过滤字段映射）即可。

- `monitoring`：  
  - 结果：职责清晰，仍有一定 `except Exception` 保护，建议区分“指标采集可降级”与“生命周期关键指标不可用”。  
  - 建议：为健康检查和关键事件总线增加错误码分级，避免所有异常被同一告警语义吞没。

- `ops`：  
  - 结果：`cli` 与 `scripts` 同名脚本并行分工，历史上形成重复入口。  
  - 执行结论：`src/ops/scripts` 已清理，统一仅保留 `src/ops/cli/*` 作为执行入口。  
  - 变更效果：入口可读性与可追踪性提升，文档入口与实现保持一致。  

- `persistence`：  
  - 结果：分层较正，repositories 与 db/writer 职责分离明显；入口集中在 `repositories/`。  
  - 建议：保留现状，增加一次 `writer/schema` 的 contract 测试（DDL、insert、upsert 的返回值与异常码一致性）。

- `research` / `readmodels` / `studio`：  
  - 结果：三者定位明确（实验计算、读模型投影、前端观测），职责分界清楚。  
  - 建议：保持现状。

#### 该轮结论

- 大部分包职责边界已可接受，不再存在典型“同文件跨域合并”问题。  
- 当前最优先关注点仍在高风险链路：`trading`、`signals`、`indicators`，其余包可以采用低优先级清洁度改造（以异常语义与观测可解释性为主）。  

### 6.H 全量包职责边界清单（2026-04-11）

- `api`：HTTP 适配与路由组织层，职责正确。  
- `app_runtime`：运行时装配与生命周期层，职责正确。  
- `backtesting`：回测与实验运行时，职责正确。  
- `calendar`：经济事件窗口与日历风控前置，职责正确。  
- `clients`：外部系统客户端封装，职责正确。  
- `config`：配置模型与配置加载层，职责正确。  
- `entrypoint`：启动入口层，职责正确。  
- `indicators`：指标计算与状态计算层，职责正确。  
- `ingestion`：行情历史与实时抓取层，职责正确。  
- `market`：市场状态与行情服务层，职责正确。  
- `market_structure`：市场结构特征层，职责正确。  
- `monitoring`：可观测与健康度采集层，职责正确。  
- `ops`：运维工具层，已完成入口统一，仅保留 `cli`。  
- `persistence`：持久化与仓储层，职责正确。  
- `readmodels`：读模型与投影层，职责正确；新增 `decision` 决策摘要逻辑收敛到该层。  
- `research`：研究实验算子层，职责正确。  
- `risk`：风控领域层，职责正确。  
- `signals`：信号与策略调度层，职责正确。  
- `studio`：前端可观测数据服务层，职责正确。  
- `trading`：交易执行与仓位协调层，职责正确。  
- `utils`：通用工具层，职责正确。  

执行落地：  

- [x] `decision` 包职责收口到 `readmodels`。  
- [x] `ops/scripts` 历史重复入口清理。  
- [x] 命令入口文档与实现对齐。  

### 6.I 启动阻塞修复记录（2026-04-11）

- 发现问题：`src/app_runtime/factories/signals.py` 的 `build_executor_config()` 已透传 `htf_conflict_block_timeframes` / `htf_conflict_exempt_categories`，但 `src/trading/execution/executor.py` 的 `ExecutorConfig` 未同步声明同名字段，导致 `deps._ensure_initialized()` 在真实容器初始化阶段直接抛 `TypeError`，服务无法进入 lifespan。  
- 本次修复：恢复装配层与执行层配置契约一致性，`ExecutorConfig` 正式补齐上述两个字段；未新增兼容分支，也未改变现有调用边界。  
- 验证补强：新增 `tests/app_runtime/test_signal_factory.py`，直接覆盖 `SignalConfig -> build_executor_config() -> ExecutorConfig` 的字段对账，避免未来再次出现“入口 smoke 通过，但真实容器初始化失败”的装配断裂。  
- 未决项：这两个 HTF conflict 字段当前已恢复配置契约，但预交易流水线尚未消费它们；如果后续确认该能力需要生效，应在 `pre_trade_pipeline / pre_trade_checks` 中补正式门禁，而不是继续停留在“可配置但未落地”的状态。  

## 7. 验证记录

本次包含 Indicators 收口与测试同步，已执行：

- 已完成一轮职责边界复核，确认 `query_services`、`runtime`、`manager` 的边界已按新目录收口。
- 已修复 import cycle（`runtime.bar_event_handler` 与 `query_services.runtime`、`pipeline_runner` 与 `runtime` 间）。
- 冒烟测试命令执行通过：  
  `pytest -q tests/indicators/test_core_functions.py tests/indicators/test_flush_event_batch.py tests/indicators/test_manager_intrabar.py -q`
- 回测入口自检通过：  
  `python -m src.backtesting --help`

#### 全量回归（2026-04-11）

- 执行命令：`pytest -q tests`
- 结果：`1214 passed in 61.95s (0:01:01)`
- 说明：全量测试套件已通过，当前审查结果无阻断级回归。
- 语法复核：`python -m py_compile src/api/monitoring_routes/health.py src/api/monitoring_routes/runtime.py`
