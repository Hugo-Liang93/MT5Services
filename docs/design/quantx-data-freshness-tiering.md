# QuantX 数据时效性分级与推送通道契约

> 更新日期：2026-04-20
> 目的：从量化交易分析师视角划分前后端数据的时效性等级，明确每类数据走 SSE 推送、HTTP 轮询还是按需查询，避免"一律实时"或"一律轮询"两种极端。
> 关联文档：
> - `docs/design/quantx-trade-state-stream.md` — SSE 通道协议
> - `docs/quantx-backend-backlog.md` — 前端读侧缺口清单
> - `TODO.md` P9 — 后端落地任务

---

## 1. 背景与目标

QuantX 当前对数据时效性的处理方式是"全部 5-15s 轮询"，这造成两类问题：

1. **关键状态延迟**：操作员手动 halt 后，前端 30s 才更新，期间他可能误以为系统仍开放交易，做出错误判断。
2. **无效流量**：策略胜率、历史信号这类几分钟才变一次的数据，每 15s 轮询是浪费。

本契约把数据按"决策影响半径 / 失活后果 / 变更频率 / 生产成本"四维度分成 5 级（T0-T4），并为每级指定推送通道、刷新频率、降级路径，作为 QuantX 前端与 MT5Services 后端的共同遵守。

---

## 2. 分级原则

| 维度 | 含义 |
|------|------|
| 决策影响半径 | 这条数据变化是否会让下一秒的交易决策不同 |
| 失活后果严重度 | 延迟会导致错单 / missed trade / 仅 UX 退化 |
| 变更频率 | 数据本身多快会变 |
| 生产成本 | 实时推送的工程 / 运行成本 |

5 级 Tier 总览：

| Tier | 名称 | 时效目标 | 通道 | 失活后果 |
|------|------|---------|------|---------|
| **T0** | Critical Realtime | < 1s | SSE 强制 | 错单 / 风险敞口 |
| **T1** | Operational Realtime | 1-5s | SSE 优先 + 5s 轮询兜底 | 操作回显延迟 / 状态混乱 |
| **T2** | Tactical Updates | 5-15s | SSE throttle 推 + 5-15s 轮询 | 视图陈旧但不影响决策 |
| **T3** | Monitoring | 30s-1min | 纯轮询 | 仪表盘陈旧 |
| **T4** | Reference | 按需触发 | 用户主动查询 | 无 |

---

## 3. 数据 → Tier 完整映射

### 3.1 T0 — Critical Realtime（< 1s）

**特征**：状态切换会立即改变下一笔交易能否被准入；延迟 = 错单或风险敞口。

| 数据 | 来源组件 | 触发频率 | 延迟容忍 |
|------|---------|---------|---------|
| `circuit_open` 切换 | TradeExecutor 熔断 | 罕见 | < 500ms |
| `runtime_mode` 切换（halt/resume/observe） | ModeController | 操作员触发 | < 500ms |
| `close_only_mode` 切换 | TradeControl | 操作员触发 | < 500ms |
| `auto_entry_enabled` 切换 | TradeControl | 操作员触发 | < 500ms |
| Margin level 跌破 critical 阈值 | AccountSnapshot / RiskProjector | 行情驱动 | < 1s |
| 强平事件 | TradeExecutor | 罕见 | < 1s |
| Quote stale 状态进入/退出 | MarketDataService | 行情中断 | < 2s |
| `should_block_new_trades` 切换 | RiskProjector | 风控触发 | < 1s |

### 3.2 T1 — Operational Realtime（1-5s）

**特征**：操作员需要"立即看到回显"，但延迟不会错单，只损 UX。

| 数据 | 来源 | 触发频率 |
|------|------|---------|
| 持仓开/平/部分平/SL 触发/TP 触发 | PositionManager | 中频（每笔交易） |
| 挂单创建/触发/取消/修改/过期 | PendingOrderManager | 中频 |
| pending entry 激活/超时 | PendingEntryManager | 中频 |
| 信号生成（confirmed bar close 后） | SignalRuntime | 每 bar（M5≈12/h, H1≈1/h, D1≈1/d） |
| 命令审计追加（操作员触发的） | OperatorCommandService | 操作员触发 |
| Alert raised / resolved | HealthMonitor | 不规则 |
| Closeout started / finished | ExposureCloseout | 罕见 |

### 3.3 T2 — Tactical Updates（5-15s）

**特征**：频繁变化的数字，单点波动不影响决策；前端用于显示趋势。

