# 代码库审查报告

> 首次审查日期：2026-04-10
> 最近更新：2026-04-12
> 范围：当前工作区全量源码、配置与主要文档。
> 结论定位：风险台账与后续整改入口，不代表已修复代码问题。

---

## 0. 2026-04-11 ~ 2026-04-12 修复更新

本轮已直接处理并验证以下启动阻塞项：

补充更新（2026-04-13）：

- **`economic_calendar_staleness` 监控阈值已按最快启用的经济日历刷新路径收口**  
  之前 health monitor 固定把 warning 阈值设成 `stale_after_seconds / 2`，在 `release_watch_idle_interval_seconds = 1800`、`stale_after_seconds = 1800` 的配置下，会在服务仍然 `health_state=ok / stale=false` 时提前持续刷 warning。现已改为按“最快启用刷新路径的最大预期间隔”计算 warning 阈值：当前 `calendar_sync=21600 / near_term=0 / release_watch_idle=1800` 时，warning 与 critical 都对齐到 `1800s`，避免 idle release_watch 期间的误报型日志噪音；若未来重新启用 `near_term_sync=900`，warning 也会自动回落到 `900s`。

1. **demo/live 环境已收口为 topology 的唯一事实源，MT5 不再反推系统环境**  
   `load_db_settings()` 现已只按当前 topology environment 路由到 `db.live` / `db.demo`，不再从 MT5 账户字段二次推导；`RuntimeIdentity` 统一收口为 `instance_id / instance_role / live_topology_mode / environment / account_alias / account_key`。启动时会强制校验：`demo` 只允许 `single_account + main`，`live` 的 `multi_account` 下所有启用账户必须使用不同 terminal path。回测、research、experiment 入口也已改为默认读取当前环境数据库，不再写死 `live` 或隐式跟随旧单库。

2. **自动交易已统一收口到 `execution_intents` 队列，而不再保留“信号直接下单”双轨语义**  
   `main` 现在只负责写入 `execution_intents`，`executor`/单账户 main 再按 `target_account_key` claim 并执行；`signal_runtime -> trade_executor` 的直接监听已移除，改为 `ExecutionIntentPublisher + ExecutionIntentConsumer` 的正式交付面。当前一期策略分发来源固定为 `signal.ini` 中的 `account_bindings.<alias>.strategies` 静态绑定，`single_account` 下默认投递到当前账户，`multi_account` 下只投递到显式绑定账户。

3. **signal performance 与 execution performance 已拆成两条正式链路**  
   `SignalModule` 现在只消费基于 `signal_outcomes` 的 signal performance；账户真实成交侧则通过独立的 execution performance tracker 仅消费当前 `account_key` 的 `trade_outcomes`。warm-up 也已同步拆分为 `fetch_recent_signal_outcomes()` 与 `fetch_recent_trade_outcomes(account_key=...)`，避免不同 live 账户的真实盈亏再反向污染当前账户的信号 multiplier。

4. **账户事实表已补齐 `account_key`，运行态追踪补齐 `instance_id / instance_role`**  
   `auto_executions`、`trade_outcomes` 之外，本轮继续把 `trade_command_audits`、`pending_order_states`、`position_runtime_states`、`trade_control_state`、`position_sl_tp_history` 的写库契约补齐到 `account_key`；`runtime_task_status`、`pipeline_trace_events` 已补上实例维度，执行侧事件同时带 `intent_id` / `account_key`。现阶段仍保留 `account_alias` 作为展示字段与兼容过滤口径，但新的事实写入已经按稳定 `account_key` 输出，读模型/API 可逐步迁移到 key 口径。

5. **SL/TP 历史写入已从占位 alias 回到运行时真实账户语义**  
   `PositionManager` 过去写 `position_sl_tp_history` 时仍依赖空 `account_alias` 占位；当前已改为在运行时装配层基于 `RuntimeIdentity` 注入真实 `account_alias + account_key`，避免多账户下 SL/TP 审计继续丢账户归属。

6. **本轮改动已补齐定向回归测试**  
   已新增/扩展配置与交易侧回归，覆盖 `db` 分库路由、`validate_mt5_topology()` 的多终端冲突校验、`RuntimeIdentity` 的 `account_key` 生成、`ExecutionIntentPublisher/Consumer` 的单账户/多账户发布消费语义，以及 `TradingStateStore` / `TradingModule` 的 `account_key` 落库。定向执行通过：`tests/config/test_mt5_multi_account_config.py`、`tests/trading/test_execution_intents.py`、`tests/trading/test_state_store.py`、`tests/trading/test_trading_module.py`、`tests/readmodels/test_trade_trace.py`、`tests/api/test_trade_api.py`、`tests/api/test_monitoring_runtime_tasks.py`、`tests/readmodels/test_runtime.py`、`tests/config/test_signal_config.py`、`tests/app_runtime/test_signal_factory.py`。

7. **指标 durable event 消费断链已修复**  
  `IndicatorEventLoop` 已改为调用 `event_store.claim_next_events(...)`，真实启动时事件循环线程可正常存活，不再因接口名不匹配崩溃。

8. **`runtime_task_status` 持久化契约已统一并补库表迁移**  
  新增统一状态集合，覆盖 `ready / failed / idle / disabled / ok / partial / error / stopped` 等当前真实生产者语义；`TimescaleWriter.init_schema()` 现在会在启动时重建相关 check constraint，现有数据库不需要手工删表。

9. **经济日历 `session_bucket/status` 与 schema 漂移已修复并补库表迁移**  
  现已允许 `all_day / asia_europe_overlap / europe_us_overlap`，同时允许 `imminent / pending_release` 状态；真实启动后不再出现该表 check constraint 失败和因此进入 DLQ 的问题。

10. **`StorageWriter.stop()` 生命周期语义已修正**  
  线程 `join(timeout)` 后若仍存活，将保留线程引用并保持 `is_running()` 为真，避免误判组件已停止。

11. **`/health` 已扩展为链路级组件视图**  
   除市场与交易摘要外，现在还会返回 `storage_writer / indicator_engine / signal_runtime / trade_executor / pending_entry_manager / position_manager / economic_calendar` 运行状态，便于直接判断“采集 → 指标 → 信号 → 执行”链路是否在线。

12. **日志目录已统一回到项目根目录 `data/`**  
   `src.entrypoint.web` 的相对 `log_dir` 解析现已锚定仓库根目录，不再向 `src/entrypoint/data/logs` 写入；本轮也已把历史遗留日志迁移到根目录 `data/logs/` 下的 `legacy-src-entrypoint-*.log`。

13. **系统 readiness 盲区已补齐：采集线程现在纳入就绪探针**  
   `/monitoring/health/ready` 过去只看 `storage_writer` 与 `indicator_engine`，即使 `BackgroundIngestor` 已退出也可能误报 `ready`。本轮已把 `ingestion` 纳入同一探针，`RuntimeReadModel.build_storage_summary()` 也同步把 `ingest_alive=false` 视为 `critical`。

14. **`PipelineTraceRecorder` 生命周期语义已修正**  
   该组件此前在 `stop()` 的 `join(timeout)` 后会无条件清空线程引用，且监听器注册失败时仍会假装启动成功。本轮已改为：监听器挂接失败立即抛错；线程未退出时保留引用并维持真实运行态，避免 observability 组件假死但状态看起来正常。

15. **`calendar` 包顶层导入导致的循环依赖已修复**  
   `src.persistence.schema.economic_calendar` 引入 contract 时会先执行 `src.calendar.__init__`，此前这里立即导入 `service`，从而形成 `db -> schema -> calendar -> service -> db` 的循环。现已改为懒加载导出，不再阻塞采集/持久化相关测试与运行时装配。

16. **测试资产已做一轮去噪和契约收口**  
   已删除 7 个只有 `pytest.mark.skip`、实际不产生任何覆盖的历史空文件；同时移除了 2 个“打印 + 吞异常”的脚本式伪测试。`tests/data/test_data_layer.py` 也已改造成无外部 DB 依赖的真实单元测试，`tests/api/test_monitoring_config_reload.py` 则同步对齐当前 404 契约，避免旧口径误报失败。

