# Live Readiness Execution Checklist

> **For agentic workers:** 按阶段执行，逐项勾选，不允许跳过前置门禁；禁止用兼容补丁、双轨语义、私有属性探测代替正式重构。

**Goal:** 将当前仓库从“研究/验证平台”收口到“可验证 demo + 可控 guarded live”的无人值守日内自动交易系统。

**Architecture:** 先统一执行合同，再打通 demo 验证闭环，再修控制面与观测面，再修多账户/多实例边界，最后才放开 guarded live；所有整改必须围绕单一职责、单向依赖、正式端口、可追踪状态展开。

**Tech Stack:** FastAPI, MT5, TimescaleDB/PostgreSQL, 后台线程组件, 结构化 readmodel, Backtesting/Research pipeline。

---

## 1. 目标状态定义

### 1.1 Demo Ready

- `demo_validation` 策略在 `demo` 环境能够真实产出 execution intent。
- `signal -> filter -> intent -> order -> fill -> outcome` 全链路可追踪、可投影、可审查。
- `demo` 装配集合、binding 集合、intent 路由集合完全一致。

### 1.2 Guarded Live Ready

- 至少 1 条 `active_guarded` 策略在 1 个 live executor 上可稳定运行。
- 控制面在瞬时 DB / 网络异常下不会永久停摆。
- 健康报告和读模型不再把 failed / critical 状态漂白成 healthy。
- 同 ticket / 同 signal / 同组件任务不会因为多账户、多实例而串状态。

### 1.3 Scale Ready

- 多账户、多 executor、多实例共库运行时，状态主键、trace、任务恢复、健康投影全部按账户和实例正确隔离。
- 研究/挖掘结果可以稳定流入晋升链路，不再依赖人工补救资源清理或观察隐式失败。

---

## 2. 不可妥协的整改原则

- [ ] 不允许新增兼容参数别名、双轨执行路径、兜底 silent fallback。
- [ ] 不允许 API / readmodel / runtime / studio 继续探测私有属性或历史字段。
- [ ] 不允许用“先跑起来”把命令、查询、状态持久化、监控投影混在同一入口。
- [ ] 不允许配置、装配、执行合同各自维护一套“谁可以执行”的独立真相。
- [ ] 不允许后台线程在 `join(timeout)` 后无条件清空线程引用；线程仍活着就必须保留引用并暴露真实 running 状态。
- [ ] 不允许多账户/多实例共享无维度主键或恢复键。
- [ ] 不允许运维 CLI、健康报告、读模型继续输出与真实执行链不一致的结论。

---

## 3. 阶段顺序与门禁

### 阶段 A：执行合同统一

前置门禁：

- [ ] 明确 deployment status 到 environment/intent/execution 的唯一合同。
- [ ] 明确哪些模块只负责“声明”，哪些模块只负责“消费合同”。

阶段完成前禁止：

- [ ] 禁止把任何策略直接升到 `active`。
- [ ] 禁止通过下调 `min_confidence` 逼系统出单。

### 阶段 B：Demo 闭环

前置门禁：

- [ ] 阶段 A 全部完成。
- [ ] `demo` 下装配、binding、publisher 三者结果一致。

### 阶段 C：控制面与健康面可信

前置门禁：

- [ ] 阶段 B 全部完成。
- [ ] 能区分“没有 live 策略”和“有策略但被门槛阻断”。

### 阶段 D：多账户 / 多实例边界收口

前置门禁：

- [ ] 阶段 C 全部完成。
- [ ] 所有 state schema、readmodel、trace 都已明确状态拥有方。

### 阶段 E：恢复链 / 启动链正确性

前置门禁：

- [ ] 阶段 D 全部完成。
- [ ] 启动恢复时任何脏数据、失败 payload、线程停不下来的状态都不会被假装成功。

### 阶段 F：Guarded Live Canary

前置门禁：

- [ ] 阶段 A-E 全部完成。
- [ ] 至少 1 条策略完成 demo 闭环验证并通过晋升门槛。

### 阶段 G：Research / Mining 稳定供给

前置门禁：

- [ ] 阶段 F 已能支撑最小 live canary。
- [ ] 研究产出不再影响执行链正确性。

---

## 4. 模块化执行清单

## WP1. 执行合同统一

**目标**