| 数据 | 来源 | 推荐推送策略 |
|------|------|-------------|
| Floating P&L 数字 | PositionManager | 不进 SSE，由 workbench 5s 轮询 |
| Quote 价格 / spread | MarketDataService | 不进 SSE |
| Exposure 重算（symbol 聚合） | WorkbenchReadModel | 仅持仓/挂单变化时重算 |
| Account equity / free_margin | AccountSnapshot | workbench 5s 轮询 |
| `daily_realized_pnl` | RiskProjector | workbench 15s 轮询 |
| Intrabar coordinator 状态 | IntrabarTradeCoordinator | workbench 5s 轮询 |

**关键决策**：T2 数据不进 T0/T1 的 SSE 通道，避免事件风暴。Quote 数字若进 SSE，按 1s 心跳频次会推爆 envelope，前端每秒 diff 视图反而卡顿。

### 3.4 T3 — Monitoring（30s-1min）

**特征**：仪表盘类，操作员观察用，对决策无影响。

| 数据 | 推荐端点 | 推荐轮询 |
|------|---------|---------|
| Executor today_stats | `/v1/executor/status` | 30s |
| 策略级 signals 计数 | `/v1/signals/summary` | 30s |
| HealthMonitor 各组件状态 | `/v1/admin/health` | 60s |
| 经济日历近 24h 事件 | `/v1/calendar/events` | 60s |
| Pipeline trace 概览 | `/v1/trade/audit/pipeline/summary` | 60s |
| Strategy audit 聚合 | `/v1/signals/diagnostics/strategy-audit` | 60s |

### 3.5 T4 — Reference（按需触发）

**特征**：用户主动查时执行；无后台轮询。

- 历史信号列表（分页 + filter）`/v1/signals/recent?from=&to=&page=`
- 命令审计历史 `/v1/trade/command-audits?from=&to=&page=`
- Trace 列表 `/v1/trade/traces?from=&to=&page=`
- Trace 详情抽屉（点击触发）`/v1/trade/trace/{signal_id}`
- Backtest 结果 `/v1/backtest/runs/{run_id}`
- Walk-forward 分析 `/v1/backtest/walk-forward/{run_id}`
- Strategy 冲突诊断 `/v1/signals/diagnostics/strategy-conflicts`
- Daily report `/v1/signals/diagnostics/daily-report`

---

## 4. 推送通道矩阵

| 通道 | 覆盖 Tier | 当前实现 | 待优化 |
|------|----------|---------|-------|
| SSE `/v1/trade/state/stream` | T0 + T1 | ✅ 12 事件已就绪 | 加 `tier` / `changed_blocks` / `snapshot_version` |
| HTTP `/v1/execution/workbench` | T2 主 + T3 兜底 | ❌ 待新增（plan Phase 1） | 5s 轮询 + ETag |
| HTTP 各 T3 端点 | T3 | ✅ 已有 | 加 `Cache-Control` + `since=<ts>` 增量 |
| HTTP 列表/详情 | T4 | 🟡 部分 | 补 `from/to/page_size`（QuantX backlog P0.1） |

**为什么不引入第二条 SSE（如 quote stream）**：

1. Quote 数字属 T2，5s throttle 已够用
2. 双 SSE 增加前端复杂度（顺序协调 / 重连逻辑 × 2）
3. 用 workbench `marketContext` 块统一即可
4. 后端维护成本：每条 SSE 都要独立的 sequence / state_version / Last-Event-ID

---

## 5. QuantX 工作台 → Tier 映射

```
              T0          T1                T2                  T3
Cockpit       runtime     最新一笔交易      总 equity           今日统计
              风控告警                       总 floating P&L

Accounts      单账户告警  单账户最新交易    equity / margin     账户健康摘要

Trades        —           新交易            —                   T4 历史 filter
                          命令追加

Execution     风控开关    持仓变化           exposure            today_stats
              circuit     挂单变化           quote 价格
              quote_stale pending 变化       floating P&L

Research      —           —                 —                   T4 backtest / WF
```

**前端订阅策略**：

| 工作台状态 | SSE | Workbench 轮询 | T3 端点轮询 |
|----------|-----|--------------|----------|
| Execution 展开（前台） | ✅ 建立 | 5s | 30s |
| Cockpit 展开（前台） | ✅ 建立 | 15s | 60s |
| Accounts 展开（前台） | ✅ 建立 | 15s | 60s |
| Trades / Research 展开 | ❌ | ❌ | T4 触发性 |
| 标签后台（hidden） | 保留连接 | 30s | 暂停 |