17. **`SignalRuntime` 测试已从私有子方法绑定收口到行为契约**  
   原 `tests/signals/test_runtime_submethods.py` 直接断言 `_dequeue_event / _is_stale_intrabar / _apply_filter_chain / _detect_regime` 等私有实现细节，重构成本高而行为保护有限。本轮已删除该文件，并把仍有价值的覆盖迁移为 `tests/signals/test_signal_runtime.py` 中的行为测试，直接校验队列反饥饿、stale intrabar 丢弃、filter 统计和 stop 后队列清理。

18. **`docs/` 已完成一次“运行时真相 / 审计治理 / 方案规划”分层审计，并新增单一导航入口**  
   已新增 `docs/README.md` 作为文档入口，`architecture / full-runtime-dataflow / signals-dataflow-overview / intrabar-data-flow / entrypoint-map` 已明确标注为当前实现真相文档；`signal-system / research-system / next-plan / r-based-position-management` 等文档已补充“设计参考/规划”定位说明，避免再把方案稿直接当作当前运行结论。`architecture.md` 也已把本应属于专题设计的参数表和方案细节收口到对应专题文档。与此同时，`docs/` 下运行时流图已统一为 Claude 风格 ASCII，不再保留 Mermaid 图。

19. **系统启动巡检与 live canary 已收口成独立 runbook**  
   已新增 `docs/runbooks/system-startup-and-live-canary.md`，把 `live_preflight`、启动后 5 分钟巡检、休盘/开盘日志判读、以及开盘窗口的系统级 live canary 步骤统一成一套固定流程。`entrypoint-map.md` 与 `docs/README.md` 已同步改为链接到该 runbook，避免启动入口文档和运行时文档再各自维护一套巡检说明。

20. **回测已正式区分 `research` 与 `execution_feasibility` 两种执行语义**  
   本轮没有拆分指标、策略、过滤或 regime/voting 内核，仍然复用同一套生产数据指标流；变化只发生在“信号如何转成可成交动作”这一层。`BacktestConfig` 新增 `simulation_mode`，CLI / API / `config/backtest.ini` 已能显式指定；`BacktestResult` 新增 `execution_summary`，会输出当前模式、accepted entries、rejected entries 与 rejection reasons。`research` 模式允许理论子最小手数仓位继续用于策略研究，`execution_feasibility` 模式则会在最小手数不可成交时明确拒单，避免再把研究回测误当成上线可执行性结论。

21. **手工运行产物目录已收口到 `data/artifacts/`**  
   根目录平级 `runtime/` 过去混放了回测 JSON、压测日志、启动排查 stdout/stderr 等人工执行产物，语义上与正式运行期目录 `data/` 并行，容易形成第二套“事实源”。本轮已把这类文件迁到 `data/artifacts/`，并在运行时流图/runbook 中明确：`data/` 是唯一运行期根目录，`data/artifacts/` 只承载手工回测、压测、排障输出，仓库根目录不再保留 `runtime/`。

22. **Research feature → shared indicator 的半自动晋升链路已落地第一版**  
   本轮新增了 `FeatureCandidateSpec / IndicatorPromotionDecision / FeaturePromotionReport`，并把 research feature registry 的元数据扩展为 `formula_summary / source_inputs / runtime_state_inputs / live_computable / compute_scope / bounded_lookback / strategy_roles / promotion_target_default`。`MiningRunner` 现在可直接输出 feature candidate 工件与 promotable 过滤结果；首个真实晋升指标 `momentum_consensus14` 已注册进 `config/indicators.json`，并接入 `structured_breakout_follow` 作为共享动量一致性确认因子。与此同时，回测验证报告已能携带 `feature_candidate_id / promoted_indicator_name / strategy_candidate_id / research_provenance`，用于把 research → indicator → strategy 的证据链真正串起来。

23. **`src/research` 已按职责边界重组为 `core / analyzers / features / strategies / orchestration`**  
   过去 `src/research` 顶层同时平铺 `runner.py`、`data_matrix.py`、`feature_candidates.py`、`candidates.py`、`models.py` 等文件，公共基础、feature 路径和 strategy 路径混在同一层，包边界不清晰。本轮已把公共基础能力收口到 `src/research/core/`，将 research feature / indicator promotion 路径收口到 `src/research/features/`，将 strategy candidate 发现收口到 `src/research/strategies/`，并把编排入口迁到 `src/research/orchestration/runner.py`。共享统计分析器仍保留在 `src/research/analyzers/`，避免按“指标版/策略版”复制两套证据引擎。

24. **`docs/research-system.md` 已补齐 research 模块职责、流程和关键文件作用说明**  
   在目录重组之后，`research-system.md` 已同步新增“模块职责总览”和“关键文件职责表”，明确写清 `orchestration / core / analyzers / features / strategies` 五层的输入、输出和边界，并把 research 的两条正式分支整理为“feature/indicator 晋升路径”和“strategy candidate 晋升路径”。这样后续再看 `src/research/*` 时，不需要再从代码倒推“谁负责编排、谁负责证据、谁负责候选工件”，减少继续演化时的边界漂移风险。

25. **首个真实 promoted indicator 已接入受限 consumer strategy，并补齐 research / execution / WF 证据链**  
    本轮没有继续把 `momentum_consensus14` 硬塞到休眠策略里，而是新增了 `structured_trend_h4_momentum` 作为 `StructuredTrendContinuation` 的受限变体：复用同一套结构化策略骨架，只在 `why` 层接入 `momentum_consensus14`，并通过 `strategy_deployment` 明确收口为 `paper_only + tf_specific + locked_timeframes=H1 + locked_sessions=london,new_york`。同时，`ValidationDecision` 已修正为支持 `research backtest result + execution_feasibility result` 双结果输入，不再混用一份回测结果承担两种语义；`walkforward_runner` 也已修复对旧字段 `is_result/oos_result` 的错误引用，并把 split 详情落盘。当前真实产物表明：该指标的 promotion 成立，但首个 downstream strategy consumer 结论仍为 `refit`，主因是研究回测样本不足、执行可行性下最小手数全部拒单，以及 WF 一致性仅 40%。

26. **QuantX 前端消费契约已从“多接口手工拼装”收口到正式分页/目录/流式接口**  
    本轮围绕 `docs/quantx-backend-backlog.md` 与 `docs/design/quantx-trade-state-stream.md`，把原先只适合后台排查的只读接口升级为前端可直接消费的正式契约：`/signals/recent` 现已支持 `direction/status/from/to/page/page_size/sort`；`/trade/command-audits` 新增 `symbol/signal_id/trace_id/actor` 与时间窗口分页；`/trade/traces` 提供 trace 目录视图；`/trade/state/stream` 提供统一 SSE 状态流（`state_snapshot`、`position_changed`、`order_changed`、`pending_entry_changed`、`alert_raised/resolved`、`command_audit_appended` 等事件）。对应的 `TradingFlowTraceReadModel` 也已补齐 trace 摘要状态、目录列表和详情关联事实，减少了前端直接拼多条后端读模型、探测隐式状态的边界泄漏；本次没有新增兼容别名路径，而是把缺失的查询/目录能力补成正式端口。

27. **QuantX 控制闭环已补上“统一动作结果 + 审计 ID + SSE 关联”第一版正式契约**  
    本轮进一步把 `POST /trade/control`、`POST /trade/runtime-mode`、`POST /trade/closeout-exposure` 从“只返回状态快照”升级为统一动作结果模型，正式返回 `accepted / status / action_id / audit_id / actor / reason / idempotency_key / recorded_at / effective_state`，并支持 `request_context`。后端会把同一 `action_id` 写入命令审计，再由 `/trade/state/stream` 的 `trade_control_changed / runtime_mode_changed / closeout_started / closeout_finished / command_audit_appended` 一并带出，前端不再需要靠时间接近性去猜“哪条状态变化对应刚才哪个按钮”。与此同时，`trade_control` 状态快照已优先走 live state 而非旧持久化快照，减少了控制后立刻读取 `/trade/state` 与 SSE 时状态源不一致的边界泄漏。

28. **QuantX 控制类 mutation 已补成真正的服务端幂等，而不再只是透传 `idempotency_key`**  
    本轮把 `POST /trade/control`、`POST /trade/runtime-mode`、`POST /trade/closeout-exposure` 的幂等能力从“请求/响应里有 `idempotency_key` 字段”推进到正式服务端语义：应用层新增独立的 operator action replay 服务，按 `command_type + idempotency_key` 收口短期内存去重，并在进程重启后回退到命令审计中查找最近一次已记录结果；同键同请求会直接回放原始动作结果，同键不同请求会返回显式冲突错误，而不是再次执行控制动作。这样前端的重试、双击和网络重放不再依赖时间接近性猜测，也避免把幂等逻辑散落在 3 条路由各自维护。当前实现没有引入兼容别名字段或第二套老路径，而是把 replay/冲突语义补成 `TradingCommandService` 背后的正式能力。

