# QuantX Canonical IA 读模型（P10 落地）

## 背景与目标

P9 已交付 `/v1/execution/workbench` 与 SSE envelope schema 1.1，但 QuantX 前端
`Cockpit / Accounts / Trades / Intel / Lab / Audit` 六个顶层工作区仍依赖多路摘要 API +
本地启发式拼接。P10 补齐**单端点 canonical 读模型**，彻底消除前端本地聚合。

---

## 7 个新端点（全部 P10 落地）

| 端点 | 子模块 | 读模型 | 责任 |
|------|-------|--------|------|
| `GET /v1/cockpit/overview` | P10.1 | `CockpitReadModel` | 7 块总控台（decision / triage_queue / market_guard / data_health / exposure_map / opportunity_queue / safe_actions）+ 跨账户 contributors[] |
| `GET /v1/intel/action-queue` | P10.2 | `IntelReadModel` | guard-aware 行动队列，`account_candidates[]` 派生自 `signal_config.account_bindings` 反向索引 |
| `GET /v1/trades/workbench` | P10.3 | `TradesWorkbenchReadModel.build_workbench` | 基于 `trade_outcomes` 的交易记录分页 + summary + freshness |
| `GET /v1/trades/{trade_id}` | P10.3 | `TradesWorkbenchReadModel.build_trade_detail` | 6 维 canonical 视图（plan_vs_live / lifecycle / risk_review / receipts / evidence / linked_account_state）|
| `GET /v1/trade/command-audits` | P10.4 | `TradingQueryService.command_audit_page` | 支持 `audit_id/action_id/idempotency_key/signal_id/trace_id/actor` 过滤 |
| `GET /v1/trade/command-audits/{audit_id}` | P10.4 | `TradingQueryService.command_audit_detail` | 单条 audit + `linked_operator_command`（反查 operator_commands） |
| `GET /v1/lab/impact` | P10.5 | `LabImpactReadModel` | WF snapshots + recommendations + demo_validation_windows + experiment_links 贯通读（ADR-010 后 `paper_sessions` 字段重命名） |

---

## 职责边界（ADR-006 合规）

```
┌────────────────────────────────────────────────────────────┐
│ API 路由层（trade_routes / cockpit_routes / intel_routes / │
│   lab_routes / execution_routes）                          │
│   - 仅做参数归一化 + ApiResponse 包装                       │
│   - 不读任何 _ 私有属性                                     │
└───────────────────┬────────────────────────────────────────┘
                    │ DI (deps.py, 每请求构造)
                    ▼
┌────────────────────────────────────────────────────────────┐
│ ReadModels（src/readmodels/）                               │
│   - 纯组合 repo / service / 其他 readmodel                  │
│   - 不持有 mutable state（per-request 构造）                │
│   - 字段契约统一 snake_case + ISO8601 + compute_source_kind │
└───────────────────┬────────────────────────────────────────┘
                    │
                    ▼
┌────────────────────────────────────────────────────────────┐
│ 数据源                                                      │
│   - Repository（TimescaleDB 跨账户单库聚合）                │
│   - SignalModule（recent_signal_page +                      │
│     strategy_account_bindings 公共方法）                    │
│   - BacktestRuntimeStore（内存）+ BacktestRepository（DB） │
└────────────────────────────────────────────────────────────┘
```

### 跨账户聚合前提

Cockpit 的 `decision / triage_queue / exposure_map` 依赖**同 environment 下所有实例
共享同一 TimescaleDB 数据库**（`db.live` 或 `db.demo`）。live 实例不会读到 demo 数据，
因为 live/demo 用两个独立 database 名物理隔离。

→ 任何 live 实例都能 serve `/v1/cockpit/overview`，返回该 environment 下的全账户视图。
  Demo 实例同理。跨 environment 的聚合由前端 BFF 完成。

---

## 公用基础设施

### 字段契约（P10.6 审计通过）
- 所有时间字段 snake_case + ISO8601（`observed_at / data_updated_at / recorded_at /
  generated_at / started_at / approved_at / applied_at / rolled_back_at`）