把“谁在什么环境可以装配 / 发 intent / 进入执行检查 / 真正下单”收口成单一正式合同。

**职责边界**

- `src/signals/contracts/`：定义 deployment status、环境执行资格、最小 confidence 合同。
- `src/app_runtime/factories/`：只负责按合同装配策略，不做额外业务推断。
- `src/trading/intents/`：只负责把已装配且允许执行的信号路由到账户，不重新发明环境资格规则。
- `src/trading/execution/pre_trade_checks.py`：只做执行前门禁，不再补第二套 deployment 解释。
- `config/signal*.ini`：只声明策略状态、binding、confidence，不编码额外隐式逻辑。

**涉及文件**

- `src/signals/contracts/deployment.py`
- `src/app_runtime/factories/signals.py`
- `src/trading/intents/publisher.py`
- `src/trading/execution/pre_trade_checks.py`
- `config/signal.ini`
- `config/signal.local.ini`

**执行清单**

- [ ] 定义一张正式矩阵：`candidate / demo_validation / active_guarded / active` 在 `demo / live` 下的装配、intent、执行资格。
- [ ] 将 `ExecutionIntentPublisher` 改为消费正式合同，而不是写死 `allows_live_execution()`。
- [ ] 将 `pre_trade_checks` 的 `effective_min_confidence` 判定并入正式执行合同，不允许 CLI/执行链分叉。
- [ ] 增加配置校验：binding 中只允许出现当前环境实际可装配策略；无效策略名直接视为配置错误。
- [ ] 清理历史文档与注释中 `paper trading`、`live only publisher` 等旧语义。

**禁止事项**

- [ ] 禁止保留“demo 装配一套、publisher live-only 一套”的双轨语义。
- [ ] 禁止新增兼容分支去映射旧 deployment 名称或旧 binding 语义。

**验证**

- [ ] 为 4 种 deployment status 各补 1 组装配/intent/pre-trade 一致性测试。
- [ ] 对比 `demo` 与 `live` 环境下的装配结果、publisher 路由结果、pre-trade 决策结果，确保完全一致。

**退出标准**

- [ ] 同一策略在同一环境下的“是否可执行”只有一个答案，且所有调用方一致。

---

## WP2. Demo 验证闭环

**目标**

让 `demo` 真正承担“真实执行验证”职责，而不是展示环境。

**职责边界**

- `src/trading/intents/`：生成 intent。
- `src/trading/`：负责订单、成交、结果跟踪。
- `src/readmodels/trade_trace.py`：负责按账户、按 signal、按阶段展示完整链路。
- `config/signal.local.ini`：负责 demo binding 的显式声明，不再漂移。

**涉及文件**

- `src/trading/intents/publisher.py`
- `src/readmodels/trade_trace.py`
- `src/trading/tracking/trade_outcome.py`
- `config/signal.local.ini`

**执行清单**

- [ ] 修复 `demo_validation` 在 `demo` 环境不产出 execution intent 的问题。
- [ ] 对齐 `demo_main` binding 与 demo 实际装配集合，移除不会装配的策略，补齐会装配但未绑定的策略。
- [ ] 收口 `signal_id` 单键覆盖问题，确保同 signal 的多账户 / 重复执行不会覆盖活跃交易或挂起 entry。
- [ ] 修复 `trade_trace` 对 `auto_executions`、`trade_outcomes` 缺少账户过滤与去重维度的问题。
- [ ] 修复 `TradeOutcomeTracker` 对 `close_source`、活跃交易索引的错误建模，确保结果审计和真实平仓来源一致。

**禁止事项**

- [ ] 禁止继续用 `signal_id` 作为跨账户全局唯一活动状态主键。
- [ ] 禁止通过“只在 demo-main 单实例运行”来回避多账户语义问题。

**验证**

- [ ] 每条 `demo_validation` 策略都能在 demo 产出 intent。
- [ ] trace 能展示 signal、intent、order、fill、outcome 的完整时间线。
- [ ] 多账户同 `signal_id` 下结果不会互相覆盖或串读。

**退出标准**

- [ ] Demo 环境成为真实执行验证环境，且结果可审查、可复现。

---

## WP3. 控制面与健康面可信化

**目标**

让 operator command、健康报告、运维诊断对真实状态负责，不再出现 silent death 和假绿。

**职责边界**