29. **QuantX 手动平仓/撤单 mutation 已接入同一套 operator action replay 边界**  
    本轮继续把 `POST /close`、`POST /close_all`、`POST /close/batch`、`POST /cancel_orders`、`POST /cancel_orders/batch` 从“原始 MT5 结果直出”升级为统一动作结果模型，正式返回 `accepted / status / action_id / audit_id / actor / reason / idempotency_key / recorded_at / effective_state`，并复用同一套 `command_type + idempotency_key` 回放/冲突拒绝语义。实现上没有再为这几条路由单独复制一套幂等逻辑，而是让 `TradingModule` 在 `_execute_command` 里直接产出 operator action response，并把结果同步写入 replay cache 与命令审计；同时，operator replay 指纹已去掉 `account_alias` 这类作用域内生字段，避免 API 请求体与审计载荷仅因账户上下文字段不同而被误判成冲突。这样前端控制台的手动平仓、批量平仓和撤单操作终于与控制类 mutation 落在同一条正式动作合同上，而不是继续保留“有些按钮可重试，有些按钮只能靠前端自己防重”的双轨语义。

30. **`pending-entry cancel` 与 `backtest/run` 已补进统一 action contract，而不再直出裸布尔/裸 job**  
    本轮继续把 `POST /monitoring/pending-entries/{signal_id}/cancel`、`POST /monitoring/pending-entries/cancel-by-symbol` 与 `POST /backtest/run` 纳入同一组执行类 mutation 语义。`pending-entry cancel` 现在改为正式请求体，支持 `reason / actor / idempotency_key / request_context`，并复用交易命令审计背后的 operator action replay 边界；同键同请求会直接回放原始取消结果，同键不同请求会显式冲突拒绝。`backtest/run` 则把动作状态拥有方收口到 `BacktestRuntimeStore`：同样支持 `actor / reason / idempotency_key / request_context`，返回统一的 `accepted / status / action_id / audit_id / message / effective_state`，并在 runtime store 内按 `command_type + idempotency_key` 做提交回放/冲突拒绝，避免网络重试或双击重复生成多条回测任务。与此同时，共享的 action contract helper 已从 `trade_routes/common.py` 抽到 `src/api/action_contracts.py`，后续再扩展其它危险操作时不需要继续复制一套字段归一化与 replay 响应拼装逻辑。

31. **单代码目录 + `config/instances/<instance>` 多实例配置与 `instance/supervisor` 入口已落地第一版**  
    配置底座已支持在共享 `config/*.ini` 之上叠加 `config/instances/<instance>/*.ini` 与实例级 `.local.ini`；`load_config_with_base()` / `get_merged_option_source()` 已能按实例名解析最终配置来源。新增 `src.entrypoint.instance` 与 `src.entrypoint.supervisor` 后，同一代码目录下可通过 `python -m src.entrypoint.instance --instance live-main` 启动单实例，也可通过 `python -m src.entrypoint.supervisor --environment live`（或 `--group live`）按 `config/topology.ini` 拉起 `main + workers` 进程组，不再依赖复制多份代码目录来承载多账户部署。

32. **账户自治风控已补齐正式当前态投影，`main` 只读聚合而不再以本地对象假装管理其他账户**  
    新增 `account_risk_state` 表与 `AccountRiskStateProjector`，由每个账户执行拥有者基于本地 `trade_control / circuit breaker / margin_guard / pending / positions / runtime_mode / quote freshness` 独立计算并写出当前风险态；`RuntimeReadModel` 与 `/trade/accounts` 现已优先读取正式投影，而不是探测当前进程里是否恰好持有某个本地风控对象。与此同时，executor 角色的 runtime registry 已停止启动共享采集、共享指标计算、信号运行时和 paper trading，只保留账户本地执行、持仓保护、风控投影与 trace，边界上正式收敛为“`main` 做共享计算，账户实例做本地执行与风控”。

33. **实例配置模型已进一步收口：只有 `mt5/market/risk` 允许实例级覆盖，其余配置保持共享事实源**  
    `config/instances/<instance>/` 现在不再被视为“什么都能放的第二套配置树”。配置加载层已显式限制只有 `mt5.ini`、`market.ini`、`risk.ini` 参与实例级合并；`app.ini`、`db.ini`、`signal.ini`、`topology.ini`、`economic.ini`、`ingest.ini`、`storage.ini`、`cache.ini` 等都保持根配置唯一事实源，即使实例目录下存在同名文件也不会被加载。这样可以避免实例配置再次演化成第二套系统，把角色/拓扑/策略/数据库等共享语义重新分叉到实例目录。

34. **多实例本地运行态与日志已按实例自动隔离，消除了共享 `data/` 污染风险**  
    之前多实例虽然已经共享同一代码目录，但 `health_monitor.db`、`signal_queue.db`、`events.db`、`mt5services.log` 等仍默认写到同一个 `data/` 与 `data/logs/`，存在运行态互相覆盖和日志混写风险。当前已改为命名实例自动隔离到 `data/runtime/<instance>/` 与 `data/logs/<instance>/`；`instance` / `supervisor` 启动下不再需要手工为每个实例额外改 `runtime_data_dir/log_dir`，也避免了多实例下本地 SQLite/WAL 与日志文件继续互相污染。

35. **入口日志现已补齐 `environment / instance / role` 上下文，多实例控制台输出不再完全不可判读**  
    `src.entrypoint.web` 与 `src.entrypoint.supervisor` 现已统一通过入口级 LogRecord context 注入，把 `environment / instance / role` 收口到每条日志记录，而不是只在启动首行打印实例名。这样多实例并行时，控制台输出与 `data/logs/<instance>/` 文件日志的内容语义保持一致；`main` 在 single-account 与 multi-account 下仍写入同一个 `data/logs/live-main/`，差异只体现在日志上下文和 topology，而不是路径漂移。

36. **账户风险投影中的 `quote_stale` 已从“API 级 1 秒 stale”收口为“执行侧行情失联”语义**  
    开盘实测中，XAUUSD 的真实 quote age 常落在 `1.1s ~ 1.9s`，而 `app.ini[limits].quote_stale_seconds = 1.0` 原本同时被 API 与 `AccountRiskStateProjector` 复用，导致 `account_risk_state.quote_stale` 在行情健康时也长期误报。当前已将 projector 改为显式使用执行侧阈值：至少 `3s`，并同时参考 `quote_stale_seconds` 与 `stream_interval_seconds` 的 3 倍；`trade/state` 中也新增 `metadata.quote_health.age_seconds / stale_threshold_seconds` 供巡检判读。这样 API 的紧阈值 stale 提示仍保留，但账户风控投影不再把正常开盘报价误标成风险盲区。

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
- 新增实例配置/入口相关定向回归已通过：
  - `tests/config/test_instance_config_overlay.py`
  - `tests/config/test_mt5_multi_account_config.py`
  - `tests/app_runtime/test_runtime_controls_account_topology.py`
  - `tests/trading/test_account_risk_projection.py`
- 回测结果口径已开始从单一“是否成交”改为显式区分“研究型回测”和“可执行性模拟”，后续 Paper Trading / Live Shadow 应继续沿用这套结果语义，而不是回到隐式兼容路径。
- QuantX 相关 API/readmodel/SSE 定向回归已通过 `42` 项，覆盖 `/signals/recent` 新分页契约、`/trade/command-audits` 过滤分页、`/trade/traces` 目录视图，以及 `/trade/state/stream` 的 `state_snapshot -> position_changed` 事件链。
- 控制闭环专项回归已进一步补到 `43` 项，新增覆盖 `trade/control`、`trade/runtime-mode`、`trade/closeout-exposure` 的统一动作结果契约，以及 `/trade/state/stream` 的 `trade_control_changed` 动作 ID 透传。
- 控制闭环与服务端幂等专项回归已通过 `53` 项，新增覆盖三类控制 mutation 的同键回放、冲突复用拒绝，以及 `TradingModule` 进程重启后基于命令审计的 operator action replay。
- 手动平仓/撤单扩展后的定向回归已通过 `74` 项，新增覆盖 `/close`、`/close_all`、`/close/batch`、`/cancel_orders`、`/cancel_orders/batch` 的统一动作结果契约、同键回放、冲突复用拒绝，以及 `TradingModule` 对手动平仓/撤单动作的内存回放与重启后审计回放。
- monitoring/backtest/trade 联合定向回归已通过 `90` 项，新增覆盖 `pending-entry cancel` 两条 mutation 的统一动作结果与冲突拒绝，以及 `/backtest/run` 的统一提交动作结果、同键回放、冲突复用拒绝与 `run_fields` 契约同步。
- `/v1/monitoring/runtime-tasks` 已收口为“默认当前实例作用域”，不再在 worker 侧混入其他实例或历史轮次的任务状态；当显式传入 `instance_id / instance_role / account_key / account_alias` 时，才切换到跨实例查询口径。

