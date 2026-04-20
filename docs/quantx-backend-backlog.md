# QuantX 对接后端 Backlog

> 更新日期：2026-04-20
> 目标：为 `QuantX` 前端交易管理控制台提供稳定、可审计、可检索、可控制的后端能力。
> 约束：前端允许降级，但不应长期依赖 mock 近似数据替代核心审计事实。

---

## 0. 推荐端点选用顺序（前端必读，避免重叠困扰）

按场景选 **唯一推荐端点**。重叠的旧端点已标 `deprecated=true`（OpenAPI + metadata.deprecation），保留 1 个月兼容期（2026-06-01 后下线）。

| 场景 | 推荐端点 | 替代/废弃端点 | 备注 |
|------|---------|------------|------|
| Execution 工作台一站读 | `GET /v1/execution/workbench?account_alias=X` | — | 9 块 contract（execution/risk/positions/orders/pending/exposure/events/relatedObjects/marketContext/stream） |
| 持仓快照 | `workbench.positions` 块 | ⚠️ `/v1/positions`（已弃用） | workbench 块带 status_counts / positions_updated_at / source_kind |
| 挂单快照 | `workbench.orders` 块 | ⚠️ `/v1/orders`（已弃用） | 同上 |
| 直查 MT5 实时持仓（绕过 service 缓存） | `/v1/account/positions` | — | 与上面数据视角不同，**保留**用于运维直查 |
| 直查 MT5 实时挂单 | `/v1/account/orders` | — | 同上保留 |
| 实时事件订阅 | `GET /v1/trade/state/stream` | — | SSE，按 envelope.changed_blocks 局部刷 workbench |
| 历史信号查询 + admission 排序 | `GET /v1/signals/recent?sort=priority_desc&actionability=blocked` | — | actionability/guard_reason_code/priority 字段（P9 Phase 1.5） |
| 策略级聚合诊断 | `GET /v1/signals/diagnostics/strategy-audit` | ⚠️ 拼接 `/strategy-conflicts` + `/outcomes/winrate` + `/aggregate-summary`（旧做法） | P0.3 单端点交付（2026-04-20） |
| Trace 详情抽屉 | `GET /v1/trade/trace/{signal_id}` | — | TradeTraceView 含 timeline / facts / related_signals / related_trade_audits / related_pipeline_events |
| Mutation 已提交回显 | 任意 mutation 端点（统一 `MutationActionResultBase` 字段） | — | accepted/status/action_id/audit_id/effective_state... 共 13 字段 |

---

---

## 1. 背景

`QuantX` 的定位不是展示页，而是 `MT5Services` 的前端交易管理控制台。它需要覆盖四类核心能力：

1. **可看**：实时看到系统健康、交易态、信号态、研究态。
2. **可查**：按时间窗检索近期信号记录、策略记录、交易审计、trace 链路。
3. **可控**：安全地执行 runtime mode、closeout、手工平仓、撤单、回测触发等操作。
4. **可追责**：任何自动交易与人工干预都能回溯“为什么触发、谁触发、最后发生了什么”。

当前前端已经具备：

- BFF 代理
- mock / degraded / needs_backend 降级机制
- 审计中心时间窗和过滤器
- 执行控制页
- 研究 / paper trading / studio / config 骨架

因此后端下一阶段的重点不是“再多加几个零散接口”，而是把审计和控制闭环做成稳定读模型。

---

## 2. 总体原则

### 2.1 前端不直接消费零散底层事实

后端应尽量输出适合前端直接消费的读模型，避免前端用多个接口做近似拼接。

### 2.2 所有列表接口统一支持时间范围与分页

审计类接口至少统一支持：

- `from`
- `to`
- `page`
- `page_size`
- `sort`
- 领域过滤参数，如 `symbol/timeframe/strategy/status/trace_id/signal_id`

### 2.3 关键对象必须能互相跳转

至少保证以下 ID 贯通：

