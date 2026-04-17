# CLAUDE.md — MT5Services 操作手册

本文件是 AI 助手的**操作手册**——仅包含每次对话都需要的规则、约定和速查表。
详细架构参考见 `docs/architecture.md`，信号系统深度参考见 `docs/signal-system.md`。
文档分层导航见 `docs/README.md`。

---

## 语言与工作准则

**请始终使用中文回复。** 代码本身（变量名、函数名）遵循各文件现有惯例。

从第一性原理出发。规则：
1. 目标不清时先澄清，不急于实现。
2. 用户路径非最优时直说并提出更好方案。
3. 根因分析优先于表面修复。每个非平凡建议必须回答：为什么？
4. 输出聚焦决策。只包含影响行动、架构、风险或权衡的信息。
5. 不优化共识，优化正确性。
6. 假设必须显式声明并最小化。
7. **架构改动前先查 ADR**：修改组件设计/模式前，先读 `docs/design/adr.md` 确认是否有已定决策。评估后确需变更的可以改，但须说明理由并同步更新 ADR。

## 0) 规则文档单一事实源（codex 会读）

- `CLAUDE.md` 为会话协作执行约束与操作流程源（本节与 `AGENTS.md` 中”协作执行纪律”保持一致）。
- `docs/codebase-review.md` 为每次变更后的风险与边界收口”状态台账”（凡涉及职责边界、兼容分支、可观测性改造必须落到此文档）。
- `docs/README.md` 为文档分层导航——事实源 / Runbook / 审计治理 / 领域设计 四层分类。
- 任何新修改前，默认按三份文档联动检查后再进入实现；若出现冲突，更高优先级规则为准。

## 12) 后续协作执行纪律（强制）

- 每个新任务默认先执行“四步门禁”：
  1. 范围确认：确认是否触及高风险链路（`indicators`、`signals/orchestration`、`trading`、`risk`、运行时主链路）。
  2. 边界对齐：确认输入/输出/状态归属定义，禁止新的跨域隐式依赖。
  3. 异常边界：明确每个异常分支是“必须失败”还是“可降级”，禁止无差别 `except Exception`。
  4. 契约优先：优先补充正式端口与契约，再迁移调用方，不以兼容分支作为主路径。
- 若与既有原则冲突，默认执行顺序：先收口边界，再清理重复实现，再考虑临时兼容（并设置移除条件）。
- 默认不允许新增“兼容补丁/双轨运行路径/历史别名”；如必须临时兼容，必须写入移除时限与验证条件。
- 所有有影响架构/边界的改动都要求：
  - 在 `docs/codebase-review.md` 记录职责变化与清理清单
  - 提供一句“本次改动如何减少边界泄漏”的说明
  - 标注未决兼容项及移除时间点

### 断言核验协议（P0/FATAL 断言的硬门槛）

任何 "X 默认禁用 / 未接入 / 从未被调用 / 是 bug" 的断言，**写下前必须完成 3 步追查**：

1. **追模型默认值**：`grep <field> src/config/models/` — Pydantic/dataclass 默认值是 Source of Truth，**不是 ini 字面值**。ini 里 `key=` 空白常被 `_drop_blank_values()` 剥离，Pydantic 默认生效。
2. **追 loader normalize 逻辑**：`grep "_drop_blank_values\|_normalize" src/config/` — 确认空值/缺失如何被处理。
3. **追全 src 调用点**：`grep -rn <method_name> src/` — 不能只看单个模块的片段，必须确认是否存在跨模块调用链。

**任一步未完成 → 只能写"疑似"，不得标 P0/FATAL**。

子代理（Agent/Explore）返回的 "FATAL/P0" 结论**同样需本主线完成 3 步核验后才能采纳**。历史教训：子代理单次扫描常在文件片段停步，跨模块保护（Pydantic 默认、normalize、setter 注入）看不到，由此产生的误报会误导用户。

任何"反常/存在 bug"的代码片段，先读相邻 ADR（`docs/design/adr.md`）——若该片段对应已定决策（如 ADR-005 超时保留引用），不是 bug 而是契约。

---

## 项目概览

FastAPI 量化交易平台，连接 MetaTrader 5 终端。实时行情 → 技术指标 → 信号生成 → 风控 → 自动交易，TimescaleDB 持久化。

- **Python** 3.9–3.12 | **FastAPI** + uvicorn | **TimescaleDB** | **MT5** (Windows-only)
- **入口**:
  - 单实例：`python -m src.entrypoint.web`（默认）或 `python -m src.entrypoint.instance --instance <name>`
  - 多实例：`python -m src.entrypoint.supervisor --group live`
  - 直接 uvicorn：`uvicorn src.api:app`