仍需单独关注但不属于本轮代码阻塞项：

1. **外部经济数据源仍可能超时**  
   真实启动时 Jin10 请求出现过 `read operation timed out`，这是外部依赖可用性问题，不是当前 schema/线程契约问题。

2. **市场数据延迟告警需要结合交易时段判断**  
   当前环境在监控中仍会报 `data latency critical`，更像是运行时没有收到足够新鲜行情或目标市场不活跃，需要结合 MT5 连接状态和交易时段继续看，不建议把它和本轮启动故障混为一类。

3. **`/trade/state/stream` 目前仍是轮询快照 diff，尚未具备服务端事件重放缓冲**  
   当前 SSE 已能满足前端状态面板、流水线追踪与告警订阅，但 `Last-Event-ID` 只会返回 `resync_required`，不会做真正的断线续传。若后续要承载更长连接时长、标签页切换恢复或多前端实例游标恢复，需要补服务端 replay buffer / cursor store，而不是继续让前端自己做事件补洞。

4. **危险操作的统一动作合同仍未覆盖到所有长耗时/批处理入口**  
   当前已经接入正式 action contract / replay 语义的包括 `trade/control`、`trade/runtime-mode`、`trade/closeout-exposure`、`close`、`close_all`、`close/batch`、`cancel_orders`、`cancel_orders/batch`、`pending-entry cancel`、`backtest/run`。但 `backtest/optimize`、`backtest/walk-forward` 等同样会触发后台长任务的 mutation 仍然沿用旧的“裸 job 提交”合同；如果后续前端继续扩展实验/回测控制台，这些入口也应复用同一套动作结果与幂等边界，而不是继续在不同子系统保留两套提交语义。

## 1. 总体结论

当前系统已经完成从 legacy 策略到结构化策略的主体迁移，领域目录、运行时装配、信号链路、交易执行、持久化、回测与研究系统都已形成清晰分层。主要问题不在“缺模块”，而在迁移后的工程一致性：

1. **本机 local 配置会优先生效，当前已不再保留 legacy 投票组，但仍会改变结构化策略有效集合**。
2. **部分长期运行组件的 stop/start 生命周期保护不一致**，线程 join 超时后仍清空线程引用，存在重复启动后台线程的风险。
3. **装配层和 API/展示层仍访问若干私有属性/方法**，说明正式端口还不完整。
4. **千行级协调器仍然集中多种职责**，后续性能与并发问题会优先在这些文件中出现。
5. **策略有效性仍处在验证阶段**，当前样本量不足以支持实盘放大或过度调参。

---

## 2. P0 风险

### 2.1 `signal.local.ini` 仍是高优先级事实源，但结构化策略冻结方式已切换到正式部署合同

**当前状态**：
- 当前代码注册策略仍是 8 个结构化策略实例。
- `config/signal.local.ini` 依旧是高优先级事实源，仍可能改变本机有效策略集合。
- 但“全部 `regime_affinity = 0` 即冻结”的隐式语义已退役，当前要求通过 `[strategy_deployment.<strategy>]` 显式声明 `candidate / paper_only / active_guarded / active`。
- `structured_session_breakout` 与 `structured_lowbar_entry` 这类单 TF 候选，现已迁移为 `strategy_deployment` 合同，带 `locked_timeframes / locked_sessions / require_pending_entry / max_live_positions` 等护栏。

**影响**：
- local 覆盖不会出现在仓库默认配置中，但会直接改变本机运行时的有效策略集合。
- 当前缺少“启动摘要/状态端点”来明确标出哪些策略被 local 配置冻结，人工排查时容易把“无交易”误判成策略逻辑问题。

**剩余建议**：
- 在启动阶段输出 effective strategy summary：启用 TF、部署状态、是否 guarded、是否支持 intrabar。
- 对 ignored local 配置提供只读诊断端点或 preflight 检查，避免“本机覆盖改变行为但 API 不可见”。
- 若策略长期停留在 `candidate / paper_only`，需要补对应的研究 provenance、回测与 paper 证据，而不是把状态长期留在 local 文件里。

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

### 4.2 单 TF 策略已进入正式 guarded 轨道，但研究闭环仍需继续补齐

当前 `tf_specific` 策略已经有正式部署合同与 live 护栏，避免了过去依赖 zero-affinity freeze 的隐式语义；但 research → candidate → structured strategy → backtest/WF/paper/live 的闭环仍处于第一阶段，尤其还缺：

- 候选结果到正式结构化策略实现的批量晋升流程
- 启动摘要中对 `candidate / paper_only / active_guarded` 的显式可视化
- 基于真实 paper session 的自动晋升/降级台账

**建议**：
- 增加策略运行态摘要：`enabled_timeframes`、`deployment_status`、`locked_sessions`、`is_guarded`、`in_voting_group`。
- Paper Trading/Backtest 输出中记录 active strategy set 和 `research_provenance` 配置快照。
- 继续把单 TF 候选的纸面证据转成可执行策略，而不是长期停留在“被识别但未落地”的状态。

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

33. **`live-main` 单实例 smoke 已打通，启动期配置空值、schema 时序和风险投影 JSON 缺陷已修复**  
    本轮按 `python -m src.entrypoint.instance --instance live-main` 做了真实单实例烟测，并在 smoke 期间显式关闭自动交易，验证 `/health=200` 与 `/v1/monitoring/health/ready=ready`。过程中修复了三类真实启动问题：  
    1) `src/config/signal.py` 现在会把直接映射到 `SignalConfig` 的空字符串配置视为“未覆盖”，不再因 `signal.ini` 中留空占位字段触发 `ValueError`/Pydantic 校验失败；  
    2) `StorageWriter` 新增正式 `ensure_schema_ready()` 入口，`AppRuntime.start()` 会在 warm-start 之前先确保 schema 与 retention 就绪，避免新库首次启动时 `position_runtime_states / trade_control_state` 查询先于建表；  
    3) `AccountRiskStateProjector` 已对非有限浮点做 JSON 清洗，不再把 `Infinity` 写入 `account_risk_state.metadata` 导致 PostgreSQL JSON 解析失败。  
    当前单实例启动已可进入 `ready`，残余告警主要是数据陈旧导致的 `market_data data latency critical`，属于环境/时段问题而非装配阻塞。

34. **`/v1/trade/accounts` 已补齐正式读模型端口，休盘 smoke 下账户视图不再因私有字段访问而 500**  
    本轮休盘巡检中，`/health`、`ready`、queues、performance、events 均已打通，唯一暴露的真实断点是 `/v1/trade/accounts` 仍直接访问 `RuntimeReadModel.runtime_identity`，而读模型内部只持有私有 `_runtime_identity`，导致账户视图在 live-main 单实例烟测时返回 500。现已在 `RuntimeReadModel` 上补齐正式公开属性 `runtime_identity`，让 `trade/accounts`、风险投影聚合和环境标签统一通过公开只读端口获取，避免 API 为了取状态继续探测私有字段；同时补充了对应 API 回归测试，覆盖新实例配置模型下的账户清单返回。

