# Telegram 通知模块设计

> 实现状态：**Phase 1 已完成**（核心骨架 + 单向推送 + DI 接入 + 可观测性）
> 范围：`src/notifications/`、`config/notifications.ini`、`config/notifications/templates/`
> 测试：`tests/notifications/`（206 条）

---

## 目标

为 MT5Services 运行时事件提供**插拔式、模板化、分级过滤**的 Telegram 推送，同时预留双向命令（查询 + 低风险控制）能力。关键事件主动到手，低价值事件不打扰。

**设计约束（由用户决策锁定）**：
1. Long polling 接收入站（非 webhook）
2. `requests` + 后台线程（不引入 httpx / aiogram）
3. 仅开放查询 + 低风险控制（**不含** /close-all、/risk-off、/close-position）
4. 单 chat + tag 前缀分类（`[CRITICAL][WARN][INFO]`）

---

## 架构

```
┌─────────────────────── 事件源 ──────────────────────┐
│ PipelineEventBus   HealthMonitor   (后续)TradingStateAlerts
│       │                  │                │
│       ▼                  ▼                ▼
│   PipelineEventClassifier   make_health_alert_listener
└───────────────────────────┬────────────────────────┘
                            │
           ┌────────────────▼──────────────────┐
           │ NotificationDispatcher            │
           │ - Deduper (LRU + TTL)             │
           │ - RateLimiter (global + per-chat) │
           │ - TemplateRegistry.render_event   │
           │ - OutboxStore.enqueue             │
           └────────────────┬──────────────────┘
                            │
           ┌────────────────▼──────────────────┐
           │ Worker 线程 (ADR-005 合规)        │
           │ fetch_due → TelegramTransport.send
           │ 成功 mark_sent / 可重试退避       │
           │ 终态 (4xx 非 429) / 超限 → DLQ    │
           └────────────────┬──────────────────┘
                            ▼
                  Telegram Bot API / sendMessage
```

**零侵入原则**：
- PipelineEventBus 已有 `add_listener()`，直接订阅，不改业务代码
- HealthMonitor 新增 `set_alert_listener(callback)`（~15 行，异常隔离）
- TradingStateAlerts 留到 Phase 2（通过定时 `summary()` 轮询适配，零侵入）

---

## 事件分级与模板映射

### CRITICAL（必推）

| 事件 | 来源 | Pipeline 事件 | 模板 |
|------|------|---------------|------|
| `execution_failed` | Executor 下单失败 | `PIPELINE_EXECUTION_FAILED` | `critical_execution_failed.md` |
| `circuit_open` | 熔断器打开 | `PIPELINE_RISK_STATE_CHANGED` (state=circuit_open) | `critical_circuit_open.md` |
| `queue_overflow` | 队列溢出 | HealthMonitor `execution_queue_overflows` | `critical_queue_overflow.md` |
| `pending_missing` | 挂单丢失 | TradingStateAlerts（Phase 2） | `critical_pending_missing.md` |
| `unmanaged_position` | 未纳管持仓 | `PIPELINE_UNMANAGED_POSITION_DETECTED` | `critical_unmanaged_position.md` |

### WARNING（默认推，可关）

| 事件 | 来源 | 模板 |
|------|------|------|
| `risk_rejection` | `PIPELINE_ADMISSION_REPORT_APPENDED` verdict=reject | `warn_risk_rejection.md` |
| `health_degraded` | HealthMonitor warning/critical 级告警 | `warn_health_alert.md` |
| `eod_block` | `PIPELINE_EXECUTION_BLOCKED` reason 含 eod | `warn_eod_block.md` |

### INFO（白名单推）

| 事件 | 来源 | 模板 |
|------|------|------|
| `execution_submitted` | `PIPELINE_EXECUTION_SUBMITTED` | `info_execution_submitted.md` |
| `position_closed` | PositionManager close hook（Phase 2） | `info_position_closed.md`（待建） |
| `daily_report` | 调度器（Phase 2） | `info_daily_report.md`（待建） |
| `mode_changed` | RuntimeModeController（Phase 2） | `info_mode_changed.md`（待建） |
| `startup_ready` | AppRuntime.start 完成（Phase 2） | `info_startup_ready.md`（待建） |

---

## 6 层开关控制