- **13 个策略实例**（11 结构化策略 + 2 HTF 变体，其中 2 个冻结）| **16 个 Studio Agent**

---

## 配置系统

### 关键规则

- `config/app.ini` 是品种/时间框架的**唯一来源**，禁止在其他文件中重复定义
- 优先级：`*.local.ini` > `*.ini` > 代码默认值（Pydantic 模型）
- **隐私分层**：凭据（password/api_key）和个人调优参数（策略权重/风险限制）**必须**放 `*.local.ini`（gitignored），提交到 Git 的 `*.ini` 中对应字段必须置空
- `.env` 文件已废弃，所有配置在 `.ini` 中
- 通过 `from src.config import get_*_config` 访问配置，不直接解析 `.ini`

### 主要配置文件

| 文件 | 用途 | 隐私 |
|------|------|------|
| `app.ini` | 品种、时间框架、采集间隔、运行模式、**日志文件配置** | 公开 |
| `topology.ini` | **实例编排 SSOT**：group(live/demo) → main + workers | 公开 |
| `market.ini` | API host/port、认证、CORS | api_key → local |
| `mt5.ini` | MT5 终端连接与账户（共享默认值） | login/password → local |
| `db.ini` | TimescaleDB 连接、**retention policy**、demo/live 分库 | user/password → local |
| `signal.ini` | 信号模块全部可调参数 | 策略调优值 → local |
| `exit.ini` | **持仓出场参数（Chandelier + exit_profile + tf_scale），支持实例级覆盖** | aggression 调优 → local |
| `risk.ini` | 风险限制 | 具体阈值 → local |
| `backtest.ini` | 回测与参数优化 | 公开 |
| `storage.ini` | 8 通道队列持久化 | 公开 |
| `indicators.json` | 指标定义与计算流水线 | 公开 |
| `research.ini` | 信号挖掘参数（BH-FDR/排列检验/Rolling IC/特征工程/Regime 分层） | 公开 |
| `economic.ini` | 经济日历同步参数 | 公开 |
| `notifications.ini` | Telegram 推送开关、事件分级、去重 TTL、限流、模板 | bot_token/chat_id → local |

### 多实例配置（`config/instances/`）

```
config/instances/
  <instance-name>/           # 如 live-main, live-exec-a, demo-main
    market.ini               # 该实例 HTTP host/port
    mt5.ini                  # 该实例 MT5 账户语义
    mt5.local.ini            # login/password/server/path（本机密钥）
    risk.ini                 # 可选：账户级风控覆盖
    risk.local.ini           # 可选：敏感风控覆盖
    exit.ini                 # 可选：账户级 Chandelier/出场参数覆盖
    exit.local.ini           # 可选：本机出场调优覆盖
```

- `topology.ini` 定义 group → main + workers，实例角色由 group 归属自动推导
- 根目录 `*.ini` 是共享基线，实例目录下同名文件为该实例专有配置
- 运行期数据隔离：`data/runtime/<instance>/`、`data/logs/<instance>/`
- 详见 `config/instances/README.md`

---

## 核心概念（每次对话必须理解的语义定义）

### Intrabar 的准确含义

**intrabar ≠ 每一笔 tick**。正确理解是：**当前未收盘 K 线的 OHLC 快照更新**。

唯一触发方式：**子 TF close 驱动合成**（Trigger 模式）。

> 历史说明：早期存在轮询模式（Ingestor 直接向 MT5 轮询未收盘 bar），已移除。
> `_ingest_ohlc()` 中未收盘 bar 直接跳过（`# 未收盘 bar 由 trigger 模式合成处理，此处跳过`）。

| 属性 | 说明 |
|------|------|
| 触发方式 | 子 TF confirmed bar close → `_synthesize_parent_intrabar()` 从内存合成父 TF 当前 bar |
| 频率 | 子 TF bar 周期（如 H1 由 M5 驱动 = 每 5min） |
| 数据可靠性 | **基于 L1 confirmed 事件**（子 TF close 不可丢失） |
| 配置位置 | `signal.ini [intrabar_trading.trigger]` |

**Trigger 配置**（`signal.ini`）：
```ini
[intrabar_trading.trigger]
M5 = M1      # M5 intrabar 由 M1 close 驱动
H1 = M5      # H1 intrabar 由 M5 close 驱动
H4 = M15     # H4 intrabar 由 M15 close 驱动
```