35. **交易控制烟测已打通：命令审计、状态回放与文本日志三条链路现已一致可追踪**  
    本轮按 `live-main` 单实例、显式不下真实订单的方式对交易控制面做了休盘烟测，实际调用了 `/v1/trade/control` 的安全开关操作，并核对了 `/v1/trade/command-audits` 与 `data/logs/live-main/mt5services.log`。过程中修复了两个真实断点：  
    1) `src/persistence/repositories/trade_repo.py` 中 `write_trade_command_audits()` 的批量写库字段顺序与 `trade_command_audits` 表契约不一致，导致 `account_key` 被错误写入 JSON 列，控制命令虽然返回成功但审计表为空；现已按正式 schema 顺序重排，并补了仓储回归测试。  
    2) `RuntimeReadModel.persisted_trade_control_payload()` 直接返回数据库原始字典，`updated_at` 等 `datetime` 值未做 JSON-safe 归一，导致 `/v1/trade/control` 在持久化状态存在时触发响应验证失败；现已在读模型层统一做递归序列化。  
    同时在 `src.trading.application.module` 中补了正式文本日志：每次 operator action 成功入审计后都会记录 `account / command / status / action_id / actor / reason / idempotency_key`，避免后续只能依赖数据库追查。当前已验证安全开关操作会同时落到 API 状态、审计表与文本日志，说明交易控制主链路的可追踪性已恢复。

36. **`trade/precheck` 准入链已修复账户信息契约缺口，休盘下可返回结构化风险阻断而非 AttributeError**  
    在继续扩大 `live-main` 单实例休盘烟测时，`POST /v1/trade/precheck` 暴露出一个真实契约断点：`src.risk.rules` 的日内亏损规则会读取 `day_start_balance / daily_realized_pnl / daily_pnl`，而 `src.clients.mt5_account.AccountInfo` 适配对象并未暴露这些字段，导致准入接口直接抛出 `AttributeError`，无法返回结构化风险评估。该问题现已通过收口账户信息契约修复：`AccountInfo` 正式补齐上述可空字段，并由 MT5 账户适配层统一返回 `None` 作为“当前 broker 不提供该字段”的显式语义，而不是让下游规则依赖隐式属性探测。修复后已验证 `/v1/trade/precheck` 能在休盘与低保证金场景下稳定返回结构化 `block` 结果，正确给出 `margin_availability` 与 `market_order_protection` 阻断原因，同时文本日志中也会记录对应的风险阻断告警。

37. **economic calendar 健康语义已从“无 last_refresh_at 即 stale”收口为正式启动引导状态，重启后也会恢复上次成功刷新时间**  
    本轮针对休盘 smoke 中出现的 `economic calendar stale` 与 Jin10 TLS 握手超时告警做了根因收口。问题不在风控侧，而在日历服务自身的健康模型：`EconomicCalendarService.is_stale()` 以前只要 `_last_refresh_at is None` 就直接返回 `True`，而 `start_service()` 又会把首次 `calendar_sync` 延后 `startup_calendar_sync_delay_seconds` 执行；同时 `restore_job_state()` 只恢复 job_state，不恢复 `_last_refresh_at`。结果是服务刚启动或刚重启，即使还处于计划中的 bootstrap 窗口，也会被错误标成 stale，并进一步让监控与 Trade Guard 看到“日历健康退化”。现已做三项正式修复：  
    1) `EconomicCalendarService` 新增 `warming_up / staleness_seconds / health_state` 正式健康语义，首次成功刷新前仅在 bootstrap 截止时间之后才视为 stale；  
    2) `restore_job_state()` 现在会恢复最近一次成功刷新的 `_last_refresh_at`，以及最近一次尝试的时间、状态和错误，避免重启后状态丢失；  
    3) 监控侧 `check_economic_calendar()` 改为消费 `staleness_seconds`，在 warming_up 阶段不再把 staleness 记为 `inf`，并补充了经济日历刷新成功日志，便于区分“外部瞬时抖动已恢复”与“持续同步失败”。  
    这次修复保持了边界正确：Trade Guard 继续只读取经济日历正式健康状态，不在风控规则里增加启动期特判。

38. **economic calendar runtime task 持久化已按实例新契约收口，shutdown 不再覆盖最后一次成功刷新结果**  
    在进一步做“停机重启后立即恢复 economic status”的真实 smoke 时，又暴露出第二层设计偏差：economic calendar 的 `runtime_task_row()` 仍按旧 13 列写 `runtime_task_status`，而仓储层已经升级到 17 列实例契约（`instance_id / instance_role / account_key / account_alias`），导致 `persist_job_state()` 实际写库失败但被内部 debug 吃掉；同时 `stop_service()` 会把 `last_status` 改成 `stopped` 并覆盖同一 runtime task 行，使得恢复逻辑即便读到记录，也会把“最后一次成功刷新”误读成 `stopped`。现已收口为：  
    1) `EconomicCalendarService` 正式注入 `runtime_identity`，runtime task row 按实例新契约完整写入；  
    2) `runtime_task_status` 查询支持按 `instance_role/account_key` 过滤，并按 `updated_at desc` 取最新，避免多实例或多轮重启的旧行干扰恢复；  
    3) economic calendar task details 新增 `last_result_status`，shutdown 只更新当前任务状态为 `stopped`，不再抹掉最近一次真实刷新结果；恢复时优先用 `last_result_status + success_count` 还原上次成功刷新时间。  
    经过真实 `live-main` 重启烟测验证，服务刚进入 ready 时即使 `release_watch` 正在新一轮刷新，`/v1/economic/calendar/status` 也已经能恢复出上一轮 `last_refresh_at` 与 `last_refresh_status=ok`，不再出现重启后全部为 `null` 的假 stale 状态。

39. **`/signals/evaluate` 的持久化载荷已收口为 JSON-safe 契约，不再因 metadata 中的 `datetime` 卡死请求**  
    在本轮单实例连通性测试中，`POST /v1/signals/evaluate` 会命中真实持久化路径，而 `SignalModule._persist_signal()` 直接把 context metadata 与 indicator snapshot 原样塞进 `SignalRecord`。当 metadata 中包含 `bar_time / recent_bars[].time` 这类 `datetime` 时，`psycopg2` 在写 `signal_events` 的 JSON 列时会抛 `TypeError: Object of type datetime is not JSON serializable`，接口表现为超时，日志中持续出现 `Failed to persist signal event`。本轮已把“信号持久化载荷必须是 JSON-safe”正式收口到 `SignalRecord.to_row()`：统一递归序列化 `datetime -> ISO8601`、`tuple -> list`、非有限浮点 -> `null`，避免再让 API、runtime 或研究链路各自猜测何时该做 JSON 归一。同时补了 `tests/signals/test_signal_module.py` 回归，明确验证带 `datetime` 的 metadata 能稳定持久化。

40. **`SignalModule` 已在编排边界统一收口信号身份字段，避免策略空串污染 `signal_events` 与 recent/summary 读口**  
    在继续做 `data -> indicators -> signals -> trade` 单实例连通性测试时，又发现 `/signals/evaluate` 虽然已经能稳定持久化，但结构化策略返回的 `SignalDecision.symbol/timeframe` 仍然是空串，导致 `signal_events`、`/signals/recent`、`/signals/summary` 被写入无符号、无周期的事实行。这不是 API 载荷问题，而是结构化策略基类长期把 `SignalDecision` 的身份字段留空，信号编排层也没有在出边界前做统一修正。该问题现已在 `SignalModule.evaluate()` 收口：策略返回后立即以当前请求的 `strategy/symbol/timeframe` 重建正式决策对象，再进入后续置信度修正与持久化流程。这样职责边界更清晰：策略只回答“方向/置信度/原因”，身份字段由编排层统一拥有，避免各策略重复样板代码，也避免 `recent/summary/trade_from_signal` 继续消费被污染的事实。已补 `tests/signals/test_signal_module.py` 回归，明确验证即便策略返回空身份字段，持久化后的 `signal_events` 也会写入正确的 `symbol/timeframe`。

41. **`/v1/trade/dispatch` 已在 API 边界统一归一化交易操作契约，不再让调用方猜测 `submit_trade/trade` 与 `direction/side` 双轨语义**  
    在单实例连通性烟测中，`/v1/trade/dispatch` 暴露出一条典型的接口契约漂移：监控读口会提示使用 `operation=trade`，但历史调用经常仍发 `submit_trade`；同时 payload 中 `direction` 在 `/trade` 主入口可接受，经由 dispatch 走统一调度时却会因底层只认 `side` 而失败。该问题现已在 `TradeAPIDispatcher` 的 API 边界正式收口：`submit_trade / execute_trade` 统一映射到正式操作 `trade`，`precheck_trade` 统一映射到 `trade_precheck`，并且凡是交易类 payload 都先通过 `TradeRequest` 做标准化，再交给 `TradingCommandService.dispatch_operation()`。这样下游只需要面对一套正式载荷，旧别名也不会继续把“语义转换”散落到命令服务内部。对应回归已补到 `tests/api/test_trade_api.py`，明确验证 `submit_trade + direction` 会被稳定归一到正式 `trade + side` 契约。