- `signal_id`
- `trace_id`
- `ticket / order_ticket / position_ticket`
- `run_id`
- `session_id`

### 2.4 危险操作必须有预检查、幂等与审计

所有执行类 mutation 至少返回：

- `accepted`
- `action_id` 或 `audit_id`
- `status`
- `message`
- `error_code`
- `effective_state`

并支持：

- `actor`
- `reason`
- `idempotency_key`

---

## 3. P0：必须先补的能力

> 目标：让审计中心和执行控制页从“可演示”变成“可日常使用”。

### P0.1 审计列表接口统一时间范围与分页

#### 当前已有

- `GET /v1/signals/recent`
- `GET /v1/trade/command-audits`

#### 当前问题

- 前端只能拿“最近 N 条”再做时间截断
- `24h/7d/30d` 审计结果不稳定
- 无法可靠导出当前筛选结果

#### 需要补齐

- `GET /v1/signals/recent`
  - 建议增加：
    - `from`
    - `to`
    - `page`
    - `page_size`
    - `symbol`
    - `timeframe`
    - `strategy`
    - `scope`
    - `status`
    - `direction`
- `GET /v1/trade/command-audits`
  - 建议增加：
    - `from`
    - `to`
    - `page`
    - `page_size`
    - `symbol`
    - `command_type`
    - `status`
    - `signal_id`
    - `trace_id`
    - `actor`

#### 验收标准

- 前端切换 `24h / 7d / 30d` 时结果稳定且可复现
- 导出 `CSV/JSON` 与当前列表筛选一致
- 不再需要前端靠 mock 或近似截断填补长时间窗

---

### P0.2 Trace 列表与搜索接口

#### 当前已有

- `GET /v1/trade/trace/{signal_id}`
- `GET /v1/trade/trace/by-trace/{trace_id}`

#### 当前问题

- 只能精确查
- 无法按时间窗、symbol、strategy 查近期异常链路
- 前端只能用 signals + audits + pipeline 自行聚合 trace 目录

#### 建议新增

- `GET /v1/trade/traces`

建议参数：

- `from`
- `to`
- `page`
- `page_size`
- `symbol`
- `timeframe`
- `strategy`
- `status`
- `signal_id`
- `trace_id`

建议返回字段：

- `trace_id`
- `signal_id`
- `symbol`
- `timeframe`
- `strategy`
- `status`
- `started_at`
- `last_event_at`
- `event_count`
- `last_stage`
- `reason`

#### 验收标准

- 前端可直接按时间窗加载 trace 目录
- 可从异常信号、异常命令审计跳回 trace 列表
- 可按 trace 状态定位阻断、失败、人工接管链路

---

### P0.3 策略审计聚合接口 — ✅ 已交付（2026-04-20）

**端点**：`GET /v1/signals/diagnostics/strategy-audit`
- 参数：`symbol / timeframe / strategy / scope / hours / limit / *_warn_threshold`
- 返回字段（每策略）：`strategy / category / signals / actionable_signals / hold_count / blocked_count / conflict_count / hold_rate / blocked_rate / conflict_rate / avg_confidence / win_rate / last_signal_at / recent_issue / status / warnings`
- 实现：[src/signals/analytics/diagnostics.py](src/signals/analytics/diagnostics.py) `build_strategy_audit_report()`
- 测试：14 单元测试 + 3 路由测试

---

### P0.3 策略审计聚合接口（历史描述）

#### 当前已有

- `GET /v1/signals/summary`
- `GET /v1/signals/diagnostics/daily-report`
- `GET /v1/signals/diagnostics/strategy-conflicts`
- `GET /v1/signals/outcomes/winrate`
- `GET /v1/admin/performance/strategies`

#### 当前问题

- 前端能拼出近似策略表，但不是标准事实源
- 无法稳定看到策略级 `signal / actionable / hold / conflict / blocked / winrate`

#### 建议新增