- 所有标识字段 snake_case（`trade_id / signal_id / trace_id / audit_id / action_id /
  idempotency_key / rec_id / run_id / session_id / experiment_id / command_id /
  intent_id / source_run_id / recommendation_id / source_backtest_run_id`）
- **零 camelCase 同义词**（grep hits = 0）

### Freshness helper（`src/readmodels/freshness.py`）
五个读模型（cockpit / intel / trades_workbench / lab_impact / workbench）共用：
- `age_seconds(ts)` 支持 datetime / ISO str / None，naive dt 按 UTC 处理
- `freshness_state(age, stale_after, max_age)` 三态 fresh/warn/stale/unknown
- `build_freshness_block(...)` 9 字段统一 payload

### Source kind 聚合（`src/readmodels/workbench.compute_source_kind`）
跨端点统一规则：
- 全部块 `source_kind=native` → 顶层 `source.kind=live`
- 任意块 `source_kind=fallback` / `available=false` → `source.kind=hybrid`
- `source_kind=static`（如 stream 块）不参与判定

Cockpit / Workbench 都复用此函数，Lab/Intel 默认 live（无降级场景）。

### Bindings accessor 单路径（P0.2）
`SignalModule.strategy_account_bindings()` → `{strategy: [account_alias]}` 反向索引。
由 `factories/signals.py` 在构建时通过 `signal_module.set_account_bindings()` 注入。
**Intel / Cockpit / 未来读模型**都走这一条路径，不再各自读 `signal_config`。

---

## 关键实现决策

### P10.1 Cockpit exposure_map 跨账户聚合
SQL 模式（`trading_state_repo.aggregate_open_positions_by_account_symbol`）：
```sql
WITH latest AS (
    SELECT DISTINCT ON (account_key, position_ticket) ...
    FROM position_runtime_states
    ORDER BY account_key, position_ticket, updated_at DESC
)
SELECT account_alias, account_key, symbol, direction,
       SUM(volume), COUNT(*)
FROM latest WHERE status = 'open'
GROUP BY account_alias, account_key, symbol, direction
```
- latest-per-ticket 去重后按 (account, symbol, direction) 聚合
- contributors[].weight = account_volume / bucket.gross_exposure

### P12-1 Cockpit exposure_map 账户×品种风险矩阵（2026-04-22）
在 symbol-major `entries[]` 之外新增账户-major 视图 `risk_matrix[]`（frontend 账户动作台风险热力图用）：

- `exposure_map.mode = "account_symbol"` — schema 版本标签
- `exposure_map.risk_matrix[]` — 每账户一行，`cells[]` 按 symbol 聚合 buy/sell
  - `gross_exposure = buy_volume + sell_volume`（open 持仓）
  - `net_exposure = buy_volume - sell_volume`
  - `pending_exposure` — `pending_order_states` 中 status ∈ {'placed', 'pending'} 的 volume 之和
  - `risk_score = cell.gross_exposure / account.total_risk`（账户内占比，0~1）
- 新增 SQL `aggregate_pending_orders_by_account_symbol()` 补 pending 数据
- 保留现有 `entries[]`（向后兼容），matrix 与 entries 并存，不破坏前端旧消费
- 一期 `risk_score` 只反映**账户内**风险集中度，未叠加 margin_guard_state / margin_level；前端若需绝对风险可结合 triage_queue.metrics 使用

### P10.1 Cockpit opportunity_queue 委托（P1.3 修复）
Cockpit 不再自己查 actionable signals，而是内部持有一个 `IntelReadModel` 实例，
`_build_opportunity_queue()` 直接调用 `intel.build_action_queue(page_size=10)`。
消除两个读模型重复查 `recent_signal_page` 的问题。