42. **`/v1/signals/runtime/status` 已补齐执行闸门与过滤摘要，单实例巡检不再只能看到“运行中”而看不到“为什么会放行/过滤”**  
    休盘连通性测试显示 `SignalRuntime` 内部其实已经维护了 `filter_realtime_status / filter_by_scope / filter_window_by_scope`，`TradeExecutor.status()` 也已经有 `execution_gate`，但 `RuntimeReadModel.signal_runtime_summary()` 长期只投影了队列和 warmup 基础状态，导致 `/v1/signals/runtime/status` 返回里 `executor_enabled / execution_gate / active_filters / filter_stats` 都是空值。这个缺口会直接削弱交易链路可审查性：看到 `signal_runtime.running=true` 并不能回答“当前有哪些过滤器启用、执行闸门是否打开、最近是被什么过滤掉的”。本轮已在读模型层把这部分正式投影补齐，统一公开当前执行器启用状态、执行闸门配置、活跃过滤器列表，以及 confirmed/intrabar 两个 scope 的过滤累计/窗口统计。这样单实例 smoke 下即可直接回答“策略在跑，但当前是 session/economic/spread 等哪些门在起作用”，不再依赖读代码或翻日志推断。

43. **`/v1/monitoring/health/ready` 的巡检契约已升级为结构化对象，runbook 已同步修正判读口径**  
    最新单实例 smoke 中，`/v1/monitoring/health/ready` 已返回结构化对象 `{status, checks, startup_phase, timestamp}`，而不是旧 runbook 中记载的裸字符串 `ready`。如果巡检脚本仍按字符串比较，就会误判服务“未就绪”，即使底层 `storage_writer / ingestion / indicator_engine` 都已经达标。本轮已把 runbook 的 ready 判读规则同步修正为：以 `status=ready` 和 `checks.*=ok` 为唯一准则，不再依赖历史的字符串返回语义。这个修正不改变接口实现，但能避免后续运维脚本和 smoke 流程继续按过期契约误报。

44. **双实例 smoke 已打通 `main + executor` 的就绪探针、风险投影与审计链，但 executor 角色边界仍有待继续收紧**  
    本轮按 `python -m src.entrypoint.supervisor --environment live` 对 `live-main + live-exec-a` 做了真实双实例烟测，确认 `8808/8809` 两个实例都能进入 `ready`，并且 `worker` 本地执行控制操作会同时写入 `trade_command_audits`、`account_risk_state` 和 `data/logs/live-exec-a/mt5services.log`，`main` 侧的 `/v1/trade/accounts` 也能正确聚合看到 `live_exec_a` 的最新风险当前态。过程中修复了一个真实观测断点：`/v1/monitoring/health/ready` 过去始终按主实例口径检查 `ingestion + indicator_engine`，导致 executor 明明已经装配好 `PendingEntryManager / PositionManager / AccountRiskStateProjector`，探针仍会返回 503；现已改为按角色判定，executor 的 ready 口径收口为 `storage_writer + pending_entry + position_manager + account_risk_state`。同时，`RuntimeReadModel` 对 executor 的 `storage / indicators / signals` 视图也已改成 `disabled` 语义，避免 `/health` 把“本来就不属于 worker 的共享计算面”误报成 critical；`paper_trading` 也已在 executor 装配阶段直接跳过。  
    但这轮 smoke 也暴露出一个仍待整改的结构性问题：executor 在 build 阶段仍会完整构建 `UnifiedIndicatorManager`、signal factory 与 economic calendar 相关组件，然后仅在 runtime mode 阶段停止其中一部分线程。这说明当前“账户执行面”和“共享计算面”在装配边界上还没有彻底拆干净；本轮只修正了观测契约和非必要的 `paper_trading` 装配，使双实例 smoke 可以成立，但后续仍应把 worker 收口到真正的 `AccountRuntime`，不再在 build 阶段构造不属于它的 shared compute 依赖。

45. **executor 装配边界已前移到 composition root，不再靠 runtime stage 事后停线程**  
    针对上一条暴露出的结构性问题，本轮已把 `executor` 的运行时装配改为真正的角色化组合，而不是“先全量构造，再在 lifecycle 中禁用”。`build_app_container()` 现在会在进入 builder phase 前先识别 `instance_role`：`main` 仍构造 shared compute 路径（`ingestion / UnifiedIndicatorManager / SignalRuntime / economic calendar sync / paper trading`），而 `executor` 只构造本地 `AccountRuntime`，不会再在 build 阶段创建上述 shared compute 组件。与此同时，`build_market_layer()` 已支持 `include_ingestion / include_indicators`，`build_trading_layer()` 已支持 `enable_calendar_sync`，并新增 `build_account_runtime_layer()` 作为 executor/main-account 的统一账户执行装配入口。`runtime_controls` 也同步从“按角色特判”收口为“按组件存在性启停”，避免未来继续把边界错误藏在 lifecycle 分支里。