| 属性 | 说明 |
|------|------|
| 数据内容 | N-1 根完整收盘 K 线 + 当前 bar（从子 TF confirmed bars 合成的最新 OHLC 值）|
| 去重 | 指标层：快照与上次完全相同则不发布 |
| 下游管道 | `set_intrabar()` → 指标计算 → 信号评估 |

### Confirmed vs Intrabar 双链路

```
confirmed 链路（L1 不可丢弃）：
  bar 收盘 → set_ohlc_closed() → events.db 持久化 → 全部指标
  → scope="confirmed" → _confirmed_events 队列（WAL 持久化，优先消费）
  → 所有策略评估 → 可触发实际交易

intrabar 链路（L3 best-effort）：
  数据源：子 TF confirmed bar close → _synthesize_parent_intrabar() 合成
  → set_intrabar() → 仅自动推导的指标子集
  → scope="intrabar" → _intrabar_events 队列（8192，confirmed 空时才消费）
  → 仅 preferred_scopes 含 "intrabar" 的策略
  → IntrabarTradeCoordinator bar 计数稳定性 → intrabar_armed 可交易信号（需启用）
```

### Intrabar 交易链路（可选，默认关闭）

子 TF close 驱动的盘中入场链路。`[intrabar_trading] enabled = true` 时生效。

```
子 TF bar close（L1 事件）
  → _synthesize_parent_intrabar() 从内存合成父 TF 当前 bar
  → 现有 intrabar 管道（指标 → 策略评估）
  → IntrabarTradeCoordinator.update()
      连续 N 个子 TF bar 同方向 + confidence ≥ 阈值
      → 发布 intrabar_armed_buy/sell
  → TradeExecutor.on_intrabar_trade_signal()
      → IntrabarTradeGuard 去重（同 bar/策略/方向只允许一次）
      → ExecutionGate.check_intrabar() 策略白名单
      → 完整 pre-trade filter chain → 下单
  → 父 TF bar 最终收盘时 confirmed 协调
      同向 → skip（不重复开仓）；反向 → 正常处理
```

### 事件持久性三级分类

| 等级 | 要求 | 事件类型 | 实现 |
|------|------|---------|------|
| **L1 (Durable)** | 必须持久化，不可丢失 | confirmed bar close、trade signal、order execution、risk block | SQLite events.db + DB 写入 + backpressure 重试 |
| **L2 (Recoverable)** | 允许丢失但可回放恢复 | indicator snapshot、reconcile、OHLC 持久化 | StorageWriter 队列（block 策略）+ 定时 reconcile 兜底 |
| **L3 (Best-effort)** | 最佳努力，丢失可接受 | intrabar preview（观测模式）、调试快照、部分监控指标 | put_nowait 队列满则丢弃 + 丢弃计数器 |

> **注意**：`intrabar_trading.enabled = true` 时，intrabar 链路升级为 **L2 语义**——
> 丢弃不会导致错误交易（fail-safe：coordinator 连续计数断裂 → 不触发），
> 但会导致交易机会静默丢失。丢弃时日志升级为 WARNING，coordinator 状态
> 通过 `/v1/signals/runtime/status` 暴露（连续计数、距 armed 差几根、上次重置原因）。
> 触发源（子 TF confirmed bar close）本身是 L1，进程重启后 coordinator 状态归零，
> 等待 N 根子 TF bar 后自动恢复。

### 置信度管线（修正链 — 顺序关键）

```
=== 结构化策略管线（统一评分框架） ===
raw_confidence = base(0.50)
    + why_score(0~1) × 0.15            (方向确认质量)
    + when_score(0~1) × 0.15           (时机精度)
    + where_score(0~1) × 0.10          (结构位质量)
    + vol_score(0~1) × 0.05            (量能确认)
    → cap 0.90
    × effective_affinity               (Regime 结构过滤，SoftRegime 加权)
    → max(confidence_floor, result)    (底线保护)
    = final_confidence
    注：PerformanceTracker/Calibrator 待回测通过后通过装饰器接入
```

### 11 层风控堆栈（从信号到执行，有序）

