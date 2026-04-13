# QuantX 统一交易态实时流设计草案

> 更新日期：2026-04-12
> 目标：定义 `QuantX` 前端与 `MT5Services` 之间的统一交易态实时流，用于替代执行页、总览页对多组状态接口的高频轮询。
> 适用范围：单实例、单账户、内网操作者场景。

---

## 1. 设计目标

这个实时流要解决的不是“页面更炫”，而是下面 4 个实际问题：

1. 执行页当前需要轮询多个接口，成本高且状态容易不一致。
2. 操作员做完 `closeout / cancel / runtime mode` 后，前端不能立即确认最终状态。
3. 总览页和执行页长期挂屏时，高频轮询会放大后端压力。
4. 审计与控制之间缺少统一的事件序列，排障时会出现“知道结果，不知道过程”。

因此该流的目标是：

- 让前端在一次连接中拿到交易态变化
- 保证关键状态变化有顺序、有时间戳、有可追踪 ID
- 把控制操作和审计回写串进同一条事件链
- 让前端在断线重连后可以恢复到一致状态

---

## 2. 非目标

当前设计不解决以下问题：

- 多租户多账户广播
- 面向公网的权限隔离
- 全量历史审计回放
- 替代所有现有 REST 读接口

REST 读接口仍然保留，实时流只负责“状态变化推送”。

---

## 3. 推荐接口

### 3.1 主接口

- `GET /v1/trade/state/stream`

返回：

- `text/event-stream`

认证：

- 与现有 API 一致，继续使用 `X-API-Key`
- 浏览器不直接连后端，由 `QuantX BFF` 代理

---

### 3.2 查询参数

建议支持：

- `symbol`
- `account`
- `include_snapshot`
- `include_alerts`
- `include_positions`
- `include_orders`
- `include_pending`
- `heartbeat_seconds`

建议默认：

- `include_snapshot=true`
- `include_alerts=true`
- `include_positions=true`
- `include_orders=true`
- `include_pending=true`
- `heartbeat_seconds=15`

说明：

- `include_snapshot=true` 表示建立连接后先推一次当前交易态快照
- 前端如果只是执行页，可以订阅全量
- 如果后续有更细颗粒度页面，可按需裁剪事件

---

## 4. 建议保留的 REST 配套接口

实时流不能完全替代读接口，建议保留：

- `GET /v1/trade/state`
- `GET /v1/trade/state/alerts`
- `GET /v1/trade/command-audits`
- `GET /v1/trade/traces`（待新增）

推荐工作方式：

1. 页面初次加载时读一次 REST 快照
2. 建立 SSE 连接
3. 之后用 SSE 增量更新本地状态
4. 断线重连失败或版本不一致时，回退到 REST 全量刷新

如果后端实现 `include_snapshot=true`，则前端也可以在部分页面跳过首个 REST 请求。

---

## 5. 事件总线模型

统一事件 envelope 建议如下：

```json
{
  "event_id": "evt_20260412_000001",
  "stream": "trade_state",
  "schema_version": "1.0",
  "event_type": "position_changed",
  "recorded_at": "2026-04-12T10:31:25.123+08:00",
  "sequence": 128431,
  "state_version": 912,
  "account": "primary",
  "symbol": "XAUUSD",
  "trace_id": "trace_xau_001",
  "signal_id": "sig_xau_001",
  "action_id": "act_closeout_001",
  "audit_id": "audit_001",
  "payload": {}
}
```

### 5.1 字段约束

- `event_id`
  - 全局唯一
  - 用于前端去重
- `stream`
  - 固定为 `trade_state`
- `schema_version`
  - 便于后续兼容演进
- `event_type`
  - 事件类型枚举
- `recorded_at`
  - 事件落地时间，ISO8601
- `sequence`
  - 单调递增，用于排序与重连校验
- `state_version`
  - 当前交易态版本号，每次状态变化时递增
- `account`
  - 预留多账户兼容，但当前仍按单账户设计
- `symbol`
  - 能定位到相关品种时必须给出
- `trace_id / signal_id`
  - 有链路归属时必须给出
- `action_id`
  - 人工或系统控制动作 ID
- `audit_id`
  - 对应命令审计 ID
- `payload`
  - 事件具体内容

---

## 6. 事件类型设计

### 6.1 state_snapshot

用途：

- 连接建立后推送一次当前全量快照

建议 `payload`：