- `GET /v1/signals/diagnostics/strategy-audit`

建议参数：

- `from`
- `to`
- `symbol`
- `timeframe`
- `strategy`

建议返回字段：

- `strategy`
- `category`
- `signals`
- `actionable_signals`
- `hold_count`
- `blocked_count`
- `conflict_count`
- `hold_rate`
- `conflict_rate`
- `avg_confidence`
- `win_rate`
- `last_signal_at`
- `recent_issue`
- `status`

#### 验收标准

- 前端策略记录页不再依赖 BFF 大量拼接
- 能稳定显示近期异常策略和策略质量变化

---

### P0.4 审计持久化与排序一致性

#### 当前问题

- 审计类接口如果只依赖短期缓存或最近记录，前端时间窗就会失真
- 不同读接口排序与时间字段可能不一致

#### 需要保证

- 信号记录可持久化检索
- 交易命令审计可长期保留
- pipeline / trace 事件至少在约定 retention 内可查询
- 所有审计读接口默认按 `recorded_at desc`

#### 验收标准

- 同一时间窗重复查询返回顺序一致
- `signal_id / trace_id / ticket` 交叉检索时不会丢链

---

## 4. P1：让控制闭环真正可用

> 目标：让执行控制页不是“发命令按钮集合”，而是安全的操作台。

### P1.1 统一危险操作返回模型 — ✅ 已交付（2026-04-20）

**基类**：[src/api/schemas.py](src/api/schemas.py) `MutationActionResultBase`（13 字段）
- 字段：`accepted / status / action_id / command_id / audit_id / actor / reason / idempotency_key / request_context / message / error_code / recorded_at / effective_state`
- 子类按需追加领域字段（如 `TradeControlUpdateView` 加 `trade_control` + `executor`；`RuntimeModeUpdateView` 加 `runtime_mode` + `trading_state`）
- 跨域统一：trade_routes / monitoring_routes 都从 schemas 导入，不再两份定义

**前端建议**：写一套通用 mutation handler 解析 13 字段；按 `error_code` 决定 toast/重试。

---

### P1.1 历史描述

#### 适用接口

- `POST /v1/trade/control`
- `POST /v1/trade/runtime-mode`
- `POST /v1/trade/closeout-exposure`
- `POST /v1/close`
- `POST /v1/cancel_orders`
- `POST /v1/monitoring/pending-entries/{signal_id}/cancel`
- `POST /v1/backtest/run`

#### 建议统一返回

- `accepted`
- `status`
- `action_id`
- `audit_id`
- `message`
- `error_code`
- `effective_state`
- `recorded_at`

#### 建议统一请求

- `actor`
- `reason`
- `idempotency_key`
- `request_context`

#### 验收标准

- 前端确认操作后，能立刻拿到可追踪的 `audit_id` 或 `action_id`
- 后续在交易审计中能反查该次操作

---

### P1.2 统一交易态实时流

详细接口草案见：

- `docs/design/quantx-trade-state-stream.md`

#### 当前问题

- 持仓、挂单、告警、runtime mode 主要靠轮询
- 审计页与执行页长期挂着时会浪费请求

#### 建议新增

- `GET /v1/trade/state/stream`

建议事件类型：

- `runtime_mode_changed`
- `trade_control_changed`
- `position_changed`
- `order_changed`
- `pending_entry_changed`
- `alert_raised`
- `alert_resolved`
- `closeout_started`
- `closeout_finished`

#### 验收标准

- 前端执行页与总览页不需要高频轮询也能保持近实时
- SSE 具备 heartbeat 和重连语义

---

### P1.3 Trace 详情字段标准化 — ✅ 已交付

**端点**：`GET /v1/trade/trace/{signal_id}` / `GET /v1/trade/trace/by-trace/{trace_id}`