1. **SignalFilterChain** — 时段/冷却期/点差/经济事件/波动率异常/趋势衰竭过滤
2. **Regime 门控** — 结构化策略在 _why() 内部自行判断 regime 适应性（ADX）；regime_affinity 仅在置信度管线中作为乘数使用
3. **HTF direction alignment** — 策略级分层：trend_continuation 硬门控（趋势延续需顺势）；breakout_follow/range_reversion 软门控（冲突时阻止）；session_breakout/trendline_touch 软加分（冲突时降分不拒绝）；sweep_reversal/lowbar_entry 不使用
4. **ExecutionGate** — voting group 保护、intrabar 策略白名单
5. **PendingEntry** — 策略输出 entry_spec（market/limit/stop + 入场价），PendingEntryManager 纯执行
6. **Pre-trade risk service** — DailyLossLimit / AccountSnapshot / MarginAvailability / TradeFrequency
7. **Executor safety** — 技术熔断器 + 持仓数量预检 + spread_to_stop_ratio 检查
8. **ExposureCloseout** — 日终自动平仓 + 挂单撤销
9. **PositionManager + Chandelier Exit** — 统一出场规则（`exit_rules.py`）：初始 SL / Chandelier trailing（current_ATR 动态）/ breakeven + 梯度锁利 / 信号反转 N-bar 确认 / 超时 / 日终平仓。Regime-aware：按 (category × regime) 选择出场 profile
10. **IntrabarTradeGuard** — 盘中交易去重（同 bar/策略/方向只允许一次）+ confirmed 协调（已有 intrabar 仓位时同向 skip / 反向放行）
11. **RegimeSizing** — TF 差异化 SL/TP（M5:1.8/2.5 ~ D1:2.5/3.5 ATR）+ TF 差异化风险%（M5:×0.50 ~ D1:×1.50）+ per-TF min_confidence

### Warmup 屏障

- **回补期间**：`BackgroundIngestor.is_backfilling=True` 时，所有 confirmed/intrabar 快照不入队、不评估、不产生信号
- **Staleness 校验**：回补结束后，`bar_time` 距当前时间不超过 `2×tf_seconds + 30s` 才解除屏障
- **指标完整性**：`SignalPolicy.warmup_required_indicators = ("atr14",)`，ATR 尚未计算时仍跳过
- 回补期间指标正常计算（API 可查询），仅信号评估被屏蔽

---

## 核心架构速查

### 数据链路

```
MT5 → BackgroundIngestor → MarketDataService(内存缓存) → StorageWriter(8通道异步) → TimescaleDB
                                    ↓
                        UnifiedIndicatorManager → 指标快照（含 volume 指标）
                                    ↓
                        MarketStructureAnalyzer → 结构上下文（7 种状态）
                                    ↓
                        SignalRuntime(confirmed优先+intrabar)
                          → 结构化策略评估（Why/When/Where 三层 + signal_grade A/B/C）
                                    ↓
                        入场漏斗：
                          SignalFilterChain → ExecutionGate
                          → TradeAdmissionService（门控审核 + trace）
                          → ExecutionIntentPublisher（异步意图发布）
                          → ExecutionIntentConsumer → pre-trade risk checks
                          → TradeExecutor → entry_type=market? 直接下单 : PendingEntryManager 挂单
```

> 完整运行时数据流详见 `docs/design/full-runtime-dataflow.md`

### 应用运行时（src/app_runtime/）

- `container.py` — AppContainer：纯组件持有（flat dataclass）
- `builder.py` — `build_app_container()`：构建所有组件（不启动线程）
- `builder_phases/` — 构建阶段：market / signal / trading / monitoring / runtime_controls / account_runtime / paper_trading / read_models
- `runtime.py` — AppRuntime：start/stop 生命周期管理
- `mode_controller.py` — 4 种运行模式：FULL/OBSERVE/RISK_OFF/INGEST_ONLY
- `factories/` — 各组件工厂函数（signals / trading）
- `src/api/deps.py` 仅作 FastAPI DI 适配层

### 启动入口（src/entrypoint/）

- `web.py` — 默认单实例 uvicorn 启动
- `instance.py` — 按实例名启动（`--instance <name>` 或 `MT5_INSTANCE` 环境变量），加载 topology 分配
- `supervisor.py` — 多实例监督进程（`--group live` 或 `--environment demo`），启动 main → 等待 ready → 启动 workers
- `logging_context.py` — 日志上下文自动注入：`[环境|实例|角色]` 前缀

### 持久化三层

| 层 | 技术 | 用途 |
|---|---|---|
| **SQLite** | events.db (460KB) + signal_queue.db (WAL) | OHLC 事件队列 + confirmed 信号持久化队列 |
| **TimescaleDB** | 26 表, 14 hypertable | 全量业务数据 + 三级 retention policy |
| **内存** | MetricsStore (deque 环形缓冲) | 健康指标（重启后重新采集）|
| **告警** | health_alerts.db (SQLite, 极小) | 告警历史 |
| **日志** | RotatingFileHandler | data/logs/ (100MB×10 + errors.log) |

### 关键模块路径

**运行时核心**