---

## 6. Freshness Contract

### 6.1 字段定义

| 字段 | 含义 | 来源 |
|------|------|------|
| `state_updated_at` | 业务状态实际发生变化的时间戳 | 组件自身（持仓/订单/风控） |
| `observed_at` | 读模型快照生成时间戳 | 读模型组装入口 |
| `tier` | T0 / T1 / T2 / T3 | 后端按本文档分级标注 |
| `source.kind` | live / hybrid / mock / fallback_applied | 端点声明 |
| `freshness_hints` | 推荐 stale 阈值 | 后端从 config 取 |

### 6.2 推荐阈值（后端默认，前端可覆盖）

```jsonc
"freshness_hints": {
  "tradability":   {"stale_after_seconds": 2,  "stale_threshold_seconds": 10,  "tier": "T0"},
  "risk":          {"stale_after_seconds": 5,  "stale_threshold_seconds": 30,  "tier": "T0"},
  "positions":     {"stale_after_seconds": 10, "stale_threshold_seconds": 60,  "tier": "T1"},
  "orders":        {"stale_after_seconds": 10, "stale_threshold_seconds": 60,  "tier": "T1"},
  "pending":       {"stale_after_seconds": 10, "stale_threshold_seconds": 60,  "tier": "T1"},
  "exposure":      {"stale_after_seconds": 15, "stale_threshold_seconds": 120, "tier": "T2"},
  "quote":         {"stale_after_seconds": 5,  "stale_threshold_seconds": 30,  "tier": "T2"},
  "events":        {"stale_after_seconds": 30, "stale_threshold_seconds": 300, "tier": "T3"}
}
```

### 6.3 前端三态规则

```
age = now - state_updated_at

age < stale_after_seconds         → fresh   (绿)
stale_after ≤ age < stale_thresh  → warn    (黄)
age ≥ stale_threshold_seconds     → stale   (红)
```

**T0 / T1 数据 stale 时**：前端必须**禁用涉及交易的操作按钮**（开仓 / 改单 / 撤单），并显示"等待数据刷新"提示。
**T2 / T3 数据 stale 时**：仅显示提示，不阻塞操作。

---

## 7. SSE Envelope 增强