### P10.3 TradesWorkbench.build_trade_detail 是纯适配器（P2.1 声明）
该方法**不做任何独立 DB 查询**，仅消费 `TradingFlowTraceReadModel.trace_by_signal_id()`
的 `facts` 字段重新分组。契约守护测试 `test_build_trade_detail_is_a_pure_trace_adapter`
通过 ExplodingRepo 证明不会调用 signal_repo 任何方法。

### P10.3 linked_account_state 数据源澄清（P2.2 修复）
旧实现扫 `pipeline_events.payload.account_risk_state / account_snapshot` —— 全代码库
没有任何地方写入这两个 key，永远是空壳。改为从真实可用的事实源提取：
- `auto_executions` → 该 signal 被哪些账户执行（account_alias / account_key / intent_id）
- `admission_reports` → 该 signal 的 admission 阶段快照（含 account_state 字段）

返回 payload 显式 `note` 字段声明数据源范围和未来扩展路径。

### P10.5 Lab Impact DB fallback（P0.1 修复）
内存 `BacktestRuntimeStore` 上限 50 条 + 重启丢数据。`LabImpactReadModel` 合并两路：
1. 内存 store 先扫（快，覆盖最近写入）
2. DB `backtest_recommendations` 表回读（重启后兜底）
3. 按 `rec_id` 去重，内存优先（更新鲜）
4. 按 `created_at DESC` 截断到 limit

### P10.5 PaperSession schema 迁移（ADR-010 SUPERSEDED）

**原设计**：`paper_trading_sessions` 表加 3 列 + 3 索引（`source_backtest_run_id` / `recommendation_id` / `experiment_id`），供 Lab impact 反查。

**现状（ADR-010, 2026-04-25）**：`paper_trading_sessions` 表已 DROP，`paper_session_id` 字段从 `experiments` 表移除。Lab impact 改读 `db.demo.trade_outcomes`，按 `(strategy, timeframe, time_window)` 聚合为 `demo_validation_windows`（仍由 `LabImpactReadModel` 提供）。详见 `docs/design/adr.md` ADR-010。

---

## 端到端验证（2026-04-20 通过）

启动 `live-main` 后 curl 7 端点：

| 端点 | 状态 | 字段数 | 备注 |
|------|------|-------|------|
| `/v1/cockpit/overview` | 200 | accounts=2, decision.verdict=healthy, source=live, triage 2 条, opportunity 1 条 | 委托 Intel 生效 |
| `/v1/intel/action-queue` | 200 | 1 条 actionable | - |
| `/v1/trades/workbench` | 200 | records=0 | trade_outcomes 表空（2026-04-20 时点；ADR-010 后 demo-main 真实下单数据进 db.demo.trade_outcomes） |
| `/v1/trades/{unknown}` | 404 | error_code=not_found | - |
| `/v1/trade/command-audits` | 200 | - | - |
| `/v1/trade/command-audits/{unknown}` | 404 | error_code=not_found | - |
| `/v1/lab/impact` | 200 | **paper_sessions=18**（2026-04-20 时点字段名；ADR-010 后已重命名为 `demo_validation_windows`，数据源改 `db.demo.trade_outcomes`）, experiment_links=1 | DB 回读验证通过 |

---

## 后续工作

- **linked_account_state point-in-time snapshot**：需扩展 `TradingFlowTraceReadModel`
  接入 `trading_state_repo.fetch_account_risk_states(account_alias, from, to)`，
  在 trace_payload.facts 下增 `linked_account_snapshots` 字段，TradesWorkbench 适配器
  自动透传（不违反 P2.1 纯适配器契约）。
- **Cockpit environment 隔离守护**：TimescaleDB schema 目前无 `environment` 列，
  跨 env 混配风险依靠 `db.live` / `db.demo` 物理库隔离防护。未来可在 cockpit 读模型入
  `runtime_identity.environment` 参数，二次过滤，防止误连不同 database。
- **backtest_runs DB 持久化**：Lab impact 的 `walk_forward_snapshots` 仍依赖内存
  `BacktestRuntimeStore`，需要把 `WalkForwardResult` 持久化到 DB（P10 plan 超出范围）。