| 层 | 位置 | 粒度 | Phase 1 落地 |
|---|---|---|---|
| 物理 | `bot_token` / `default_chat_id` 未设 → 工厂返回 None | 模块不构建 | ✓ |
| 全局 | `[runtime] enabled = false` | 模块不启动（可 toggle 激活） | ✓ |
| 运行时 | `POST /v1/admin/notifications/toggle {enabled:false}` | 停 worker + 解 listener，不改 ini | ✓ |
| 事件级 | `[events] <name> = off` | 单事件过滤 | ✓ |
| 实例级 | `[event_filters] suppress_info_on_instances = demo-main` | demo 实例抑制 INFO | ✓ |
| 最终防刷 | Deduper TTL + RateLimiter token bucket | 压制告警风暴 | ✓ |

---

## 持久化与恢复（L2 语义）

**outbox**：`data/runtime/<instance>/notifications_outbox.db`（WAL SQLite）

- 每条 dispatcher.submit 先入 outbox（`status=pending`）
- Worker 拉 due 行 → transport.send → mark_sent / mark_failed / mark_dlq
- 进程重启后 pending 自动接续投递
- DLQ 保留 7 天（可配，默认策略未实现）

**退避策略**：
- 默认 `retry_backoff_seconds = [5, 15, 45]`，三次后进 DLQ
- Telegram 429 返回 `parameters.retry_after` 覆盖本地退避
- 4xx（非 429）直接 DLQ，不浪费重试

**DLQ 查询**：`OutboxStore.dlq_entries()`（未来通过 API 暴露）

---

## DI 接入路径

遵循 CLAUDE.md "新增服务组件" 5 步：

1. **容器字段** — [`src/app_runtime/container.py`](../../src/app_runtime/container.py) 的 `notification_module`
2. **工厂** — [`src/app_runtime/factories/notifications.py`](../../src/app_runtime/factories/notifications.py) 的 `create_notification_module`
3. **构建阶段** — [`src/app_runtime/builder_phases/notifications.py`](../../src/app_runtime/builder_phases/notifications.py) 的 `build_notifications_layer`
4. **builder** — [`src/app_runtime/builder.py`](../../src/app_runtime/builder.py) Phase 5.5（monitoring + read_models 之后）
5. **runtime** — [`src/app_runtime/runtime.py`](../../src/app_runtime/runtime.py) `_start_notifications()` + `close()` 先行卸载
6. **API getter** — [`src/api/deps.py`](../../src/api/deps.py) `get_notification_module()` 返回 `Optional`

### 关闭顺序

AppRuntime.stop 中 notifications **先于** pipeline_bus / health_monitor 拆除，确保 listener 解绑后再销毁上游源，避免僵尸回调。

### ADR-005 合规

Dispatcher 的 worker 线程、Module 的 stop：
- `stop()` 中 `thread.join(timeout)` 之后检查 `is_alive()`
- 仍存活 → 保留引用不清空 → 下次 start 直接 refuse 启动第二条线程

---

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/admin/notifications/status` | 运行状态、outbox 计数、指标快照、dedup 抑制数、rate limit 余量 |
| POST | `/v1/admin/notifications/toggle` | body `{enabled: bool}` — 运行时开关，不改 ini |

**不在 Phase 1 范围**：DLQ 查询端点、模板热重载、手动触发测试事件。

---

## 配置参考

### `config/notifications.ini`（公开基线）

默认 `enabled = false`，内置 13 个事件 + 合理阈值。详见文件内注释。

### `config/notifications.local.ini`（gitignored）

必填：
```ini
[runtime]
enabled = true

[chats]
default_chat_id = 123456789

bot_token = 1234567890:AAE_xxxxx
```

可选：
```ini
[runtime]
http_proxy_url = http://127.0.0.1:7890   # 国内机器需代理翻墙
inbound_enabled = true                    # 启用命令接收（Phase 3）

[inbound]
allowed_chat_ids = 123456789              # 查询命令白名单
admin_chat_ids = 123456789                # 控制命令白名单（allowed 子集）
```

---

## 模板编写规范

文件：`config/notifications/templates/<key>.md`

结构：
```markdown
<!--
severity: critical | warning | info
tag: CRITICAL | WARN | INFO
required_vars: var1, var2, var3
-->
正文 Markdown

- 字段 {{ var1 }}
- 字段 {{ var2 }}