46. **executor 的指标与经济日历依赖已改为只读适配，不再反向带入 shared compute 堆栈**  
    为了让 `AccountRuntime` 在没有 `UnifiedIndicatorManager` 和本地日历同步线程的情况下仍然具备持仓管理与风控输入，本轮新增了两个正式只读端口：`ConfirmedIndicatorSource` 与 `ReadOnlyEconomicCalendarProvider`。前者直接基于共享库中的确认 OHLC/indicators 快照为 `PositionManager` 提供最新确认指标，后者则只通过 DB 与 `runtime_task_status` 读取 economic calendar 的共享结果，而不会在 executor 本地启动同步线程。这样 executor 既能继续使用 confirmed 指标和经济事件作为本地风控输入，又不会在 build 阶段重新拥有 shared compute 的运行时职责。已新增 composition-root 级测试，直接验证 executor builder 不再调用 `build_signal_layer / build_paper_trading_layer`，而是只走 `build_account_runtime_layer`。

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
35. 2026-04-13：自动执行资格从“隐式默认放行”改为“显式合同 + 显式绑定”。`signal.ini` 基线默认 `auto_trade_enabled=false`；每个注册策略都必须显式声明 `[strategy_deployment.<name>]`，否则启动失败；`ExecutionIntentPublisher` 不再对 single-account 做“空绑定自动路由当前账户”，且只为 `allows_live_execution()` 的策略发布 intent。未决项：后续仍需收口 economic decay / confidence floor 与 signal filter / trade guard 双口径问题。
36. 2026-04-13：经济事件渐进降权不再被 `confidence_floor` 回抬。`apply_confidence_adjustments()` 现在一旦命中 `economic_event_decay < 1.0`，后续不再施加底线保护，避免事件窗口里的压制信号被重新抬成可执行候选。未决项：signal filter 窗口与最终 trade guard 窗口仍是双口径，需要进一步统一或显式区分。
37. 2026-04-13：executor 的 storage/indicator 读模型语义已改成正式角色语义，不再把“没有 ingestor / indicator_manager”误报为 critical。`RuntimeReadModel` 现在显式接收 `storage_writer`，executor 的 `storage_summary()` 直接基于 writer 线程和队列状态生成摘要，并把 `ingestion=disabled` 作为正式语义返回；`indicator_summary()` 也在 executor 上直接返回 `disabled`，避免 `/health` 和 `/v1/monitoring/health/ready` 继续把不属于账户执行面的共享计算模块误判为故障。未决项：`trade_executor.enabled` 目前仍混合了“执行器运行中”和“自动交易门打开”两个语义，后续应继续拆开。
38. 2026-04-13：`account_risk_state_projector` 的组件注册顺序缺陷已修复，worker 风险当前态现在会在双实例拓扑中稳定落库并被 main 聚合。根因是 `build_runtime_controls()` 之前先构建了 `runtime_component_registry`，而 `account_risk_state_projector` 在 registry 之后才创建，导致 registry 中该组件的 `supported_modes` 永远为空，worker 即使进入 `full` 模式也不会启动本地风险投影。现已把 projector 的创建与 hook 绑定前移到 registry 之前，再构建 component registry，使 `account_risk_state_projection` 成为真正的账户执行面组件。双实例 smoke 已验证：`live-exec-a` 的 `/v1/monitoring/health/ready` 返回 `account_risk_state=ok`，`/v1/trade/accounts` 可从 main 和 worker 两侧同时聚合看到 `live_exec_a` 的最新 `risk_state`。未决项：`/v1/monitoring/runtime-tasks` 目前仍是全局查询口径，worker 侧会看到共享任务历史，后续应按实例默认过滤。
47. 2026-04-13：signal-domain 的经济事件语义已收口到 `economic.ini / EconomicConfig`，不再让 `signal.ini` 和 runtime evaluator 各自维护一套窗口常量。此前 signal filter 使用 `signal.ini` 里的 `economic_*` 键，而 confidence decay 又在 `runtime_evaluator` 内部硬编码 `-20m / +2h` 查询窗口和固定阶梯衰减，形成两套隐藏语义。本轮新增 `SignalEconomicPolicy`，由 `economic.ini` 的 `pre_event_buffer_minutes / post_event_buffer_minutes / high_importance_threshold / release_watch_*` 派生 signal filter 的硬阻断窗口与 confidence decay 的预热窗口；`EconomicEventFilter` 和 `runtime_evaluator` 统一消费这份 policy，不再各自维护 lookahead/lookback/importance 配置。同时，`signal.ini` 中原有的 `economic_filter_enabled / economic_lookahead_minutes / economic_lookback_minutes / economic_importance_min` 已删除，`get_signal_config()` 会对任何遗留 local 覆盖 fail-fast，强制把 signal-domain 经济事件语义收口到 `economic.ini`。按用户确认，账户侧 `trade_guard_calendar_health_mode` 仍保持 `warn_only`，本轮未改变 fail-open/fail-closed 策略。
48. 2026-04-13：deployment 合同已下沉到执行边界复核，`locked_timeframes / locked_sessions` 不再只依赖上游 signal 编排保证。此前 `StrategyDeployment` 虽然正式承载了 `locked_timeframes / locked_sessions`，并由 `SignalPolicy` 注入到策略路由，但 `run_pre_trade_filters()` 在真正执行前只检查 `status / min_final_confidence / max_live_positions / require_pending_entry`，导致 replay、手工注入 signal 或未来跨入口复用事件时，合同约束可能失效。本轮已在执行前过滤链新增 `strategy_locked_timeframe / strategy_locked_session` 两个正式拒绝原因，并使用 `SignalEvent.timeframe` 与 `metadata.session_buckets`（缺失时回退到 `bar_time / generated_at` 推导）做复核。这样 deployment 合同从“上游约定”升级成“执行前强校验”，账户执行面即使接收到外部注入的 signal，也会在本地拒绝合同外时段/周期的交易。
49. 2026-04-13：`/v1/monitoring/health/ready` 当前已验证是“进程/组件就绪”语义，而不是“市场数据已新鲜”语义。最新 `live-main` 单实例 smoke 中，服务在休盘和历史 OHLC 明显陈旧的情况下仍然快速返回 `ready`，同时日志持续出现 `market_data data latency critical` 和 `Warmup skip (stale bar_time)`。这说明当前 ready 探针只要求 `storage_writer / ingestion / indicator_engine` 进入运行态，不会因为休盘或 quote/bar 新鲜度不足而降级；运维上必须把 `ready` 与 `market_data` 告警分开判读，不能把 `ready=true` 误认为“当前已具备实时交易条件”。
50. 2026-04-13：账户风险当前态里的 `quote_stale` 目前只作为可观测 flag，不参与 `should_block_new_trades` 的最终判定。`AccountRiskStateProjector` 会在本地风险投影中标记 `active_risk_flags += quote_stale`，但 `should_block_new_trades` 只受 `runtime_mode / close_only / circuit_breaker / margin_guard` 影响，不会因为 quote 过期而自动阻断新交易。最新单实例 smoke 已验证：在 `auto_entry_enabled=true`、`close_only_mode=false` 的情况下，账户风险当前态仍会是 `quote_stale=true` 且 `should_block_new_trades=false`。这在休盘/断线/MT5 quote 陈旧场景下属于交易语义未决项，后续需要明确是否应将 quote freshness 上升为账户级硬阻断条件。
51. 2026-04-13：`paper_trading` 的正式职责已重新澄清为 `main` 上的策略验证 sidecar，而不是 demo 专属能力。此前把它从 live main 默认运行态里移除，是把“不要混淆真实账户执行”和“不要装配验证能力”混为一谈，方向过度收缩。现已恢复为：`paper_trading` 只要启用就装配在 `main`，用于 shadow execution / 策略验证；`executor` 仍然严格禁止装配。这样边界更符合真实职责：`main` 拥有共享计算与验证 sidecar，`worker` 只拥有账户本地执行与风控。后续真正需要补的是 observability 语义，把 `paper_trading` 明确标成验证支路，而不是继续从 live main 里移除它。
52. 2026-04-13：`paper_trading` 的可观测语义已从“普通 runtime component”正式收口为 `validation_sidecar`。此前虽然 `main` 上已经恢复装配 `paper_trading`，但 `/health`、`/v1/trade/state` 和 runtime 读模型仍只把它混在普通 `components` 视图里，容易把“策略验证 sidecar 正在运行”和“真实账户执行正在运行”混看。本轮已在 `RuntimeReadModel` 中新增 `paper_trading_summary()` 正式投影，并将其暴露到 `runtime_mode.validation_sidecars.paper_trading`、`dashboard_overview.validation.paper_trading`、`trade_state.validation.paper_trading` 以及根 `/health` 的 `runtime.validation_sidecars.paper_trading`。这样既保留 `runtime_mode.components.paper_trading=true` 作为“组件已装配”的事实，又能明确标注其 `kind=validation_sidecar`、`running/status/session_id/signals_received/signals_executed/signals_rejected` 等验证支路状态。最新单实例 smoke 已验证：`live-main` 的 `/v1/monitoring/health/ready` 正常返回 `ready`，同时 `/health` 与 `/v1/trade/state` 都会把 `paper_trading` 标成 `validation_sidecar`，不再与真实账户执行语义混淆。
53. 2026-04-13：`/v1/trade/dispatch` 的交易载荷归一化已补齐最后一处语义泄漏，`submit_trade + direction` 现在能在开盘 canary 中稳定进入真实 dry-run 执行链。此前 `TradeAPIDispatcher` 虽然会用 `TradeRequest` 解析 `direction -> side`，但 `model_dump()` 仍把原始 `direction` 一起带给 `TradingCommandService.execute_trade()`，导致开盘实测中 `dispatch` 在 API 边界之后仍抛出 `unexpected keyword argument 'direction'`，形成“precheck 已 allow、dispatch 却死于接口契约”的假断链。本轮已在 API 调度器里把归一化后的 payload 显式移除 `direction`，只保留下游正式契约 `side/sl/tp/...`；并补回归测试，明确验证 `submit_trade + direction` 最终只会向下游传 `side`。最新开盘单实例 canary 已验证：`precheck` 返回 `allow`，`dispatch(dry_run)` 返回 `success=true`，`requested_operation=submit_trade` 与 `operation=trade` 同时可见，`trade_command_audits` 里也能看到对应的 `precheck_trade / execute_trade` 记录。
54. 2026-04-13：intrabar 主链当前暴露的是**配置闭环缺失**，而不是引擎全断。最新开盘单实例验证中，`/v1/signals/diagnostics/pipeline-trace?scope=intrabar` 已出现 `XAUUSD/H1` 的 `bar_closed -> indicator_computed -> snapshot_published`，`/v1/ohlc/intrabar/series?timeframe=H1` 与 `/v1/indicators/XAUUSD/H1/live` 也都能返回实时快照，说明 intrabar 基础链路本身是工作的；但 `M30/M15/M5` 仍完全无 intrabar 数据。根因有两层：  
    1) `intrabar_trading.trigger` 当前要求 `M30/M15/M5 <- M1`，而共享 `app.ini[trading].timeframes` 仍是 `M5,M15,M30,H1,H4,D1`，并不包含 `M1`，因此这些父级 TF 的 child close 事件在当前有效配置下根本不可能产生；  
    2) 试图通过 `config/instances/live-main/app.local.ini` 临时把 `M1` 只加到单实例，并不会生效，因为配置系统当前只允许 `market.ini / mt5.ini / risk.ini` 参与实例级 overlay，`app.ini` 仍是全局共享事实源。  
    这意味着 intrabar 当前的真实风险点不是“线程没启动”，而是系统缺少对 `trigger_map` 与有效 timeframes 闭环的一致性校验。后续应在启动期对 `intrabar_trading.trigger` 与 `TradingConfig.timeframes` 做 fail-fast 校验，或明确禁止配置出“父级启用但 child TF 不存在”的无效组合。
