# 系统启动巡检与 Live Canary Runbook

> 更新日期：2026-05-08（追加 recovery_budgeted 以 loss budget 为主风控、cycle quota 可禁用；追加真实 demo recovery calibration guard；2026-05-07 追加 active alert 自动恢复解除与 optional market-data stale advisory 口径；追加常驻 recovery runner 的 auto direction、entry cost gate 与 net exit 监控口径；2026-05-06 追加真实 demo 常驻 martingale runner 的 dispatch retry、ticket/exposure 监控口径；追加常驻 tick-derived bounded recovery runner 的 demo-only dry-run、ready 与 `/v1/trade/state` 监控口径；追加 tick-derived route、tick feature health 与 tick replay canary 门槛；追加 bounded recovery / martingale canary 默认关闭、dry-run 与 trace 验收口径；追加策略声明式 market data dependency 合同；追加 preflight 显式自动拉起 MT5 terminal 模式；追加 demo canary 启动状态恢复与 startup_summary 口径；追加 trace 目录近期窗口合同；2026-04-26 按正式合同口径校核：`deployment.allows_*` / `source_kind` 三态 / `active_alerts` 参与评级 / supervisor unexpected-exit alarm；2026-04-13 初版）
> 目标：把“服务能否正常启动、系统主链路是否真实打通、日志里哪些算真实问题”收口成一套可执行流程。
> 范围：只覆盖系统管线本身，不以真实交易下单是否成功作为本 runbook 的验收条件。

---

## 1. 权威观测面

这份 runbook 只依赖以下观测面，避免多处取数造成歧义：

| 观测面 | 位置 | 用途 |
|------|------|------|
| 主运行日志 | `data/logs/<instance>/mt5services.log` | 看启动顺序、线程存活、外部依赖异常 |
| 错误日志 | `data/logs/<instance>/errors.log` | 快速筛 WARNING / ERROR / EXCEPTION |
| 根健康探针 | `GET /health` | 看运行时组件快照和市场/交易摘要 |
| 就绪探针 | `GET /v1/monitoring/health/ready` | 看 `status=ready` 以及 `checks.storage_writer / ingestion / indicator_engine` 是否达到 ready |
| 队列视图 | `GET /v1/monitoring/queues` | 看 `writer_alive / ingest_alive` 与队列压力 |
| 指标性能视图 | `GET /v1/monitoring/performance` | 看 `event_loop_running` 等指标链路状态 |
| 事件存储视图 | `GET /v1/monitoring/events` | 看 indicator durable event queue 是否推进 |
| 交易 trace 视图 | `GET /v1/trade/trace/by-*`、`GET /v1/trade/traces` | 用业务标识反查单条链路；宽列表只用于近期巡检 |
| 预检 CLI | `python -m src.ops.cli.live_preflight --environment live`；需要预热 terminal 时显式加 `--auto-launch-terminal` | 启动前检查 MT5 / DB / 配置一致性 |

---

## 2. 启动前检查

### 2.1 最小前置动作

1. 先运行只读预检：

```powershell
python -m src.ops.cli.live_preflight --environment live
```

默认预检保持严格模式：要求 MT5 terminal 进程已经存在，不会为了检查而隐式启动 GUI。若本次目标是预热 demo terminal 或验证本机 local 凭据可自动登录，使用显式模式：

```powershell
python -m src.ops.cli.live_preflight --environment demo --auto-launch-terminal
```

该模式允许 MT5 Python API 调用 `mt5.initialize(path, login, password, server, timeout)`，用 `config/instances/<instance>/mt5.local.ini` 中的凭据拉起 terminal 并建立 session；它仍不启动 MT5Services 服务，也不执行交易。不要用裸 `Start-Process terminal64.exe` 代替该步骤，因为裸启动 terminal 不会读取项目 local 配置。

若预检返回以下任一错误码，先处理 MT5 会话，不要继续启动：

- `terminal_not_running`
- `ipc_timeout`
- `interactive_login_required`
- `login_failed`
- `account_mismatch`

2. 确认运行期目录只落在项目根目录 `data/`，而不是 `src/`：

```powershell
Get-ChildItem -Path .\data
Get-ChildItem -Path .\src -Recurse -Directory | Where-Object { $_.FullName -match '\\data($|\\)|\\logs($|\\)' }
```

3. 确认本机 `config/*.local.ini` 覆盖是已知且可解释的，尤其是：
   - `signal.local.ini`
   - `risk.local.ini`
   - 任何会冻结策略、改变运行模式、关闭模块的本机覆盖
4. 如果本次要打开自动交易，先确认两件事都已经显式配置：
   - 每个注册策略都有 `[strategy_deployment.<name>]`
   - 每个允许 live execution 的策略都在 `signal.ini` / `signal.local.ini` 里显式绑定到真实 `account_alias`

### 2.2 启动前必须明确的判断

| 判断项 | 要点 |
|------|------|
| 本次目标是不是“只验系统” | 如果是，只看采集/缓存/落库/事件消费/观测，不把“没有交易”判成失败 |
| 当前是否休盘 | 休盘时 `market_data data latency critical` 可能是预期现象 |
| 当前是否准备做开盘 canary | 如果是，至少预留 15 到 30 分钟观察窗口 |
| 当前是否打开自动交易 | 只有当 live 策略已显式 deployment + 显式 account binding 时才允许打开；否则应视为配置不完整 |

### 2.3 部署合同前置校验（正式合同口径）

按 ADR-010 / ADR-009 / §0dd，`deployment.status` × `environment` 决定装配/intent/执行资格，启动前必须用工具核对，禁止凭印象判断：