| 组件 | 路径 |
|------|------|
| AppContainer / AppRuntime | `src/app_runtime/container.py` / `runtime.py` |
| 构建阶段 | `src/app_runtime/builder_phases/` (market/signal/trading/monitoring/runtime_controls/account_runtime) |
| 入口：单实例 / 多实例监督 | `src/entrypoint/instance.py` / `supervisor.py` |
| 拓扑配置 / 实例上下文 / 运行时身份 | `src/config/topology.py` / `instance_context.py` / `runtime_identity.py` |
| MarketDataService | `src/market/service.py` |
| UnifiedIndicatorManager | `src/indicators/manager.py` |
| MarketStructureAnalyzer | `src/market_structure/analyzer.py` |

**信号域**

| 组件 | 路径 |
|------|------|
| SignalModule | `src/signals/service.py` |
| SignalRuntime | `src/signals/orchestration/runtime.py` |
| 策略目录 | `src/signals/strategies/catalog.py` |
| 结构化策略基类 | `src/signals/strategies/structured/base.py` |
| 结构化策略（7 策略） | `src/signals/strategies/structured/` |
| 策略部署契约 | `src/signals/contracts/deployment.py` |
| MetadataKey 常量 | `src/signals/metadata_keys.py` |
| IntrabarTradeCoordinator | `src/signals/orchestration/intrabar_trade_coordinator.py` |

**交易域**

| 组件 | 路径 |
|------|------|
| TradingModule | `src/trading/application/module.py` |
| TradingService（高阶命令编排） | `src/trading/application/trading_service.py` |
| TradeAdmissionService（入场审核） | `src/trading/admission/service.py` |
| ExecutionIntentPublisher / Consumer | `src/trading/intents/publisher.py` / `consumer.py` |
| OperatorCommandService / Consumer | `src/trading/commands/service.py` / `consumer.py` |
| TradeExecutor | `src/trading/execution/executor.py` |
| PositionManager | `src/trading/positions/manager.py` |
| 统一出场规则（Chandelier Exit） | `src/trading/positions/exit_rules.py` |
| PendingOrderManager | `src/trading/pending/manager.py` |
| IntrabarTradeGuard | `src/trading/execution/intrabar_guard.py` |
| BrokerCommentCodec（MT5 注释编解码） | `src/trading/broker/comment_codec.py` |
| TradingAccountRegistry | `src/trading/runtime/registry.py` |
| TradingStateStore / Recovery | `src/trading/state/store.py` / `recovery.py` |
| AccountRiskStateProjector | `src/trading/state/risk_projection.py` |

**持久化 & 监控**

| 组件 | 路径 |
|------|------|
| StorageWriter（含 StorageChannel 枚举） | `src/persistence/storage_writer.py` |
| TimescaleWriter | `src/persistence/db.py` |
| 仓储（execution_intent/operator_command/pipeline_trace/signal/trade/trading_state） | `src/persistence/repositories/` |
| HealthMonitor (内存环形缓冲) | `src/monitoring/health/monitor.py` |
| PipelineEventBus | `src/monitoring/pipeline/event_bus.py` |
| SQLite 连接工厂 | `src/utils/sqlite_conn.py` |

**经济日历**

| 组件 | 路径 |
|------|------|
| EconomicCalendarService | `src/calendar/service.py` |
| SignalEconomicPolicy | `src/calendar/policy.py` |
| ReadOnlyEconomicCalendarProvider | `src/calendar/read_only_provider.py` |

**通知（Telegram 推送）**

| 组件 | 路径 |
|------|------|
| NotificationModule（组装根） | `src/notifications/module.py` |
| NotificationDispatcher（去重/限流/持久化） | `src/notifications/dispatcher.py` |
| PipelineEventClassifier（事件分类） | `src/notifications/classifier.py` |
| TelegramTransport（Bot API 发送 + 退避） | `src/notifications/transport/telegram.py` |
| TemplateRegistry（Markdown 模板 + 校验） | `src/notifications/templates/loader.py` |
| OutboxStore（L2 持久化队列 + DLQ） | `src/notifications/persistence/outbox.py` |
| Deduper / RateLimiter / Metrics | `src/notifications/{dedup,rate_limit,metrics}.py` |
| 工厂 + 构建阶段 | `src/app_runtime/factories/notifications.py` / `builder_phases/notifications.py` |
| Admin API（status + toggle） | `src/api/admin_routes/notifications.py` |

**回测 & Paper Trading**