当前 envelope（[stream.py:170](../../src/api/trade_routes/state_routes/stream.py#L170)）已含 `event_id / sequence / state_version / event_type`，本契约要求新增：

```json
{
  "event_id": "trade_state_00000128",
  "stream": "trade_state",
  "schema_version": "1.1",
  "event_type": "position_changed",
  "tier": "T1",                           // 新增：前端按 tier 决定刷新策略
  "entity_scope": "account",              // 新增：account / symbol / system
  "changed_blocks": ["positions", "exposure"],  // 新增：受影响的 workbench 块
  "snapshot_version": 912,                // 与 workbench.state_version 同步（替代 state_version）
  "recorded_at": "...",
  "sequence": 128,
  "account": "live-main",
  "symbol": "XAUUSD",
  "trace_id": "...",
  "signal_id": "...",
  "payload": { ... }
}
```

**新增字段意义**：
- `tier`：前端按 T0 优先级渲染，T2 可批处理
- `entity_scope`：前端判断是否影响当前视图（如 account 范围事件不影响 system 级 Cockpit）
- `changed_blocks`：精确告诉前端哪几个 workbench 块需要重拉，避免全量刷新
- `snapshot_version`：与 `/v1/execution/workbench` 返回的 `state_version` 同名同值，便于前端对账

---

## 8. 降级矩阵

| 故障 | 受影响 Tier | 降级路径 |
|------|----------|---------|
| SSE 断开 | T0 / T1 | 前端切 2s workbench 轮询（应急）→ 重连成功后回 5s |
| Workbench API 失败 | T2 + T3 兜底 | 退化为旧多路 API（`/v1/trade/state` + `/v1/account/positions` + ...），保留兼容 2 周 |
| MT5 quote 中断 | T0 quote_stale | tradability `verdict=blocked, reason=quote_stale` → 前端禁用开仓 |
| 单 worker 实例 down | 该账户全部 | BFF 标该 alias `unavailable`，其他账户正常 |
| 数据库写延迟 | T3 历史端点 | 临时返回内存最近窗 + `source.kind=hybrid` |
| 后端整体不可达 | 全部 | 前端展示最后一次成功快照 + 全局 stale banner |

---

## 9. 调用频率预算

### 9.1 单操作员场景

同时开 Cockpit + Execution + Trades + Research 四个工作台：

| 通道 | RPS |
|------|-----|
| 1 个 SSE 长连接（持久） | ~0（基本无开销） |
| Workbench 5s × 1（Execution 主调） | 0.20 |
| T3 端点 30s × 5（多个仪表盘） | 0.17 |
| T4 触发性（用户偶发查询） | ~0.05 |
| **合计** | **~0.42 RPS** |

### 9.2 多操作员 / 多账户

- 10 个操作员并发：4.2 RPS
- 多账户 BFF fanout × 3 账户：12.6 RPS

仍远低于压力线（FastAPI + uvicorn 单进程轻松扛 100+ RPS）。

### 9.3 前端硬约束

- 同账户 workbench 轮询间隔 ≥ 5s（Execution 前台）/ 15s（Cockpit 前台）/ 30s（标签后台）
- SSE 断线指数退避：1s → 2s → 5s → 10s → 30s（封顶 60s）
- 历史查询节流 ≥ 1s（防止快速翻页打爆）
- 同一资源连续重复请求 < 3 次/s 视为异常

---

## 10. 与既有 Plan 的衔接

`TODO.md` P9 的 5 个 Phase 在本契约下的角色定位：

| Phase | 角色 | Tier 关联 |
|-------|-----|----------|
| 1.4 tradability 原生 verdict | 把 T0 状态从 derived 升为 native | T0 核心 |
| 1.1-1.3 workbench 端点 | T2 主路径 + T3 兜底 + freshness 字段载体 | T2/T3 |
| 1.5 SignalRecord guard_reason_code | T1 推送 + T4 查询 | T1/T4 |
| 2.1 SSE envelope 扩展 | 加 `tier` / `changed_blocks` / `snapshot_version` | T0/T1 |
| 2.3 unified source 语义 | 跨端点 source.kind 一致 | 全 Tier |
| 3.x 后续 | freshness 阈值从 `config/freshness.py` 注入 | 全 Tier |

---

## 11. 实施 Checklist

### 11.1 后端

- [ ] `src/config/freshness.py` 定义各 tier 默认阈值（Pydantic 模型）
- [ ] `tradability_state_summary()` 加 `tier="T0"` + verdict + reason_code
- [ ] WorkbenchReadModel 顶层挂 `freshness_hints`，每块挂 `tier`
- [ ] SSE envelope `next_stream_envelope()` 加 `tier` / `entity_scope` / `changed_blocks`
- [ ] T3 端点统一加 `Cache-Control: max-age=<对应 tier 的轮询间隔>`
- [ ] T4 列表端点统一支持 `from/to/page/page_size`（QuantX backlog P0.1 已识别）

### 11.2 前端（QuantX）

- [ ] 工作台展开/隐藏时切换轮询频率（前台/后台/不订阅）
- [ ] SSE 监听 `tier` 字段，T0 立即应用，T2 可批处理
- [ ] `changed_blocks` 驱动局部重拉（不要全量刷新）
- [ ] T0 / T1 数据 stale 时禁用交易按钮
- [ ] SSE 断线指数退避 + workbench 应急轮询切换
- [ ] BFF 跨账户 fanout：按 alias 调 N 次 workbench，合并 exposure contributors

### 11.3 联合验证

- [ ] 模拟 quote stale → 验证 T0 verdict 切换 → SSE 推送 → 前端按钮禁用 < 2s
- [ ] 操作员 halt → 验证 SSE 推送延迟 < 500ms（ModeController 触发到前端按钮变色）
- [ ] 平仓操作 → 验证 SSE 推送 `changed_blocks=["positions", "exposure"]` → 前端只重拉这两块
- [ ] SSE 断开 60s → 验证前端切到 2s 轮询 → 重连成功后回 5s

---

## 12. 不在本契约范围内

- 多租户 / RBAC / SSO（现阶段单内网操作者）
- WebSocket 双工通道（前端发命令仍走 HTTP POST）
- Tick 级行情订阅（项目本来就是 OHLC bar 驱动，无 tick 流）
- 跨账户聚合的后端实现（BFF 职责）
- T0 数据从 1s 快照 diff 升级到事件总线 push（除非实测延迟超 500ms 持续触发，否则不动 ADR-001 同步 dispatch 原则）