返回 [src/api/trade_routes/view_models.py](src/api/trade_routes/view_models.py) `TradeTraceView`，含：
- `signal_id / trace_id / found`
- `identifiers`（signal_ids / request_ids / trace_ids / operation_ids / order_tickets / position_tickets 索引）
- `summary`（stages / pipeline_event_counts / command_counts / pending_status_counts / position_status_counts / admission）
- `timeline[]`（按时间序的 stage 事件）
- `graph`（nodes + edges 的关联图）
- `facts`（聚合事实）
- `related_signals` / `related_trade_audits[]` / `related_pipeline_events[]`

前端一次请求拿全 trace 上下文，无需多端点拼接。

---

### P1.3 历史描述

#### 当前问题

- `trade trace`、`signal diagnostics trace`、`pipeline trace` 之间字段口径可能不一致

#### 需要统一

- `trace_id`
- `signal_id`
- `stage`
- `event_type`
- `status`
- `summary`
- `details`
- `recorded_at`
- `source`

并能返回：

- `timeline`
- `facts`
- `related_signals`
- `related_trade_audits`
- `related_pipeline_events`

#### 验收标准

- 前端 trace 抽屉只请求 1 次即可拿到完整上下文

---

## 5. P2：研究、Paper Trading 与配置治理

> 目标：让研究与运行时不再割裂。

### P2.1 Walk-Forward 与 Recommendation 明细

#### 当前问题

- 前端研究页已预留，但明细接口还不够完整

#### 需要补齐

- walk-forward 分阶段结果
- evaluation summary
- recommendation 生命周期
- recommendation 与 backtest run / paper session 的关联

#### 建议接口

- 保持现有 `/v1/backtest/*`
- 补齐详细读取接口，优先不改已有基本路由结构

---

### P2.2 Paper Trading 与回测 run 关联追踪

#### 需要保证

- `paper session` 能关联来源 `backtest_run_id`
- `paper trade` 能反查建议来源、策略来源或 signal 来源

#### 验收标准

- 前端能回答“这轮模拟交易是从哪次回测建议启动的”

---

### P2.3 配置写入与热更新审计

#### 当前状态

- 配置页当前只读

#### 如果后续开放前端编辑

必须同时具备：

- mutation 接口
- 参数校验
- actor / reason
- 热更新结果
- 审计落库
- 回滚能力

---

## 6. 前端最需要的标准字段

以下字段建议在相关读模型中统一命名，不要同义词并存：

- `signal_id`
- `trace_id`
- `run_id`
- `session_id`
- `symbol`
- `timeframe`
- `strategy`
- `direction`
- `scope`
- `status`
- `confidence`
- `reason`
- `summary`
- `details`
- `ticket`
- `actor`
- `recorded_at`
- `created_at`
- `updated_at`

时间字段统一使用 ISO8601。

---

## 7. 推荐交付顺序

### 第一批

1. `signals/recent` 时间范围 + 分页
2. `trade/command-audits` 时间范围 + 分页
3. `trade/traces` 列表与搜索

### 第二批

1. `strategy-audit` 聚合接口
2. `trade/state/stream` 实时流
3. trace 详情读模型标准化

### 第三批

1. walk-forward 详细结果
2. recommendation 生命周期明细
3. 配置 mutation + 审计

---

## 8. 前端验收口径

当以下能力全部满足时，可认为 `QuantX` 已从“骨架 + mock”进入“可日常使用”：

1. 审计中心可稳定查看 `24h / 7d / 30d` 的信号、策略、交易、trace。
2. 能从任意一条信号或交易审计定位到完整 trace。
3. 执行控制页所有危险操作都能返回审计回写。
4. 总览页和执行页能通过实时流感知关键状态变化。
5. 研究页能把回测、recommendation、paper trading 串起来。

---

## 9. 非目标

以下内容不作为本阶段阻塞项：

- RBAC / SSO
- 多账户多租户
- 面向公网的权限系统
- 完整的前端可编辑配置中心

当前阶段优先服务单实例、单账户、内网操作者场景。