| 组件 | 路径 |
|------|------|
| 回测引擎 | `src/backtesting/engine/runner.py` |
| 回测信号评估 | `src/backtesting/engine/signals.py` |
| 执行语义（research vs execution_feasibility） | `src/backtesting/engine/execution_semantics.py` |
| 验证决策 | `src/backtesting/validation.py` |
| BacktestRuntimeStore | `src/backtesting/data/store.py` |
| Paper Trading | `src/backtesting/paper_trading/bridge.py` |

**Research（信号挖掘）**

| 组件 | 路径 |
|------|------|
| 挖掘 Runner | `src/research/orchestration/runner.py` |
| DataMatrix | `src/research/core/data_matrix.py` |
| 挖掘分析器（预测力/阈值/规则） | `src/research/analyzers/` |
| FeatureHub（特征编排入口） | `src/research/features/hub.py` |
| Feature Providers（6 模块） | `src/research/features/{temporal,microstructure,cross_tf,regime_transition,session_event,intrabar}/` |
| 特征候选 / 特征提升 | `src/research/features/candidates.py` / `promotion.py` |
| 策略候选 | `src/research/strategies/candidates.py` |
| 纯统计原语（ACF/block shuffle/效力分析） | `src/research/core/statistics.py` |
| 过拟合防护（BH-FDR/排列检验） | `src/research/core/overfitting.py` |
| 跨 TF 一致性分析 | `src/research/core/cross_tf.py` |

**API 路由拆分**

| 组件 | 路径 |
|------|------|
| 交易命令路由（直接/操作员/信号） | `src/api/trade_routes/command_routes/` (direct/operator/signal) |
| 交易状态路由（概览/列表/审计/流） | `src/api/trade_routes/state_routes/` (overview/lists/audit/stream) |
| Action 契约（API 公共请求/响应模型） | `src/api/action_contracts.py` |
| ReadModels（运行时/交易追踪/管道审计） | `src/readmodels/runtime.py` / `trade_trace.py` / `pipeline_gate_audit.py` |

---

## 代码规范

### 类型安全
- **mypy strict mode**，所有函数必须有类型注解
- 无充分理由不使用 `Any`
- 数据边界（API 请求/响应、配置）使用 Pydantic v2 模型

### Pydantic v2
- `model_validate()` 而非 `parse_obj()`
- `model_dump()` 而非 `dict()`
- `model_config = ConfigDict(...)` 而非 `class Config:`

### 线程安全
- `MarketDataService` 使用 `RLock`——读写均须持锁
- `StorageWriter` 队列线程安全——只 put 数据
- `SignalRuntime` 使用分片锁防止全局锁争用
- 不得在无锁的情况下跨线程共享可变状态

### 错误处理
- 使用 `AIErrorCode` 枚举（`src/api/error_codes.py`）
- 路由处理器返回 `ApiResponse[T]` 包装器
- 使用 `structlog` 结构化日志（路由层），`logging` 模块（其他）

### DI 流程（新增服务组件）
1. `src/app_runtime/container.py` 的 `AppContainer` 添加字段
2. `src/app_runtime/factories/` 添加工厂函数
3. `src/app_runtime/builder.py` 的 `build_app_container()` 构建
4. `src/app_runtime/runtime.py` 的 `AppRuntime.stop()` 注册关闭
5. `src/api/deps.py` 添加 getter 函数

### 跨模块边界（ADR-006）
- **装配层（builder / factories）和 API 层禁止读写组件的 `_` 前缀私有属性**
- 后置注入依赖 → 组件提供 `set_xxx()` setter 或公开属性（`self.xxx`，无下划线）
- 热重载修改 → 组件提供 `update_xxx()` 方法
- API 诊断读取 → 组件提供 `describe_xxx()` / `status()` / 只读 property
- 同包内部模块可用公开属性交互，但同样禁止 `_` 前缀
- 详见 `docs/design/adr.md` ADR-006

---

## SOP：常见操作

### 新增指标
1. `src/indicators/core/` 实现函数（`func(bars, params) → dict`）
2. `config/indicators.json` 添加条目（`dependencies` 一律 `[]`）
3. `tests/indicators/` 添加测试

### 新增策略（结构化策略）
1. `src/signals/strategies/structured/` 新建文件，继承 `StructuredStrategyBase`
2. 声明 `htf_policy = HtfPolicy.XXX`（HARD_GATE/SOFT_GATE/SOFT_BONUS/NONE，注册时校验）
3. 实现 `_why()` + `_when()`（硬门控），可选 `_where()` + `_volume_bonus()`（软门控）
4. 实现 `_entry_spec()`（入场规格：market/limit/stop + 入场价 + zone_atr）
5. `src/signals/strategies/structured/__init__.py` 导出
6. `src/signals/strategies/catalog.py` 注册
7. `signal.ini` + `signal.local.ini` 的 `[strategy_timeframes]` **必须同时添加**
8. `tests/signals/` 添加测试
9. 横切关注点（performance/calibrator）通过装饰器接入，**不在策略内部重复实现**