```json
{
  "runtime_mode": {},
  "trade_control": {},
  "closeout": {},
  "positions": [],
  "orders": [],
  "pending_entries": [],
  "alerts": []
}
```

说明：

- 前端拿到这个事件后即可初始化执行页
- 这个事件不是审计事实，主要用于视图初始化

---

### 6.2 runtime_mode_changed

触发时机：

- `POST /v1/trade/runtime-mode` 生效后

建议 `payload`：

```json
{
  "previous_mode": "full",
  "current_mode": "ingest_only",
  "configured_mode": "ingest_only",
  "reason": "manual_closeout",
  "actor": "operator"
}
```

---

### 6.3 trade_control_changed

触发时机：

- `POST /v1/trade/control` 生效后

建议 `payload`：

```json
{
  "previous": {
    "auto_entry_enabled": true,
    "close_only_mode": false
  },
  "current": {
    "auto_entry_enabled": false,
    "close_only_mode": true
  },
  "actor": "operator",
  "reason": "spread_risk"
}
```

---

### 6.4 position_changed

触发时机：

- 新开仓
- 仓位减仓
- 仓位平掉
- 盈亏、止损止盈、保护状态发生关键变化

建议 `payload`：

```json
{
  "change_type": "opened",
  "ticket": 1408211,
  "position": {
    "ticket": 1408211,
    "symbol": "XAUUSD",
    "direction": "buy",
    "volume": 0.4,
    "open_price": 2331.2,
    "sl": 2327.8,
    "tp": 2338.5,
    "profit": 0.0,
    "status": "open"
  }
}
```

约束：

- `change_type` 建议枚举：`opened / updated / partially_closed / closed`

---

### 6.5 order_changed

触发时机：

- 挂单创建
- 挂单修改
- 挂单触发
- 挂单取消
- 挂单过期

建议 `payload`：

```json
{
  "change_type": "cancelled",
  "ticket": 280119,
  "order": {
    "ticket": 280119,
    "symbol": "XAUUSD",
    "type": "buy_limit",
    "volume": 0.2,
    "price": 2329.6,
    "status": "cancelled"
  }
}
```

---

### 6.6 pending_entry_changed

触发时机：

- pending entry 新增
- 被激活
- 被取消
- 超时失效

建议 `payload`：

```json
{
  "change_type": "cancelled",
  "pending_entry": {
    "signal_id": "sig_xau_003",
    "trace_id": "trace_xau_003",
    "symbol": "XAUUSD",
    "timeframe": "H1",
    "strategy": "structured_session_breakout",
    "status": "cancelled",
    "reason": "manual_cancel"
  }
}
```

---

### 6.7 alert_raised

触发时机：

- 风控告警触发
- 执行器异常
- 队列拥塞
- closeout 触发

建议 `payload`：

```json
{
  "alert_id": "alert_001",
  "severity": "warning",
  "code": "spread_warning",
  "message": "点差超过阈值",
  "component": "trade_executor",
  "status": "active"
}
```

---

### 6.8 alert_resolved

触发时机：

- 告警解除

建议 `payload`：

```json
{
  "alert_id": "alert_001",
  "resolved_by": "system",
  "resolution": "spread_back_to_normal"
}
```

---

### 6.9 closeout_started

触发时机：

- 进入人工或系统 closeout

建议 `payload`：

```json
{
  "mode": "exposure_closeout",
  "reason": "manual_closeout",
  "actor": "operator",
  "position_count": 2,
  "order_count": 1
}
```

---

### 6.10 closeout_finished

触发时机：

- closeout 全部完成或部分完成

建议 `payload`：

```json
{
  "status": "completed",
  "closed_positions": 2,
  "cancelled_orders": 1,
  "failed_actions": 0,
  "runtime_mode_after": "ingest_only"
}
```

---

### 6.11 command_audit_appended

用途：

- 让执行页和审计中心更快看到新命令审计

建议 `payload`：

```json
{
  "audit_id": "audit_001",
  "command_type": "closeout_exposure",
  "status": "success",
  "actor": "operator",
  "message": "人工 closeout 完成"
}
```

说明：

- 这不是必须项，但强烈建议加入
- 否则前端仍需要单独轮询 `command-audits`

---

### 6.12 heartbeat

用途：

- 保活
- 帮助前端判断连接是否 stale

建议 `payload`：

```json
{
  "healthy": true,
  "lag_ms": 42
}
```

---

## 7. SSE 输出格式建议

推荐使用标准 SSE：