```powershell
python -m src.ops.cli.confidence_check --tf <M5/M15/M30/H1/H4/D1>
```

预期输出与决策口径对照：

| 工具输出 | 真实含义 | 决策 |
|------|------|------|
| `*** NO LIVE-ELIGIBLE STRATEGIES — all current strategies are CANDIDATE / DEMO_VALIDATION ***` | 当前 `[strategy_deployment.*]` 没有 ACTIVE / ACTIVE_GUARDED 策略 | live-main / live-exec-* 装配集合为空（§0dd 状态事实）。**不允许打开 live 自动交易**。要 canary 必须先 promote 至少 1 条策略到 `active_guarded` |
| `ALL TFs BLOCKED` + 候选不为空 | 阈值（baseline + `effective_min_confidence`）确实拒绝了所有信号 | 调阈值或调研 confidence 管线，**不要**把它误读为"没有 live 策略" |
| 单 TF 列出 PASS/BLOCK 行 + `min={X}*` 标记 | `*` 表示 deployment.effective_min_confidence 推高了 baseline | 这是 ACTIVE_GUARDED 强制提阈值的合同行为，不是配置错误 |

四种 deployment status × environment 装配/intent 矩阵（速查）：

| status | live 装配 | demo 装配 | live publisher | demo publisher | pre_trade_checks |
|--------|----------|----------|----------------|----------------|------------------|
| `candidate` | ❌ | ❌ | ❌ | ❌ | reject (allows_runtime_evaluation=False) |
| `demo_validation` | ❌ | ✅ | ❌ | ✅ | reject in live (allows_live_execution=False) |
| `active_guarded` | ✅ | ✅ | ✅ | ✅ | pass + 强制 effective_min_confidence ≥ baseline+0.05 |
| `active` | ✅ | ✅ | ✅ | ✅ | pass + 自带 min_final_confidence（若声明） |

`signal.ini` / `signal.local.ini` 的 `[account_bindings.<account>]` 必须只引用当前能装配的策略（live binding 仅 ACTIVE/ACTIVE_GUARDED；demo binding 含全部 demo_validation）。binding 与装配集漂移会导致"已绑定但永不装配"或"已装配但 publisher 找不到 target account"的静默漏单——sentinel test 已锁住这两类（参 §0dd）。

---

## 3. 标准启动步骤

### 3.1 启动服务

说明：

- `python -m src.entrypoint.web`
- `python -m src.entrypoint.instance --instance ...`
- `python -m src.entrypoint.supervisor --environment ...`

现在都会先执行正式 MT5 session gate。若终端未预热、IPC 未就绪、账户不匹配，或 MT5 仍在等人工密码框，入口会直接失败退出，而不是进入半启动状态。

单实例：

```powershell
python -m src.entrypoint.web
```

命名实例（推荐多账户/多进程部署时使用）：

```powershell
python -m src.entrypoint.instance --instance live-main
```

按环境启动整组实例（推荐）：

```powershell
python -m src.entrypoint.supervisor --environment live
```

按拓扑组启动 `main + workers`：

```powershell
python -m src.entrypoint.supervisor --group live
```

### 3.2 并行观察日志

另开两个窗口：

```powershell
Get-Content -Path .\data\logs\<instance>\mt5services.log -Encoding utf8 -Wait
```

```powershell
Get-Content -Path .\data\logs\<instance>\errors.log -Encoding utf8 -Wait
```

### 3.3 并行观察探针

```powershell
curl.exe http://127.0.0.1:8808/health
curl.exe http://127.0.0.1:8808/v1/monitoring/health/ready
curl.exe http://127.0.0.1:8808/v1/monitoring/queues
curl.exe http://127.0.0.1:8808/v1/monitoring/performance
curl.exe http://127.0.0.1:8808/v1/monitoring/events
```

---

## 4. 启动后 5 分钟巡检

### 4.1 通过条件