- `src/trading/commands/consumer.py`：只负责稳定消费 operator command，不因一次基础设施异常死亡。
- `src/monitoring/`：只负责准确表达真实状态，不对 failed/critical 做漂白。
- `src/readmodels/runtime.py`：只负责投影真实状态，不引入“友好但错误”的推断。
- `src/ops/cli/`：只做诊断，不自造与执行链矛盾的结论。

**涉及文件**

- `src/trading/commands/consumer.py`
- `src/monitoring/manager.py`
- `src/monitoring/health/reporting.py`
- `src/monitoring/health/common.py`
- `src/monitoring/health/monitor.py`
- `src/trading/state/alerts.py`
- `src/readmodels/runtime.py`
- `src/ops/cli/confidence_check.py`

**执行清单**

- [ ] 为 operator command consumer 的 claim / complete / process 三层调用补正式异常边界与存活策略。
- [ ] 修复 `active_alerts`、failed summary、critical trading metrics 不影响 overall status 的问题。
- [ ] 修复 `pending_monitor_alive` 之类“有指标、无告警规则”的半接线状态。
- [ ] 修复 `TradingStateAlerts.summary()` 在状态源和 MT5 同时失败时仍返回 healthy 的问题。
- [ ] 修复 `RuntimeReadModel` 对 pending manager / trading summary 的 healthy 漂白问题。
- [ ] 重写 `confidence_check` 的结论模型：先判断是否存在 live-eligible 策略，再判断 confidence 阈值；纳入 `deployment.effective_min_confidence()`；symbol + timeframe + regime 维度必须同时存在。

**禁止事项**

- [ ] 禁止继续用空列表、空 summary、空 metrics 代表“健康”。
- [ ] 禁止健康报告同时出现 `critical alerts` 和 `overall_status=healthy`。
- [ ] 禁止 CLI 把“没有 live 策略”误报成“阈值太高”。

**验证**

- [ ] 模拟 claim/complete/DB 写失败后，consumer 线程仍存活。
- [ ] 存在 critical alert 或 failed summary 时，overall status 必须降级。
- [ ] `confidence_check` 输出与真实 pre-trade 执行逻辑一致。

**退出标准**

- [ ] 控制面异常不会 silently down，健康面不会 silently green。

---

## WP4. 多账户 / 多实例边界收口

**目标**

让所有状态、trace、runtime task、schema 的键语义与部署拓扑一致。

**职责边界**

- `src/persistence/schema/`：定义真实主键与 upsert 语义。
- `src/readmodels/`：按账户 / 实例投影，不跨边界聚合。
- `src/calendar/`：按实例恢复和展示 runtime task 状态。

**涉及文件**

- `src/persistence/schema/pending_order_states.py`
- `src/persistence/schema/position_runtime_states.py`
- `src/persistence/schema/trade_control_state.py`
- `src/persistence/schema/runtime_tasks.py`
- `src/readmodels/trade_trace.py`
- `src/readmodels/workbench.py`
- `src/calendar/economic_calendar/calendar_sync.py`
- `src/calendar/read_only_provider.py`

**执行清单**

- [ ] 将 `pending_order_states` 和 `position_runtime_states` 的主键与 upsert 冲突目标扩展为账户维度键。
- [ ] 修复 `trade_control_state` 的主键与唯一键冲突目标不一致问题。
- [ ] 修复 `trade_trace` 的查询过滤与去重键，补齐 account 维度。
- [ ] 修复 delegated / multi_account 拓扑下 workbench、tradability、executor source kind 的错误推断。
- [ ] 修复 `runtime_task_status` 的 `legacy` 主键空间问题；没有 runtime identity 时必须明确拒绝共享持久化或生成真实隔离 identity。
- [ ] 修复 calendar 恢复与 read-only provider 只按 role/account 查询、不带 instance_id 的问题。

**禁止事项**

- [ ] 禁止继续使用“ticket 全局唯一”“signal 全局唯一”“main role 即可代表所有实例”的旧假设。
- [ ] 禁止通过注释声明“当前单账户所以没问题”来跳过 schema 修正。

**验证**

- [ ] 两个账户同 ticket 的 pending/position state 不再互相覆盖。
- [ ] 两个实例共库运行时不会互相恢复 runtime task。
- [ ] 多账户同 signal 的 trace 事实不会串读，也不会被错误去重。