55. 2026-04-13：closeout 控制链已按 no-op 业务场景验证通过，`API -> 应用服务 -> 状态投影 -> 审计 -> 文本日志` 语义一致。最新验证中，在无持仓、无挂单的 live-main 场景下调用 `/v1/trade/closeout-exposure`，返回 `accepted=true / status=completed`，并明确给出 `remaining_positions=[] / remaining_orders=[]`；`/v1/trade/state/closeout` 能同步看到 `last_reason / last_comment / actor / action_id / audit_id / idempotency_key`，`/v1/trade/command-audits` 也记录了完整的 `closeout_exposure` 请求/响应载荷，主日志中对应的 `Operator action recorded` 也存在。这说明 closeout 链在“没有可平仓对象”时不会卡在中间态，也不会出现 API 成功但状态/审计不一致的问题。未决项仅剩真实持仓/挂单场景下的部分成功、部分失败和 `runtime_mode_after_manual_closeout` 行为验证。
56. 2026-04-13：intrabar trigger 与有效时间框架的闭环校验已从 warning-only 升级为 startup/hot-reload fail-fast。此前 `build_signal_layer()` 只会对缺失 trigger map 发 warning，导致系统即使在 `intrabar_trading.enabled=true`、`enabled_strategies` 已声明、但 child timeframe 根本不在 `app.ini[trading].timeframes` 中时仍然进入 `ready`，运行后只表现为某些父级 TF 永远没有 intrabar 数据。这轮已把校验前移到信号装配边界，并在 `signal.ini` 热重载时复用同一份合同校验：对每个实际启用的 intrabar 策略，都会验证其活动父级 timeframe 是否存在 trigger 映射，以及该 child timeframe 是否属于全局有效 `trading.timeframes`。若不满足，启动直接失败，热重载也拒绝应用新配置。这样系统不再允许“intrabar 父级启用但 child TF 不在有效时间框架集合里”的静默坏配置继续带病运行。
57. 2026-04-13：`/v1/trade/precheck` 与 `/v1/trade/dispatch` 已开始收口到统一 `AdmissionReport`，不再由各入口各自拼装执行原因。本轮新增 `src.trading.admission.TradeAdmissionService`，把 `precheck_trade` 的账户风控结果、运行时 `tradability`、账户风险当前态和 trade control 统一归并成正式准入报告，并在 API 层作为 `/v1/trade/precheck` 的直接返回模型，以及 `/v1/trade/dispatch` 的附属字段返回。当前 `AdmissionReport` 已正式包含 `decision / stage / reasons / economic_guard / quote_health / trade_control / margin_guard / position_limits / trace_id` 等字段，后续 `ExecutionIntentConsumer`、intrabar 执行协调器与其他执行入口将复用同一服务，避免“同一笔交易不同入口给出不同阻断原因”的语义分裂。
58. 2026-04-13：交易链路 trace 已升级到 admission 级业务解释视图，不再只能看到技术事件时间线。`pipeline_trace_events` 早已具备 `admission_report_appended` 事件底座，但 `TradingFlowTraceReadModel` 过去不会把这类事件提升成正式事实，导致 `/v1/trade/traces` 只能看到 pipeline timeline，不能直接回答“这条信号在准入阶段发生了什么”。本轮已把 admission 事件收口进 `facts.admission_reports` 与 `summary.admission`，并同步补齐 `pipeline_admission` 阶段与 trace list 摘要中的最新 admission 决策/阶段。这样 trace 读模型现在可以直接回答：该链路是否生成过 AdmissionReport、最新决策是 `allow/warn/block`、发生在哪个阶段，而不是要求运维再回头手工拼装 pipeline event payload。
59. 2026-04-13：`/v1/trade/state/stream` 已开始直接消费正式 `pipeline_trace_events` 事实源，而不再只依赖本地状态 diff 进行近似推送。此前 SSE 流只能比较 `trade_control / positions / pending / alerts` 等 snapshot 差异，无法感知 `admission_report_appended / command_completed / risk_state_changed` 这类正式业务事件，导致前端和巡检侧仍需轮询 `/v1/trade/traces` 才能知道执行面发生了什么。本轮已在 `RuntimeReadModel` 新增 `recent_trade_pipeline_events_payload()`，按当前 `instance_id + account_key` 过滤正式 pipeline 事件，并在 `/v1/trade/state/stream` 的 snapshot 与增量 diff 中同步透出。这样 SSE 现在既能继续推送传统状态变化，也能直接推送 admission/intent/command/risk/unmanaged-position 关键事件，状态流开始和 trace 事实源对齐。未决项：`ExecutionIntentConsumer` 与 operator command 消费器侧的事件覆盖还需继续补齐，才能让 stream 看到完整的 intent/command 生命周期。
60. 2026-04-13：`TradeExecutor` 已在 confirmed / intrabar 执行边界正式产出 `admission_report_appended`，执行 runtime 的放行/阻断开始和 API 准入语义对齐。此前 `AdmissionReport` 只在 `/v1/trade/precheck` 与 `/v1/trade/dispatch` 入口生成，confirmed 信号和 intrabar 预览在本地执行面被挡住时，只会发 `execution_blocked` 或写 skip reason，导致“API 看得到准入报告，执行 runtime 看不到” 的语义分叉。本轮已把 admission 事件下沉到执行边界：confirmed 流在全部 pre-trade checks 通过后，会先发 `decision=allow` 的 admission 事件再进入 execution_decided/submitted；confirmed/intrabar 在 `reject_signal()`、intrabar guard 缺失、intrabar gate/cost block 等场景下，也都会正式发出 `decision=block` 的 admission 事件。这样 trace、SSE 和执行日志现在能共同回答“执行面为什么挡住了这条信号”，而不再只在 API 层可解释。
61. 2026-04-13：`src.monitoring.pipeline` 的公共导出已补齐 admission / intent / command / risk / unmanaged-position 常量，避免下游测试和读模型继续绕过正式端口直接引用内部 events 模块。此前新增事件类型虽然已经存在于 `src.monitoring.pipeline.events`，但 `__init__` 仍只导出早期的 execution 类常量，导致执行面测试与潜在调用方若想消费 admission/intent/command/risk 事件，只能直接 import 内部文件，公共 API 与实际事件合同出现脱节。本轮已把这些常量全部补入 `src.monitoring.pipeline.__init__` 的导出列表，后续任何读模型、测试或监控适配层都可以经由正式公共端口消费完整 pipeline 事件族，而不是继续扩大内部模块耦合。
62. 2026-04-13：后台消费链的 trace 合同已开始按正式生命周期补齐，`ExecutionIntentConsumer` 与 `OperatorCommandConsumer` 不再只产出“处理成功/失败”的单点事件。此前 intent/command 后台线程即使已经具备 claim、lease、dead-letter 等状态机，pipeline 事件里仍缺少统一的 trace/account/instance 标识，`ExecutionIntentConsumer` 对 reclaim / dead-letter 也没有正式事件，`OperatorCommandService/Consumer` 则缺少对 `command_submitted / command_completed / command_failed(dead_lettered)` 的测试约束。本轮已将仓储 claim 返回值升级为结构化 transitions（`claimed / reclaimed / dead_lettered`），并在消费器侧补齐正式 trace 上下文：intent 事件现在稳定携带 `trace_id / account_key / account_alias / claimed_by_instance_id / claimed_by_run_id / instance_id / instance_role / signal_scope / source_metadata`，command 事件则统一携带 `trace_id / submitted_by_* / claimed_by_* / response_payload`。同时新增 `tests/trading/test_execution_intents.py` 与 `tests/trading/test_operator_commands.py`，正式覆盖 `intent_reclaimed / intent_dead_lettered / command_submitted / command_completed / command_failed(dead_lettered)` 等关键节点，确保后续 `/v1/trade/traces` 与 `/v1/trade/state/stream` 能把同一条业务链从 signal 一路串到后台消费完成，而不是在 intent/command 环节丢失标识或生命周期信息。