| 模块 | 看哪里 | 通过条件 |
|------|------|------|
| Web 入口 | `/health` | 返回 200 |
| 主实例 ready | `/v1/monitoring/health/ready` | `main` 实例返回 `{"status":"ready"}`，且 `checks.storage_writer=ok`、`checks.ingestion=ok`、`checks.market_data_health=ok`、`checks.indicator_engine=ok`；若启用 `tick_derived` 策略，还必须出现且通过 `checks.tick_feature_health=ok`；若启用 `[recovery_runtime_runner]`，还必须出现且通过 `checks.recovery_runner=ok` |
| 执行实例 ready | `/v1/monitoring/health/ready` | `executor` 实例返回 `{"status":"ready"}`，且 `checks.storage_writer=ok`、`checks.execution_intent_consumer=ok`、`checks.operator_command_consumer=ok`、`checks.pending_entry=ok`、`checks.position_manager=ok`、`checks.account_risk_state=ok` |
| 采集线程与 MT5 market circuit | `/health`、`/v1/monitoring/health/ready` 或 `/v1/monitoring/queues` | `ingest_alive=true`，且 `details.market_data_health.mt5.circuit_open=false`、`freshness.critical_stale_count=0`、`freshness.critical_missing_count=0`。M1/M5 OHLC lane stale 是阻断项；H1/H4/D1 使用各自动态阈值，不按 M1 阈值误判。tick stale 默认是 advisory；只有当策略能力声明 `market_data_requirements=["tick"]` 并进入 `dependency_contract.required_lanes` 后，tick stale/missing 才是 ready 阻断项。`data_ingestion.market_data_lane_stale` 只代表 required/critical lane 存在 stale；optional tick stale 只应表现为 `market_data_advisory_stale_count`，不得进入 active alert 或拉低 overall |
| Tick feature 链路 | `/health`、`/v1/monitoring/health/ready`、`/v1/trade/state` | 若启用 `tick_derived` 策略或 `[recovery_runtime_runner]`，`tick_feature_health.status` 不得是 `stale/sparse/blocked/unavailable`，`tick_feature_health.queue_depth` 低于当前配置容量，且 `dropped_snapshots=0` 或有明确解释。XAUUSD 必须使用 `config/app.ini[tick_feature_point_size].XAUUSD=0.01` 这类品种点值合同，否则正常黄金点差会被误判为 `spread_wide`；若 `last_reasons` 包含 `quote_side_missing`，说明当前 tick/quote 只有 price/last 而缺 bid/ask，可研究但不可交易 |
| 常驻 recovery runner | `/v1/monitoring/health/ready`、`/v1/trade/state`、`/health` | 若启用 `[recovery_runtime_runner]`，`/ready.checks.recovery_runner=ok`，`/v1/trade/state.recovery_runner.running=true`、`stalled=false`、`consecutive_errors=0`，`processed_snapshots` 应随 tick feature 推进；`active_cycle_id` 可为空或为当前 cycle，但不能长期 `stalled=true`。真实 demo 时还必须看 `dry_run=false`、`calibration_guard.enabled=true`、`calibration_guard.reason=calibration_guard_passed` 或可解释的 fail-closed 原因、`entry_calibration.target_shortfall_points.p90_recent<=0`、`entry_calibration.net_margin_points.p50_recent>0`、`active_submitted_tickets`、`active_live_position_tickets`、`last_position_check_error`、`decision_counts.block/hold` 与 `last_reason`；`blocked_initial_retry_wait` / `blocked_step_retry_wait` 表示交易端口或风控阻断后正在退避，不应出现 `runner_exception` |
| 落库线程 | `/health` 或 `/v1/monitoring/queues` | `writer_alive=true` |
| 指标事件循环 | `/health` 或 `/v1/monitoring/performance` | `event_loop_running=true` |
| 信号运行时 | `/health` | `signal_runtime.running=true` |
| MT5 会话 | `/health` | `runtime.external_dependencies.mt5_session.session_ready=true`，且 `interactive_login_required=false` |
| 启动摘要与状态恢复 | `data/logs/<instance>/mt5services.log`、`/v1/trade/state` | `startup_summary mode=<mode>` 必须与 `/v1/trade/state.runtime_mode.current_mode` 一致；日志应出现 `Position runtime state recovery` 摘要。若 MT5 当前无持仓，`/v1/trade/state.account_open` 与 active managed positions 不应继续显示历史遗留 open 仓位 |
| 执行行情门禁 | `/v1/trade/state` | 若本次准备做 live canary，则目标 executor 应满足 `tradability.quote_health.stale=false`、`tradability.market_data_health.blocking=false`、`account_risk.should_block_new_trades=false`；若本次准备验证 `intrabar` trigger，还应同时确认 admission/trace 中不存在 `market_data_unhealthy`、`intrabar_synthesis_stale` 或 `intrabar_synthesis_unavailable`；若验证 `tick_derived`，还必须确认 `tradability.tick_feature_health.blocking=false` 且 admission/trace 不存在 `tick_feature_health_unavailable` 或 `tick_feature_health_blocked` |
| Intrabar 合成时效 | `/health` | 若主实例启用了 intrabar，则 `runtime.components.ingestor.intrabar_synthesis.status` 不应长期为 `warning`，`stale` 数量应可解释 |
| 执行消费推进 | `/v1/monitoring/health/ready` 或 `/v1/monitoring/health` | `execution_intent_consumer` 与 `operator_command_consumer` 不应 `stalled=true`，`stall_reasons` 不应包含 `poll_stalled / consecutive_errors / in_flight_stalled` |
| 交易链路 trace 可审计性 | `/v1/trade/trace/{signal_id}`、`/v1/trade/trace/by-trace/{trace_id}`、`/v1/trade/trace/by-intent/{intent_id}`、`/v1/trade/trace/by-command/{command_id}`、`/v1/trade/trace/by-action/{action_id}`、`/v1/trade/traces?intent_id=...`、`/v1/trades/{trade_id}` | canary 或真实执行产生 `signal_id / trace_id / intent_id / command_id / action_id` 任一标识后，都应能反查同一条业务链路；`identifiers` 应包含对应标识，`summary.admission` 应能解释最新准入阶段，`lifecycle.summary/entry/management/exit/outcome` 应能串联 order ticket、position ticket、SL/TP 调整与平仓结果。无标识的 `/v1/trade/traces` 宽列表默认只查最近 6 小时，并在 metadata 暴露 `default_window_applied=true`；历史审计必须显式传时间范围或走 `by-*` |
| Active alerts 生命周期 | `/v1/monitoring/health` | `active_alerts` 不为空时，`overall_status` 必须降级为 `warning` 或 `critical`；同一 metric 恢复到 healthy 后，active alert 必须自动解除。绝不允许同时出现 `critical alerts` 与 `overall_status=healthy`，也不允许已恢复故障长期挂在 active alerts 中制造"假红" |
| Workbench source_kind 三态 | `/v1/execution/workbench` | 单实例 = `native`；多账户 main 不挂 executor（delegated 拓扑）= `delegated`；executor 拓扑异常 = `fallback`。`fallback` 才是降级，`delegated` 是合法拓扑（§0r #3）|
| Supervisor 子进程退出告警 | `data/logs/<instance>/mt5services.log` | §0aa 后任何 child 退出（含 `code=0`）都视为 unexpected——若日志出现 `supervisor: instance %s exited unexpectedly (code=...)` 但 `_managed` 中实例仍存在，按 supervisor restart 流程检查；§0bb 加了独立 alarm counter，可在 `/health` 看 `supervisor_unexpected_exit_total` |
| 入口日志 | `data/logs/<instance>/*.log` | 日志文件创建在实例隔离目录，而不是 `src/` 下或共享日志目录 |
| 运行期本地文件 | `data/runtime/<instance>/` | `events.db`、`signal_queue.db`、`health_monitor.db`、`health_alerts.db` 落在实例隔离目录；其中部分文件可能在组件首轮写入后创建 |
| 手工运行产物 | `data/artifacts/` | 回测 JSON、压测日志、启动排查输出统一放这里，不再使用根目录 `runtime/` |