**退出标准**

- [ ] 账户和实例成为一等建模维度，不再是隐含上下文。

---

## WP5. 恢复链 / 启动链正确性

**目标**

让启动恢复、挂单恢复、状态重建、线程停止语义全部与真实运行态一致。

**职责边界**

- `src/trading/state/`：负责恢复策略与恢复结果分类。
- `src/signals/orchestration/runtime_recovery.py`：负责 runtime state 恢复，不让单条脏数据打崩全局。
- `src/calendar/economic_calendar/calendar_sync.py`：负责 worker 生命周期与 job state 持久化。
- `src/readmodels/cockpit.py`：只投影底层数据的新鲜度，不发明 fresh。

**涉及文件**

- `src/trading/state/recovery.py`
- `src/trading/state/recovery_policy.py`
- `src/signals/orchestration/runtime_recovery.py`
- `src/calendar/economic_calendar/calendar_sync.py`
- `src/readmodels/cockpit.py`
- `src/signals/evaluation/calibrator.py`

**执行清单**

- [ ] 统一 `_ticket_was_cancelled()` 的失败 payload 合同，正式支持 `failed=[{'ticket': ..., 'error': ...}]`。
- [ ] 修复过期挂单撤单失败被误记为 restored 的问题。
- [ ] 修复 orphan pending order 在标准 failed payload 下直接崩溃的问题。
- [ ] 为 runtime state 恢复增加单行隔离；坏时间串只能标记坏记录，不能中断整体恢复。
- [ ] 修复 calendar worker 停不下来时仍被持久化为 `stopped` 的语义错误。
- [ ] 修复 cockpit `exposure_map` freshness 用 `observed_at` 伪装底层更新时间的问题。
- [ ] 修复 calibrator / 其他后台线程 stop 后无条件清空线程引用的问题。

**禁止事项**

- [ ] 禁止宽泛 `except Exception` 把“恢复失败”伪装成“恢复成功”。
- [ ] 禁止把当前观测时间当成底层状态更新时间。
- [ ] 禁止线程仍活着时把运行态写成已停止。

**验证**

- [ ] 标准 failed payload 下恢复链不会崩，也不会分类错误。
- [ ] 恢复数据包含坏时间串时，系统仍能完成启动。
- [ ] worker 未退出时，内存态和持久化状态都不会写成 `stopped`。

**退出标准**

- [ ] 启动恢复链只会“正确恢复”或“显式失败”，不再“静默错恢复”。

---

## WP6. Guarded Live Canary

**目标**

在最小风险下验证 live 执行面，而不是一次性放开生产。

**职责边界**

- `config/signal*.ini`：声明唯一 canary 策略与绑定。
- `docs/runbooks/`：声明唯一 live canary 操作流程。
- `src/ops/cli/`：提供与真实执行合同一致的诊断工具。

**涉及文件**

- `config/signal.ini`
- `config/signal.local.ini`
- `docs/runbooks/system-startup-and-live-canary.md`
- `docs/design/entrypoint-map.md`
- `docs/codebase-review.md`

**执行清单**

- [ ] 只选择 1 条通过 demo 验证的策略升到 `active_guarded`。
- [ ] 只绑定 1 个 live executor，限制为 1 个 symbol、最小仓位、最小交易窗口。
- [ ] 更新 runbook：启动前检查、启动后巡检、开盘窗口 canary、回滚条件全部以正式合同为准。
- [ ] 为 guarded live 设定明确停机条件：拒单率、滑点、执行失败率、熔断状态、trace 丢失、健康降级。

**禁止事项**

- [ ] 禁止在第一轮 live canary 同时升多条策略。
- [ ] 禁止用“人工盯盘可接受”来替代健康/trace/读模型的缺陷修复。

**验证**

- [ ] canary 期间所有订单都能从 signal 追到 outcome。
- [ ] 任一停机条件触发时，系统能显式进入安全状态。

**退出标准**

- [ ] 至少 1 条策略完成 guarded live 真实验证，且整条链路可复盘。

---

## WP7. Research / Mining 稳定供给

**目标**

让 research/mining 成为长期可运行的候选供给链，而不是一次性实验脚本。

**职责边界**