{% if var3 %}可选段 {{ var3 }}{% endif %}
```

启动期严格校验：每个非 `off` 的事件必须有对应 `.md`，`required_vars` 必须与正文 `{{ var }}` 引用一致。缺失 → fail-fast。

**Linter 测试**：`tests/notifications/test_template_registry.py::TestBuiltInTemplates` 自动扫描所有内置模板。

---

## 关键实现决策

### 1. CRITICAL 限流时仍入 outbox（INFO/WARN 丢弃）

**Why**：限流本身是临时现象（Telegram 30/sec API 限），CRITICAL 丢失 = 静默失明。INFO/WARN 丢失 = 降噪。outbox + 退避保证最终送达。

**Where**：[`src/notifications/dispatcher.py`](../../src/notifications/dispatcher.py) `submit()` 第 ~100 行

### 2. `stop()` 不关 outbox，分离 `close()`

**Why**：运行时 toggle 可能反复切换，outbox 跨 toggle 保持状态（pending 行不丢失）。AppRuntime 终态才 `close()` 释放 SQLite 句柄。

**Where**：[`src/notifications/module.py`](../../src/notifications/module.py) `stop()` / `close()`

### 3. Classifier 异常隔离

**Why**：ADR-001 说 PipelineEventBus 同步 dispatch，listener 异常 try/except 保护。但通知 mapper 代码 bug 若 raise 到 listener 层，会污染日志栈且可能触发 bus 自身退化。在 mapper 函数体内捕获所有异常并返回 None。

**Where**：[`src/notifications/classifier.py`](../../src/notifications/classifier.py) `PipelineEventClassifier.classify()`

### 4. 模板 `required_vars` 与正文 `{{ }}` 一致性校验

**Why**：发出"缺字段"的半渲染消息比不发更糟糕。开发期就要捕获模板/classifier 之间的漂移。

**Where**：[`src/notifications/templates/loader.py`](../../src/notifications/templates/loader.py) `_validate_metadata()`

### 5. 令牌桶初始 `_last_refill = None`

**Why**：`monotonic()` 在 bucket 构造时返回的时间与 test 传入的 `now=0.0` 不在同一时间线，会导致首次 refill 计算错误。延迟到第一次 acquire 时锚定时钟起点。

**Where**：[`src/notifications/rate_limit.py`](../../src/notifications/rate_limit.py) `_TokenBucket.__init__`

---

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| Telegram API 国内不可达 | `[runtime] http_proxy_url` 支持；DLQ 兜底 |
| 告警刷屏 | Deduper + RateLimiter + severity 白名单 |
| bot_token 泄漏 | 强制 `.local.ini`（gitignored），日志不打印 token |
| worker 线程挂死 | ADR-005 保留引用 + `status()` 暴露 `worker_running` |
| 配置错误导致模块不构建 | 启动期日志明确提示"no bot_token configured" |
| outbox.db 膨胀 | `purge_sent_older_than()` 已实现，Phase 2 接入定时器 |
| 模板字段漂移 | 启动期校验失败即 crash 启动（避免运行时才发现） |

---

## 后续 Phase 规划

### Phase 2（进行中）

- **定时日报调度器**：每天 UTC HH:MM 触发 `/v1/trade/daily_summary` → `info_daily_report` 推送
- **TradingStateAlerts 适配器**：轮询 `summary()`，把 `pending_missing` / `pending_orphan` / `unmanaged_live_positions` 映射成 NotificationEvent（零侵入）
- **outbox 清理定时器**：`purge_sent_older_than(7 days)` 每 6h 跑一次
- **INFO 模板补齐**：`info_daily_report`、`info_mode_changed`、`info_startup_ready`、`info_position_closed`

### Phase 3（入站查询）

- TelegramPoller（long polling getUpdates）
- CommandRouter + chat_id allowlist
- 查询 handlers：/health /positions /signals /executor /pending /mode /trace

### Phase 4（入站控制）

- 低风险：/halt /resume /reload-config（通过 `OperatorCommandService.enqueue` 走审计链）

### Phase 5（可选）

- `/trace <signal_id>` 深度链路（多段分块）
- DLQ 查询 API
- 模板热重载 API

---

## 参考

- 实现清单：src/notifications/、src/config/models/notifications.py
- 配置：config/notifications.ini、config/notifications.local.ini.example
- 模板：config/notifications/templates/
- 测试：tests/notifications/（8 个文件，206 条测试）
- DI 触点：src/app_runtime/{container,runtime,builder}.py、factories/、builder_phases/
- API：src/api/admin_routes/notifications.py、src/api/deps.py（`get_notification_module`）
- ADR：ADR-001（PipelineEventBus 同步 dispatch）、ADR-005（线程引用保留）、ADR-006（不读私有属性，使用 setter/setter 接入）