### 4.2 失败条件

以下任一项出现，都按“真实系统问题”处理：

1. `/v1/monitoring/health/ready` 返回 503，或返回体中 `status != ready`。
2. `ingest_alive=false`、`writer_alive=false` 或 `event_loop_running=false`。
3. `checks.market_data_health!=ok`，尤其是 `mt5.circuit_open=true`、`freshness.critical_stale_count>0`、`freshness.critical_missing_count>0`、M1/M5 `ohlc:<symbol>:<TF>.stale=true`，或 `data_ingestion.market_data_lane_stale.details.blocking_stale_lanes` 非空。若 `dependency_contract.required_lanes` 包含 `tick:<symbol>`，对应 tick lane 的 `stale=true` 或 `status=warming_up` 也按阻断处理；未被策略声明依赖的 optional tick stale 只能作为 advisory 指标处理，不应触发 active alert。
4. 启用 `tick_derived` 策略时，`checks.tick_feature_health!=ok`，或 `/v1/trade/state.tradability.tick_feature_health.blocking=true`。
5. 启用 `[recovery_runtime_runner]` 时，`checks.recovery_runner!=ok`，或 `/v1/trade/state.recovery_runner.running=false/stalled=true/consecutive_errors>0`。
6. `execution_intent_consumer` 或 `operator_command_consumer` 返回 `stalled=true`；即使线程还活着，也按“无法推进交易消费链”处理。
7. 日志持续出现 schema/check constraint 错误、启动 `TypeError`、线程崩溃、事件循环接口错误。
8. 运行期日志或 SQLite 文件重新落到 `src/` 下，或落回共享 `data/logs/`、`data/runtime/` 根目录。
9. 手工排障/压测/回测产物重新落到仓库根目录 `runtime/`。
10. `events.db` 视图长期不推进，同时 confirmed OHLC 已经在更新。
11. `/health.runtime.external_dependencies.mt5_session.session_ready=false`，或 `error_code` 为 `interactive_login_required / account_mismatch / login_failed`。
12. 准备执行 live canary 时，`/v1/trade/state` 显示 `tradability.quote_health.stale=true`、`tradability.market_data_health.blocking=true` 或 `account_risk.should_block_new_trades=true`。
13. `startup_summary mode=error` 但 `/v1/trade/state.runtime_mode.current_mode` 有明确模式，按启动摘要合同漂移处理；不要把它误判为真实 runtime mode error。
14. MT5 当前无仓位但 `/v1/trade/state` 仍显示 active/open managed positions，按 position runtime state 恢复失败处理。

### 4.3 PowerShell 版快速判读顺序

1. 先看 `errors.log` 有没有连续异常。
2. 再看 `startup_summary`：`mode` 应与 `/v1/trade/state.runtime_mode.current_mode` 一致；若存在 `Position runtime state recovery`，确认它的 `closed_missing` 与 MT5 当前仓位事实相符。
3. 再看 `/v1/monitoring/health/ready` 是否稳定返回 `status=ready`，且 `checks.*=ok`。
4. 主实例重点看 `details.market_data_health`：`mt5.circuit_open=false`、`freshness.critical_stale_count=0`、`freshness.critical_missing_count=0`、M1/M5 OHLC lane 未 stale；若本次策略声明 tick 依赖，确认 `dependency_contract.required_lanes` 中的 tick lane 都不是 stale/warming_up。
5. 若本次启用 `tick_derived`，再看 `details.tick_feature_health` 或 `/v1/trade/state.tradability.tick_feature_health`：feature freshness、queue depth、dropped snapshots 都必须可接受。
6. 若本次启用 `[recovery_runtime_runner]`，再看 `details.recovery_runner` 和 `/v1/trade/state.recovery_runner`：`running=true`、`stalled=false`、`consecutive_errors=0`，`processed_snapshots` 应随 tick feature 推进。
7. executor 重点看 `details.execution_intent_consumer / details.operator_command_consumer`：`stalled=false`、`consecutive_errors=0` 或可解释。
8. 若已有 `trace_id / intent_id / command_id / action_id`，用 `/v1/trade/trace/by-*` 先反查业务链路，再看裸日志；若只有列表条件，用 `/v1/trade/traces?intent_id=...` 或对应 execution 标识过滤。不要用无条件 `/v1/trade/traces` 做跨日历史审计；宽列表默认近期窗口只是控制面巡检入口。
9. 再看 `/health` 里的 `ingestor / storage_writer / indicator_engine / signal_runtime`，最后才看更细的 `queues / performance / events`。
10. 若 `ready=ok` 但仍怀疑 MT5 登录侧异常，直接看 `/health.runtime.external_dependencies.mt5_session`；`ready=ok` 只代表当前运行链路通过门禁，不替代 live preflight。

多实例补充：