- `src/backtesting/component_factory.py`：负责 research deps 的正式资源所有权。
- `src/research/nightly/runner.py`：负责 nightly orchestration、异常边界、资源释放。
- `src/api/research_routes/routes.py`：负责受控任务状态与结果生命周期。
- `src/backtesting/cli.py`：负责 CLI 路径 cleanup 完整性。
- `docs/research-system.md`：负责研究到晋升的唯一流程定义。

**涉及文件**

- `src/backtesting/component_factory.py`
- `src/research/nightly/runner.py`
- `src/api/research_routes/routes.py`
- `src/backtesting/cli.py`
- `docs/research-system.md`

**执行清单**

- [ ] 给 research deps 暴露正式 cleanup 端口或上下文生命周期，不再丢失 writer/pipeline 所有权。
- [ ] nightly runner 对基础设施错误 fail-fast，不把 `ConnectionError` 伪装成普通 failed run。
- [ ] nightly/backtest/mining 路径统一资源释放：writer、pipeline、线程池、连接池都必须在 finally 中关闭。
- [ ] 为 research API 的 `_mining_jobs` 增加容量上限或 TTL。
- [ ] 把文档里的 `Paper Trading` 全部替换为当前真实链路：`demo_validation -> active_guarded -> active`。

**禁止事项**

- [ ] 禁止研究组件继续依赖“调用方自己记得 cleanup”的隐式约定。
- [ ] 禁止 API 进程无上限堆积完整研究结果。

**验证**

- [ ] 多轮 nightly / mining 运行后无连接、线程池、pipeline 泄漏。
- [ ] 基础设施错误会显式 fail-fast。
- [ ] 研究结果生命周期受控，内存不线性膨胀。

**退出标准**

- [ ] research/mining 可长期运行，且不再拖累执行系统稳定性。

---

## 5. 测试与验证矩阵

### 5.1 合同测试

- [ ] deployment status × environment × intent × pre-trade 一致性测试
- [ ] binding 校验测试
- [ ] CLI 诊断与执行链一致性测试

### 5.2 多账户 / 多实例测试

- [ ] 同 ticket 多账户状态不覆盖
- [ ] 同 signal 多账户 trace 不串读
- [ ] 共库多实例 runtime task 不串恢复

### 5.3 恢复链测试

- [ ] 标准 failed payload 下恢复分类正确
- [ ] orphan pending 不崩
- [ ] 坏历史数据不会打崩 runtime state 恢复

### 5.4 存活性测试

- [ ] operator command consumer 遇 claim / complete / DB 异常后仍存活
- [ ] pending manager / calibrator / calendar worker stop 语义真实
- [ ] trace recorder / monitoring path 遇瞬时写入失败后可恢复

### 5.5 观测一致性测试

- [ ] critical alert 必然反映到 overall status
- [ ] cockpit freshness 反映底层真实更新时间
- [ ] pending / trading summary 不再出现 `running=False` 但 `status=healthy`

---

## 6. 文档同步清单

- [ ] 更新 `docs/codebase-review.md`：记录本执行清单的治理目标与未决项。
- [ ] 更新 `docs/research-system.md`：移除 `Paper Trading` 旧流程。
- [ ] 更新 `docs/runbooks/system-startup-and-live-canary.md`：以 guarded live 为唯一 runbook。
- [ ] 更新 `docs/design/entrypoint-map.md`：如启动/控制面组件职责有变化，必须同步。
- [ ] 如执行合同重构，补充 `docs/architecture.md` 或 `docs/design/adr.md` 的职责边界说明。

---

## 7. 最终放行条件

只有在以下条件全部满足后，才允许把系统判定为“可用”：

- [ ] Demo Ready 已完成
- [ ] Guarded Live Ready 已完成
- [ ] 至少 1 条 `active_guarded` 策略完成真实 canary
- [ ] 控制面异常不会导致长期 silent down
- [ ] 健康/读模型/CLI 不再输出假绿或假结论
- [ ] 多账户 / 多实例边界已收口
- [ ] 恢复链分类与线程生命周期语义已修正
- [ ] research/mining 不再依赖人工 cleanup 和隐式观察

在上述条件满足前，系统只能被称为：

- `研究/验证平台`
- 或 `Demo 闭环验证中`

不能被称为：

- `可上线 live 自动交易系统`
- `可扩容多账户实盘系统`