### 策略生命周期管理

策略有四种状态：**CANDIDATE → PAPER_ONLY → ACTIVE_GUARDED → ACTIVE**（部署契约 `signals/contracts/deployment.py`）

旧的三态简化模型仍可用于日常操作：
- **冻结（Freeze）**：回测表现不佳 → `signal.local.ini` 中 `[regime_affinity.<name>]` 全设 0.0
- **废弃（Deprecate）**：从 `[strategy_timeframes]` 移除所有 TF 条目 → 不加载

> 2026-04-06: Legacy 策略（35 个）和复合策略（4 个）已全部移除，代码历史保留在 git。
> 当前 13 个注册实例（11 策略 + trend_h4 + trend_h4_momentum 变体），其中 lowbar_entry/session_breakout 冻结。

### 新增 API 路由
1. `src/api/<domain>_routes/` 创建路由文件
2. `src/api/<domain>.py` 组合根注册
3. `src/api/error_codes.py` 添加错误码
4. 业务逻辑下沉到服务层或 readmodels，路由层只做协议适配

### 信号挖掘准则
1. **必须多 TF 挖掘** — 不得在单 TF 上得出结论。最少跑 M15/M30/H1 三个 TF
2. **跨 TF 一致性分类**：
   - **Robust**（2+ TF 方向一致）：可纳入策略开发，推荐 IC 最强的 TF
   - **Divergent**（2+ TF 方向翻转）：各 TF 含义不同，需独立评估
   - **TF-specific**（仅 1 TF）：需谨慎，可能是统计噪音
3. **最佳 TF 推荐公式**：`score = |IC| × (1 + IR_bonus) × perm_bonus × sample_bonus`
4. **CLI 用法**：`python -m src.ops.cli.mining_runner --tf M15,M30,H1 --compare`
   - `--providers temporal,microstructure` 可指定只运行特定 Feature Provider（默认全部启用）
5. **跨 TF 分析器**：`src/research/core/cross_tf.py`

### 回测调参写入
- **只写 `signal.local.ini`**，不修改 `signal.ini`
- `ConfigApplicator.apply()` 自动备份 + 原子写入 + 内存热更新

---

## 文档同步准则

**提交代码时必须同步更新相关文档。** 每次 commit 前检查：

| 变更类型 | 需要更新的文档 |
|---------|-------------|
| 新增/删除模块或文件 | CLAUDE.md（模块路径表）、`docs/architecture.md` |
| 策略/指标数量变化 | CLAUDE.md（项目概览行）、`docs/signal-system.md` |
| 配置文件结构变化 | CLAUDE.md（配置表）、README.md |
| 架构性重构 | `docs/architecture.md`、CLAUDE.md（Known Issues） |
| 完成 TODO 项 | TODO.md（归档） |
| 新增设计方案 | `docs/design/` 新建文件 |
| 变更已有 ADR 涉及的组件 | `docs/design/adr.md`（更新或新增 ADR） |

---

## 测试规范

```
tests/api/              → src/api/
tests/app_runtime/      → src/app_runtime/ (builder/factory/runtime 组合测试)
tests/config/           → src/config/
tests/indicators/       → src/indicators/
tests/signals/          → src/signals/
tests/trading/          → src/trading/
tests/calendar/         → src/calendar/
tests/backtesting/      → src/backtesting/
tests/research/         → src/research/
tests/persistence/      → src/persistence/
tests/readmodels/       → src/readmodels/
tests/monitoring/       → src/monitoring/
tests/entrypoint/       → src/entrypoint/
tests/ops/              → src/ops/
tests/smoke/            → 启动冒烟测试
tests/integration/      → 端到端流程
```

标记：`@pytest.mark.unit` / `@pytest.mark.integration` / `@pytest.mark.slow`

运行：
```bash
pytest                    # 全部
pytest -m unit            # 仅单元测试
pytest -m "not slow"      # 跳过慢速测试
black src/ tests/ && isort src/ tests/ && mypy src/ && flake8 src/ tests/
```

---

## Git 工作流

- 默认分支：`main`
- 禁止提交密钥——凭据在 `*.local.ini`（gitignored）
- 提交前运行 `black`、`isort`、`mypy`、`flake8`
- **遇 bug 即修**：测试中发现任何失败（含已有 bug）必须立即修复