- `main` 和 `executor` 的 `/ready` 检查项不同，不能再用主实例口径去判定 worker。
- `executor` 的 `/health` 中 `ingestor.running=false`、`indicator_engine.running=false`、`signal_runtime.running=false` 是当前设计下的预期现象；但 `execution_intent_consumer / operator_command_consumer / pending_entry / position_manager / account_risk_state` 必须正常，否则不能视为可交易。

---

## 5. 哪些日志算真实问题，哪些要结合时段判断

### 5.1 明确属于真实问题

| 现象 | 判定 | 原因 |
|------|------|------|
| `ready` 探针 503 | 阻塞问题 | 主链路关键组件未达到 ready |
| 启动直接报 `interactive_login_required` | 阻塞问题 | MT5 终端需要人工解锁/输密码，当前不支持无人值守自动越过 |
| 启动直接报 `account_mismatch` | 阻塞问题 | 终端当前登录账户与实例配置不一致 |
| `writer_alive=false` / `ingest_alive=false` / `event_loop_running=false` | 阻塞问题 | 采集、落库或指标 durable 消费已断 |
| `trade_state.quote_stale=true` 且新交易被挡 | 阻塞问题 | 执行侧报价已过期；当前系统会把 stale quote 作为正式开仓门禁，而不再只是观测 flag |
| `intrabar_synthesis_stale` / `intrabar_synthesis_unavailable` 持续出现 | 阻塞问题 | 盘中 trigger 对应的子 TF 合成事实已断更或根本未带出；当前系统会把它作为 intrabar 开仓正式门禁 |
| 重复出现 DB check constraint 失败 | 阻塞问题 | 持久化契约已漂移，系统会持续报错或进入 DLQ |
| 启动阶段出现 `TypeError` / 组件装配失败 | 阻塞问题 | 说明装配层与运行态契约未对齐 |
| `src/` 下再次出现运行期 `data`/`logs` | 配置回归 | 运行期文件锚点被写坏 |

### 5.2 不要直接误判成阻塞

| 现象 | 判定 | 如何处理 |
|------|------|------|
| 休盘窗口出现 `market_data data latency critical` | 时段相关现象 | 先确认是否休盘；休盘时这不等于链路断裂 |
| 经济日历外部源偶发 `read operation timed out` | 外部依赖退化 | 如果核心 ready/health 正常，可记为外部依赖风险，不单独判系统失败 |
| `economic calendar` 在启动早期显示 `warming_up=true`、`stale=false` | 启动引导期现象 | 表示服务尚未完成首次正式刷新窗口，不应按 stale 告警处理；只有超过 bootstrap deadline 仍无成功刷新才算真实 stale |
| `economic calendar` 重启后短时间显示 `health_state=refreshing`，但 `last_refresh_at` 已有值 | 正常恢复现象 | 表示上一轮成功刷新已从持久化状态恢复，本轮 `release_watch` 正在继续执行；这不是 stale，也不是恢复失败 |
| `/health` 中 `trade_executor.running=false` | 当前实现语义 | `TradeExecutor` 是懒启动 worker，未收到交易信号前不代表未装配 |
| 启动后短时间没有信号或没有交易 | 不足以判失败 | 本 runbook 验的是系统链路，不是策略一定出信号 |
| multi_account main 实例 `/v1/execution/workbench.source_kind=delegated` | 合法拓扑（§0r #3） | main 角色只做共享计算，不直接挂 executor；`tradability.verdict=not_applicable` + `reason_code=not_executor_role` 是预期；admission 不会再把 `runtime_present=False` 误标为故障 |
| 启动期 `confidence_check` 显示 `NO LIVE-ELIGIBLE STRATEGIES` | 当前部署阶段事实 | 见 §2.3——只代表 active=0；不允许打开 live 自动交易，但不影响系统巡检本身 |

---

## 6. 开盘窗口 Live Canary

### 6.1 目标

这一步不是验证“能不能交易赚钱”，而是验证开盘后系统主链路是否在真实 feed 下持续稳定：

```text
MT5 行情 -> BackgroundIngestor -> MarketDataService
-> StorageWriter -> TimescaleDB / runtime files
-> closed-bar event -> events.db
-> IndicatorEventLoop -> signal runtime
```

若本次启用 `tick_derived`，同时观察第二条链路：

```text
MT5 ticks -> MarketDataService.extend_ticks -> TickBatchEvent
-> TickFeatureEngine -> TickFeatureSnapshot
-> SignalRuntime tick_derived -> execution_intents
-> admission -> risk -> executor
```

### 6.2 观察窗口

- 最短 15 分钟
- 更稳妥是 30 分钟
- 只在目标市场开盘且有真实行情流入时执行

### 6.3 Canary 步骤

1. 开盘前 5 分钟先完成第 2 节预检和第 4 节启动后巡检。
2. 开盘后第 0 到 2 分钟：
   - 看 `/health` 是否仍返回 200；
   - 看 `errors.log` 是否出现连续异常；
   - 确认没有新的装配失败或线程退出。
3. 开盘后第 2 到 5 分钟：
   - 看 `/v1/monitoring/health/ready` 是否持续为 `ready`；
   - 看 `writer_alive / ingest_alive / event_loop_running` 是否保持为真。
4. 开盘后第 5 到 15 分钟：
   - 看 `/v1/monitoring/events` 是否在推进；
   - 看 `events.db` 对应事件视图是否不再停滞；
   - 看 `errors.log` 是否出现持续刷屏的持久化异常。
5. 开盘后第 15 到 30 分钟：
   - 看主日志中是否出现重复队列阻塞、线程重启、事件循环中断；
   - 看系统仍然只向根目录 `data/` 写运行期文件。