```text
id: evt_20260412_000001
event: position_changed
data: {"event_id":"evt_20260412_000001","stream":"trade_state","schema_version":"1.0","event_type":"position_changed","recorded_at":"2026-04-12T10:31:25.123+08:00","sequence":128431,"state_version":912,"account":"primary","symbol":"XAUUSD","trace_id":"trace_xau_001","signal_id":"sig_xau_001","payload":{"change_type":"opened","ticket":1408211,"position":{"ticket":1408211,"symbol":"XAUUSD","direction":"buy","volume":0.4,"status":"open"}}}

```

要求：

- `id` 使用 `event_id`
- `event` 使用 `event_type`
- `data` 为完整 JSON envelope

---

## 8. 顺序、重连与一致性语义

### 8.1 交付语义

建议采用：

- 至少一次投递
- 前端按 `event_id` 去重
- 前端按 `sequence` 排序

不建议承诺：

- 精确一次投递

---

### 8.2 Last-Event-ID

建议支持：

- SSE 标准 `Last-Event-ID`

行为建议：

1. 客户端断线后带 `Last-Event-ID` 重连
2. 服务端尝试从最近事件缓冲中续传
3. 如果缓冲中不存在该 ID：
   - 发送 `state_snapshot`
   - 或显式返回 `resync_required`

建议新增事件类型：

- `resync_required`

建议 `payload`：

```json
{
  "reason": "event_buffer_missed",
  "expected_last_event_id": "evt_20260412_000010"
}
```

---

### 8.3 state_version

`state_version` 用来帮助前端判断自己是不是丢过关键状态更新。

建议规则：

- 任意影响交易态聚合结果的事件都递增 `state_version`
- `heartbeat` 不递增
- 如果前端发现版本跳跃异常，可主动回退到 REST 全量同步

---

## 9. 和控制接口的配合关系

统一交易态实时流的最大价值之一，是把“控制操作”与“状态变化”串起来。

推荐链路如下：

1. 前端发起 `POST /v1/trade/runtime-mode`
2. 后端返回 `action_id` 和 `audit_id`
3. 后端写命令审计
4. SSE 推送：
   - `command_audit_appended`
   - `runtime_mode_changed`
5. 前端根据 `action_id / audit_id` 更新按钮状态和操作回显

同理适用于：

- `closeout_exposure`
- `cancel_orders`
- `close`
- `pending_entry cancel`

---

## 10. 为什么这个优化能减少轮询

如果没有统一流，执行页至少要定时轮询这些接口：

- `/v1/trade/state`
- `/v1/trade/state/alerts`
- `/v1/positions`
- `/v1/orders`
- `/v1/trade/command-audits`
- `/v1/monitoring/pending-entries`

这会带来三个问题：

1. 请求数量多，页面挂久了很浪费
2. 各接口更新时间不一致，前端会看到“半新半旧”的状态
3. 操作后要等下一轮轮询才能看到结果

有了统一流后：

- 初始只需要一次快照
- 后续按事件增量更新
- 只有重连失败或版本失配时才需要全量轮询补偿

这就是“减少执行页轮询”的核心意义。

---

## 11. 分阶段落地建议

### Phase 1

先支持：

- `state_snapshot`
- `runtime_mode_changed`
- `trade_control_changed`
- `position_changed`
- `order_changed`
- `alert_raised`
- `alert_resolved`
- `heartbeat`

这样执行页和总览页就已经能显著减少轮询。

### Phase 2

再补：

- `pending_entry_changed`
- `closeout_started`
- `closeout_finished`
- `command_audit_appended`

这样可以把执行控制和审计闭环打通。

### Phase 3

最后再评估是否需要：

- 更细粒度的风险事件
- 和 signal / trace 事件的跨域统一总线

---

## 12. 对 QuantX 前端的直接收益

落地后，前端可以立即获得这些能力：

1. 执行页从“每 5~15 秒轮询多组接口”改成“一个 SSE + 少量兜底轮询”。
2. 总览页的持仓、挂单、风险提示能接近实时更新。
3. 操作员执行 closeout 或切模式后，能立刻看到回显，不再像黑盒。
4. 审计中心可以把控制动作和交易状态变化串成同一条时间线。
5. 长时间挂屏时，对后端压力更可控。

---

## 13. 相关文档

- `docs/quantx-backend-backlog.md`
- `docs/design/full-runtime-dataflow.md`
- `docs/design/signals-dataflow-overview.md`