---

## Known Issues

> 仅列出仍在生效的、影响日常开发的问题。已修复的历史问题见 git log。

- **Config snapshot at import time**：`src/api/__init__.py` 在导入时一次性读取 API 配置（`@lru_cache`），修改 `market.ini` 后需调用 `/v1/admin/reload-config` 或重启
- **回测进程内执行**：当前回测任务通过 FastAPI `BackgroundTasks` 在进程内异步执行，API 重启会丢失正在运行的任务
- **WF 结果内存缓存**：`WalkForwardResult` 对象缓存在内存中（上限 50），API 重启后丢失
- **generate_report(hours=24)**：内存环形缓冲 ring_size=2400 仅覆盖 ~3.3h，24h 报告窗口内早期数据为空
- **Flaky 集成测试风险**：`test_signal_trade_chain.py` 中的测试依赖 `deps` 模块全局状态隔离，若新增 API 测试触发 `_ensure_initialized()` 可能污染后续测试

### 已沉淀的设计决策（详见 `docs/design/adr.md`）

以下改进已沉淀为架构决策，**修改相关组件前先读 ADR 文件，评估后可变更但须更新 ADR**：
- ADR-001: PipelineEventBus 同步 dispatch
- ADR-002: SignalRuntime warmup/metadata 纯函数提取
- ADR-003: MetadataKey 常量化
- ADR-004: 组件生命周期安全契约（8 项防护机制不可移除）
- ADR-005: 后台线程 join 超时后必须保留仍存活线程引用（防止双线程消费）
- ADR-006: 跨模块边界禁止读写私有属性（装配/API 层必须通过公开端口）

---

## 深度参考文档（按需读取）

> 文档分层导航见 `docs/README.md`（事实源 / Runbook / 审计治理 / 领域设计 四层）

### 事实源（当前实现的权威快照）

| 文档 | 内容 | 何时读取 |
|------|------|---------|
| `docs/architecture.md` | 完整架构：启动流程、数据流、模块边界、布局规范 | 架构性重构时 |
| `docs/design/full-runtime-dataflow.md` | 完整运行时数据流：启动顺序、日志路径、健康探针、状态所有权矩阵 | 排查运行时问题时 |
| `docs/design/signals-dataflow-overview.md` | 信号域运行时链路：策略评估、队列优先级、异常退化 | 修改信号管线时 |
| `docs/design/intrabar-data-flow.md` | Intrabar 支链：子 TF→父 TF 合成、trigger 配置、coordinator 判定 | 修改 intrabar 链路时 |
| `docs/design/entrypoint-map.md` | 统一启动入口映射：web/instance/supervisor、MT5 session gate | 修改启动流程时 |
| `docs/design/quantx-trade-state-stream.md` | QuantX 前端交易态 SSE 流设计：12 种事件类型、分阶段落地 | 开发前端对接时 |

### 运维 Runbook

| 文档 | 内容 | 何时读取 |
|------|------|---------|
| `docs/runbooks/system-startup-and-live-canary.md` | 启动巡检 + 5 分钟检查 + Live Canary 步骤 + 日志判读 | 启动系统/排障时 |

### 审计与治理

| 文档 | 内容 | 何时读取 |
|------|------|---------|
| `docs/design/adr.md` | 架构决策记录（6 项 ADR），变更前必须评估 | 修改已有 ADR 涉及的组件前 **必读** |
| `docs/codebase-review.md` | 风险与边界收口状态台账 | 每次变更前检查 |

### 领域设计参考

| 文档 | 内容 | 何时读取 |
|------|------|---------|
| `docs/signal-system.md` | 信号系统：策略列表、Intrabar 链路、置信度管线 | 新增/修改策略时 |
| `docs/research-system.md` | 挖掘系统：三大分析器、过拟合防护、多 TF 准则 | 新增/修改挖掘逻辑时 |
| `docs/design/pending-entry.md` | PendingEntry 设计方案（A7 已将入场职责回归策略层） | 修改入场逻辑时 |
| `docs/design/risk-enhancement.md` | PnL 熔断器 + PerformanceTracker 恢复设计 | 修改风控逻辑时 |
| `docs/design/notifications.md` | Telegram 推送模块架构 + 事件分级 + 6 层开关 + DI 接入 | 新增推送事件源/模板/改动分发链路时 |
| `docs/design/next-plan.md` | 下一阶段开发规划 | 规划新功能时 |
| `docs/quantx-backend-backlog.md` | QuantX 后端待办（总览读模型、SSE 流、资金聚合） | 开发 QuantX 对接时 |