6. 若本次是 demo 手工下单 canary（非默认 live canary 要求）：
   - 先用 `/v1/trade/precheck` 确认 admission 未阻断；market order 必须带保护性 SL；
   - `/v1/trade/dispatch` 返回的 `metadata.trace_id`、`data.result.trace_id`、`data.admission_report.trace_id` 必须一致；
   - 用 `/v1/trade/trace/by-trace/{trace_id}` 能反查 `execute_trade` 审计，`identifiers.order_tickets` 应包含 broker ticket；
   - 平仓后用 `/v1/trade/trace/by-command/{command_id}` 或 `/v1/trade/trace/by-action/{action_id}` 反查关闭动作；最后确认 `/v1/positions?symbol=<symbol>` 为空。
7. 若本次是 `tick_derived` demo 下单 canary：
   - 先确认同一策略、同一 symbol、同一 session profile 已有 tick replay 报告，且报告包含 `tick_coverage`、`feature_coverage`、`average_spread_points`、`max_spread_points`、`rejected_signal_count`；其中 `tick_coverage / feature_coverage` 只认可 bid/ask 完整的可成交 tick，last-only 历史 tick 不计入覆盖；
   - `/v1/monitoring/health/ready` 必须包含并通过 `tick_feature_health`，`/v1/trade/state.tradability.tick_feature_health.blocking=false`；
   - admission/trace 中不得出现 `tick_feature_health_unavailable` 或 `tick_feature_health_blocked`；
   - 若 feature queue depth 持续增长、`dropped_snapshots>0` 或 spread/replay 成本明显偏离报告，停止 canary。
8. 若本次是 bounded recovery / martingale canary：
   - 共享 `config/risk.ini` 必须保持 `[recovery_execution_canary] enabled=false` 且 `dry_run=true`；只能在目标实例 `risk.local.ini` 做显式、短窗口覆盖。
   - 常驻验证入口为实例级 `[recovery_runtime_runner] enabled=true demo_only=true`。默认先用 `dry_run=true`；只有真实 demo 验证窗口才允许在目标实例 local 显式改成 `dry_run=false`。该 runner 不属于普通 `SignalStrategy`，不进入 `strategy_timeframes/account_bindings`，只消费 `TickFeatureSnapshot` 并写 `recovery_cycle_states` 与 `execute_trade` 审计。
   - 常驻 runner 运行时，`/v1/monitoring/health/ready` 必须包含 `checks.recovery_runner=ok`；`/v1/trade/state.recovery_runner.running=true`、`stalled=false`、`consecutive_errors=0`，`processed_snapshots` 应持续增长。若 `stalled=true`，先查 tick feature freshness/queue，而不是调整马丁参数。
   - 若 `direction_mode=auto`，必须能从 `/v1/trade/state.recovery_runner.decision_analytics` 或 trace / trade payload metadata 看到 `direction_policy.reason`；`direction_signal_too_weak`、`direction_pressure_mismatch` 是正常 hold，不应视为系统故障。若 `cost_gate.reason` 为 `spread_too_wide` 或 `net_target_below_cost`，说明当前 spread/成本预算下不应入场，禁止通过降低成本门槛强行恢复真实 demo。
   - 观察真实 demo 时至少记录 `decision_analytics.reason_counts`、`direction_policy_reason_counts`、`cost_gate_reason_counts`、`cost_gate_allowed_counts` 与 `net_exit`；这些统计用于判断问题属于方向触发、成本环境、加仓参数还是退出目标，而不是只看是否成交。
   - 真实 demo runner 的退出不能只看裸 `recovery_target_points`；必须确认 close decision metadata 中 `net_profit_points >= target_net_points`，否则视为成本合同失效。
   - 若 `order_kind=market`，实例级 canary 必须显式配置 `protective_stop_points`；缺失时 recovery 扩仓必须结构化拒绝，不能为了跑通 dry-run 放宽 `market_order_protection=sl`。
   - 标准 dry-run 入口为 `python -m src.ops.cli.recovery_canary --environment demo --instance demo-main --auto-launch-terminal ...`；该入口只允许 demo + `dry_run=true`，不会提交真实订单。
   - 当前 quote live-cycle 入口为同一 CLI 追加 `--live-cycle`。默认只 dry-run 初始仓和扩仓评估；只有显式追加 `--submit-initial-order` 才允许提交 demo 初始仓。若还要提交一笔真实 demo recovery 扩仓，必须额外显式追加 `--submit-recovery-order`，且本轮只允许 `--max-steps 1`、初始手数不超过 `0.01`、单次 recovery 不超过 `0.02`、总暴露不超过 `0.03`，并禁止 `--keep-initial-open`。提交真实仓位后 CLI 必须自动 close 全部 canary ticket，并记录 `recovery_cycle_states.status=closed`；若自动 close 返回失败，CLI 必须二次只读核对 MT5 持仓；ticket 仍在或核对失败时必须输出 `critical_alerts` 并把 recovery cycle 写成 `blocked`。
   - 进入 dry-run 前必须先用同策略、同 symbol、同 session profile 的 `TickRecoveryReplayRunner` 报告跑过 `RecoveryCanaryGate`，结果为 `allowed=true` 且 `stage=dry_run_canary`；若 reasons 包含 `tick_coverage_below_minimum`、`replay_cycle_not_closed`、`blocked_ratio_above_maximum` 或 `real_order_requires_reviewed_dry_run`，不得继续。
   - `RecoveryCanaryGate` 的 blocked ratio 只统计坏数据/不可交易类 block；达到硬上限后的 `max_steps_reached / max_next_volume_exceeded / max_total_volume_exceeded` 只作为暴露上限事实进入 details，不再把“达到上限后等待恢复”误判为 replay 质量失败。
   - dry-run 阶段必须看到 `recovery_cycle_states` 有目标 `account_key + cycle_id` 记录，`trade_command_audits` 出现 `operation=execute_trade` 且 `request_payload.metadata.execution_scope=recovery_scaling`。若使用 `--live-cycle` 且未提交真实初始仓，CLI 必须输出 `dry_run_cycle_finalization.status=closed`，并且 canary 结束后不应留下 `tick_recovery_probe` 的 open recovery cycle。若使用 `--submit-recovery-order`，必须看到 `current_step.status=submitted`、`recovery_cleanup.status=closed`，以及 `close_initial` / `close_step_1` 两个 cleanup action；结束后 MT5 XAUUSD 持仓/挂单和 open recovery cycle 都必须为 0。
   - 若 dry-run 内部 `precheck.executable=false` 或抛出 `PreTradeRiskBlockedError`，结果必须结构化返回 `category=pre_trade_risk`；不得记录新的 recovery cycle，也不得用 traceback 结束。`dry_run=true` 的 `execute_trade` 审计不得消耗 `max_trades_per_hour/max_trades_per_day` 额度。
   - `recovery_budgeted` 真实 demo 常驻 runner 的主风控是 recovery loss budget、单 cycle 暴露上限、成本门禁、保护止损与清仓能力；`max_cycles_per_session/max_cycles_per_day/max_cycles_per_hour/min_cycle_interval_seconds/cooldown_after_cycle_close_seconds=0` 表示禁用对应频率门禁。只有需要临时节流观测时才启用 cycle quota；若 `/v1/trade/state.recovery_runner.last_reason=max_cycles_per_day_reached` 或 `max_cycles_per_hour_reached`，先确认该实例是否仍处于节流档，而不是把它当作正式 recovery 风控。
   - 交易端口抛出的限频、broker 或 reservation 失败必须结构化落成 `action=block` / `status=blocked|skipped`，随后进入 `blocked_initial_retry_wait` 或 `blocked_step_retry_wait`；`consecutive_errors` 必须保持 0。若出现 `runner_exception` 或 command audits 按 tick 频率刷 `Hourly trade limit reached`，立即暂停实例并修 recovery dispatch boundary。
   - 真实 demo 审计查询使用 `GET /v1/trade/command-audits?...`，不是 `/v1/trade/commands/audits`。提交成功必须看到 `command_type=execute_trade`、`status=success`、broker `ticket/order_id/deal_id`；失败但被风控或限频挡住时，应只有退避周期内的有限 `execute_trade failed` 审计，不应连续按 tick 增长。
   - 用 `/v1/trade/trace/by-trace/{trace_id}` 或 `/v1/trade/trace/{source_signal_id}` 反查同一条链路，必须看到 `recovery_cycle_ids`、`recovery.step`、`recovery_cycle_id`、`recovery_step_index` 与 dry-run 审计一致。若执行了真实初始仓自动清理，还必须看到 `trade.close_position` / `recovery.closed` 与 `recovery:<cycle_id>:close_initial`；若执行了 `--submit-recovery-order`，trace summary 必须是 `status=completed`、`last_stage=recovery.closed`、`command_counts.execute_trade=2`、`command_counts.close_position=2`。若 cleanup 失败，则必须看到 `critical_alerts[*].code` 与 `recovery_cycle_states.status=blocked`，不得判 canary 通过。
   - 若 `RecoveryPreTradeGuard` 返回非允许、recovery 状态缺失或 stale、trace 找不到 cycle、spread/step volume 与 replay 报告不一致，立即停止 canary。

### 6.4 Canary 通过标准

满足以下条件即可判“系统通路通过”：

1. `ready` 探针在整个观察窗口内稳定可用。
2. `ingest_alive / writer_alive / event_loop_running` 全程未掉。
3. 没有重复的 schema/contract/type error。
4. `events.db` 与监控事件视图能体现事件持续推进。
5. 运行期日志和 SQLite 文件全部落在 `data/logs/<instance>/` 与 `data/runtime/<instance>/`。
6. **`active_alerts` 始终参与评级且可自动恢复**：`/v1/monitoring/health` 中 `critical_count > 0` 时 `overall_status` 必须为 `critical`，不出现"健康面假绿"现象；同一 metric 重新采样为 healthy 后，active alert 应自动消失，避免"健康面假红"。
7. **`source_kind` 反映真实拓扑**：`/v1/execution/workbench.source_kind` 与拓扑期望一致（单实例 native / delegated main 不报 fallback）。
8. 若执行了 demo 手工下单 canary，`request_id` 只作为幂等/业务请求标识；链路审计以 `trace_id` 为主键，`by-trace` 必须能连到 `execute_trade` 顶层 `ticket/order_id/deal_id`。
9. 若执行了 `tick_derived` canary，必须能在 health/tradability 中解释 tick feature freshness，并能用 replay 报告解释成交侧价格和 spread 成本。
10. 若执行 recovery canary，初始阶段必须是 dry-run 审计通过；常驻 runner 必须在 `/ready` 与 `/v1/trade/state.recovery_runner` 同时显示 running 且未 stalled。真实下单前必须人工复核 `execution_scope=recovery_scaling`、`recovery_cycle_id`、`recovery_step_index`、`source_signal_id`、`guard.allowed=true`，且禁止从共享默认配置开启。dry-run live-cycle 结束后必须确认 `dry_run_cycle_finalization.status=closed` 且无 open `tick_recovery_probe` cycle；若执行 `--submit-recovery-order`，本轮只允许一层 recovery，并必须确认 `recovery_cleanup.status=closed`、MT5 持仓/挂单为空、open cycle 为空。任何 cleanup `critical_alerts` 都代表 canary 失败，先人工处理残留 ticket，再继续。

### 6.5 进入 active_guarded canary 的额外前置（不仅是系统通路）

本 runbook §6.4 是"系统主链路通路"门槛，不等于"可以打开 live 自动交易"。要 promote 1 条策略到 `active_guarded` 并放出实盘 canary，还必须满足：

1. 该策略在 demo 环境跑过 ≥7 天 `demo_validation`，trace 完整、outcome 落库可对账（参 ADR-010 §6 新策略上线路径）。
2. `confidence_check --tf <target>` 显示该策略不再返回 `NO LIVE-ELIGIBLE`，且 `effective_min_confidence` 推高后仍能产出 PASS。
3. binding 配置仅引用该 1 条策略 + 仅 1 个 live executor + 最小仓位/最小交易窗口（清单 WP6 退出标准）。
4. 已定义停机条件：拒单率 / 滑点 / 失败率 / 熔断 / trace 丢失 / 健康降级——任一触发，立即降级 `active_guarded → demo_validation` 而非继续运行。
5. 若该策略是 `tick_derived`，必须先通过 tick replay，不允许只凭 M1/M5 OHLC 回测或 demo 主链 ready 就 promote。
  6. 若该策略包含 bounded recovery / martingale 资金管理，只允许有限层数、有限总手数、有限单次手数、loss budget 与清仓审计；最小层间隔或 cycle quota 是可选节流工具，不是 recovery_budgeted 的主风险合同。必须先通过 tick recovery replay、dry-run recovery canary、hard-cap guard 与 trace 审计。无限马丁或用普通 signal 连续发单模拟马丁不允许进入 `active_guarded`。

### 6.6 Canary 失败后如何归类

| 失败现象 | 先归到哪个层次 |
|------|------|
| 没有新鲜行情，但线程都活着 | 环境 / 时段 / MT5 feed 问题 |
| 采集活着，但 `events.db` 不推进 | 指标 durable event 链路问题 |
| `writer_alive=false` 或队列异常堆积 | 持久化链路问题 |
| `event_loop_running=false` | indicators runtime 问题 |
| 只有经济日历外部源超时 | 外部依赖问题 |

---

## 7. 建议的固定执行顺序

每次准备验证系统时，固定走下面的序列，不要跳步：

1. `python -m src.ops.cli.live_preflight --environment live`
2. 若需要由工具预热 MT5 terminal，执行 `python -m src.ops.cli.live_preflight --environment demo --auto-launch-terminal` 或对应环境；不要裸启动 terminal GUI
3. `python -m src.entrypoint.web`
4. 盯 `data/logs/errors.log`
5. 查 `/v1/monitoring/health/ready`
6. 查 `/health`
7. 查 `/v1/monitoring/queues`、`/v1/monitoring/performance`、`/v1/monitoring/events`
8. 如果在开盘窗口，再继续做 15 到 30 分钟 live canary

这套顺序的目标很明确：先判断“系统是否活着”，再判断“链路是否推进”，最后才讨论“策略和交易是否合理”。

## 7.1 运行数天后的延后门禁审计

以下统计**不要在刚上线、样本不足时就做结论**。建议至少运行 3 到 5 天，再执行：

```powershell
python -m src.ops.cli.pipeline_gate_audit --environment live --days 5
```

常用变体：

```powershell
python -m src.ops.cli.pipeline_gate_audit --environment live --days 7 --focus session_transition_cooldown,quote_stale,intrabar_synthesis_stale,intrabar_synthesis_unavailable
```

```powershell
python -m src.ops.cli.pipeline_gate_audit --environment live --days 7 --json
```

该 CLI 的目标不是判“策略好坏”，而是回答：

- 最近 N 天哪些 gate family 拦截最多
- `session_transition_cooldown / quote_stale / intrabar_synthesis_*` 各自拦了多少真实事件
- 拦截主要集中在哪些 timeframe / source（`signal_filter` vs `execution`）
- 每天的门禁分布是否稳定，还是只集中在某一两次启动窗口

## 8. 多实例部署补充说明

- 共享基线配置放在 `config/*.ini`。
- 实例覆盖配置放在 `config/instances/<instance>/*.ini` 与同目录 `.local.ini`。
- 实例目录只允许承载实例级配置：`mt5.ini`、`market.ini`、`risk.ini`。
- 当前环境由 `topology group` 唯一决定，`mt5.ini` 只描述账户连接参数。
- `app.ini`、`db.ini`、`signal.ini`、`topology.ini` 等共享配置不会从实例目录加载。
- `main` 实例只负责共享计算与统一观测；`worker` 只负责单账户执行与本地风控。
- 若主账户也交易，应让该实例同时具备共享计算面和本地 `AccountRuntime`，但它仍只对自己的账户负责。
- 若启用 `intrabar_trading.enabled=true`，必须保证 `signal.ini[intrabar_trading.trigger]` 中实际会用到的 parent TF，其 child TF 已包含在 `app.ini[trading].timeframes` 里；当前系统已对这条合同做 fail-fast 校验，不能再通过 `config/instances/<instance>/app.local.ini` 之类的实例级覆盖去补 `app.ini` 共享时间框架。
补充约束：

- 当前每条日志都会自动带 `environment / instance / role` 前缀，控制台与文件日志口径一致。
- `main` 与 `worker` 即使由 supervisor 同时拉起，也必须优先按 `data/logs/<instance>/` 分实例判读，不应再把混合控制台输出当成唯一事实源。
