# Next Plan — 下一阶段开发规划

> 首版日期：2026-04-05
> 系统现状与已完成项追踪见 `TODO.md`。本文仅包含待实施方案的技术细节。
> 当前架构/策略/代码/性能风险台账见 `docs/codebase-review.md`；P0/P1 整改顺序以该审查文档为准。
> 文档类型：规划文档，不代表当前已实现状态；当前运行时事实源以 `docs/architecture.md` 与 `docs/design/full-runtime-dataflow.md` 为准。

---

## Phase 0：策略验证闭环（最高优先）

### 0.1 各 TF 基线回测

**执行方式**：`src.ops.cli.backtest_runner` 本地执行，不走 API

```bash
# 各 TF 分别跑，近 3 个月数据
python -m src.ops.cli.backtest_runner --timeframe M5 --days 90
python -m src.ops.cli.backtest_runner --timeframe M15 --days 90
python -m src.ops.cli.backtest_runner --timeframe M30 --days 90
python -m src.ops.cli.backtest_runner --timeframe H1 --days 90
```

**产出要求**：每个 TF 记录 PnL / WR / Sharpe / Sortino / MaxDD / 总交易数。汇总为对比表判断哪些 TF 可行。

### 0.2 策略相关性裁剪

**执行方式**：回测时加 `--monte-carlo` 获取蒙特卡洛 p-value，用 `correlation.py` 生成相关性矩阵

**裁剪规则**：
- 同投票组内相关性 >0.7 → 保留 Sharpe 最高的，其余 regime_affinity 全设 0.0
- 蒙特卡洛 p-value >0.1 → 策略收益不显著优于随机，考虑禁用
- 目标：在当前 8 个结构化策略实例内识别需保留、降权、冻结或拆分验证的策略/TF 组合

**涉及文件**：`signal.local.ini`（affinity 覆盖）

### 0.3 Paper Trading 验证

**配置**：`config/paper_trading.ini` 设 `enabled = true`，`app.ini [trading_ops] runtime_mode = observe`

**验证清单**：
1. 信号是否正常接收（`/v1/paper-trading/trades`）
2. 模拟持仓是否正确管理 SL/TP（`/v1/paper-trading/positions`）
3. 日终是否自动平仓
4. 持久化是否完整（`paper_trading_sessions` / `paper_trade_outcomes` 表）
5. 绩效 vs 回测对比（差距 >30% 需排查）

### 0.4 实验链路闭环（2026-04-06 已实现）

**已完成**：Research → Backtest → Paper Trading → Live 的数据契约和链路打通。

- **experiment_id 穿透**：`MiningResult`、`BacktestResult`、`Recommendation`、`PaperSession` 均支持 `experiment_id` 可选字段
- **start-from-recommendation**：`POST /v1/paper-trading/start-from-recommendation` 从已 apply 的推荐关联启动 Paper Trading
- **验证对比纯函数**：`compare_paper_vs_backtest()` 对比胜率/Sharpe/回撤，`GET /v1/paper-trading/validate` 端点
- **Experiment Registry**：`experiments` 表 + `/v1/experiments` API，被动追踪实验全生命周期
- **Research 持久化 + API**：`research_mining_runs` 表 + `/v1/research/mining` API

**链路工作流**：
```
POST /v1/experiments                        → 创建实验
POST /v1/research/mining/run                → 挖掘（带 experiment_id）
  ↓ 人工编码策略
POST /v1/backtest/run                       → 回测（带 experiment_id）
POST /v1/backtest/walk-forward              → WF 验证
POST /v1/backtest/recommendations/generate  → 生成推荐
POST /v1/backtest/recommendations/{id}/approve + apply
POST /v1/paper-trading/start-from-recommendation → 关联启动 Paper
GET  /v1/paper-trading/validate             → 自动对比
GET  /v1/experiments/{id}/timeline          → 查看全链路
  ↓ 人工确认
POST /v1/runtime/mode (→ full)              → 上线
```

---

## Phase 2：中等复杂度（参数优化）

### 2A. Regime-Aware 动态 TP/SL 倍数

**问题**：`sizing.py` 中 SL/TP ATR 倍数按 TF 固定，不随市场 Regime 调整。RANGING 市场 TP 过大会被反转打止损；TRENDING 市场 TP 过小提前止盈浪费空间。

**方案**：

```python
# sizing.py compute_trade_params() 接收 regime 参数
REGIME_TP_MULTIPLIER = {
    RegimeType.TRENDING:  1.20,   # TP 扩大 20%，让趋势充分运行
    RegimeType.RANGING:   0.80,   # TP 收紧 20%，避免被均值回归打回
    RegimeType.BREAKOUT:  1.10,
    RegimeType.UNCERTAIN: 1.00,
}
REGIME_SL_MULTIPLIER = {
    RegimeType.TRENDING:  1.00,
    RegimeType.RANGING:   0.90,   # SL 收紧，震荡市止损不宜过宽
    RegimeType.BREAKOUT:  1.10,   # 突破需要更大止损空间
    RegimeType.UNCERTAIN: 1.00,
}
```

**涉及文件**：`src/trading/sizing.py`、`src/backtesting/portfolio.py`（同步更新）
**配置**：`signal.ini [regime_sizing]` 可覆盖倍数
**复杂度**：低（sizing 是纯函数，加参数即可）

---

### 2B. 跨 TF 净敞口控制

**问题**：单品种最多 3 仓，但可能同时在 M5/H1/H4 各开 3 仓同向，实际净敞口远超预期。`AccountSnapshotRule` 只检查总仓数，不计净手数。

**方案**：在 `AccountSnapshotRule`（`src/risk/rules.py`）新增净手数检查：

```python
# 同一品种同方向总手数 ≤ max_net_lots_per_symbol (默认 0.3 手)
net_lots = sum(pos.volume for pos in same_symbol_same_dir_positions)
if net_lots + new_volume > config.max_net_lots_per_symbol:
    return RiskCheckResult.reject("NET_LOTS_LIMIT", ...)
```

**涉及文件**：`src/risk/rules.py`、`config/risk.ini`（新增 `max_net_lots_per_symbol`）
**复杂度**：低（在现有规则框架内扩展）

---

### 2C. Equity Curve Filter（权益曲线过滤）

**问题**：当账户权益持续下行时，系统仍持续开新仓。缺乏基于资金曲线走势的动态开关。

**方案**：在 `SignalFilterChain` 新增 `EquityCurveFilter`：

```
原理: 计算近 N 笔交易（或近 N 天）权益移动均线
      当前权益 < MA × (1 - drawdown_threshold) → 触发过滤
      权益恢复到 MA 以上 → 自动解除
```

**数据来源**：`TradingModule.get_account_info()` 实时查询（每次过滤前调用，或后台线程定时更新缓存）

**涉及文件**：
- `src/signals/execution/filters.py` — 新增 `EquityCurveFilter`
- `config/signal.ini` — 新增 `[equity_curve_filter]` section
- `src/trading/service.py` — 暴露权益历史查询接口

**复杂度**：中（需要后台线程维护权益 MA，或每次过滤时实时查询）

---

### 2D. 回测结果数据库持久化增强

**问题**：Walk-Forward 结果存内存缓存，API 重启后丢失；无法跨 session 对比历史回测。

**当前状态**：⚠️ 部分已实现 — `backtest_repo.py`（459 行）已有 `save_result()` / `save_recommendation()` 等方法，回测结果和推荐记录可持久化到 DB。Walk-Forward splits 的 DB 持久化和重启恢复仍为待做。

**剩余工作**：

```
Walk-Forward 结果 → 写入 backtest_wf_splits 表（待做）
API 重启后 → 从 DB 查询历史 WF 结果 list（待做）
```

**涉及文件**：`src/persistence/repositories/backtest_repo.py`、`src/backtesting/api.py`

**复杂度**：低（repo 框架已就绪，补全 WF 相关写入/查询）

---

## Phase 3：高复杂度（后续规划）

### 3A. 多品种支持扩展

**现状**：系统以 XAUUSD 为主，合约规格、点值、点差范围硬编码在多处。

**方案**：`contract_size_map`（已有）扩展为完整的品种规格配置，含点值、最小手数、保证金模式。

---

### 3B. 实盘参数热重载

**现状**：`[regime_detector]` / `[strategy_params]` 修改后需重启服务。

**方案**：API 端点 `POST /v1/config/reload`，触发 `get_signal_config.cache_clear()` + 各组件重新读取配置，无需重启。

---

### 3C. 策略级仓位追踪与绩效归因

**现状**：`trade_outcomes` 表有 `strategy` 字段，但缺少按策略维度的实时仓位收益追踪。

**方案**：`PositionManager` 在 `track_position()` 时记录策略来源，`TradeOutcomeTracker` 按 strategy/regime/tf 维度汇总月度盈亏归因报告。

---

### 3D. 多账户并行运营

**现状**：`TradingAccountRegistry` 支持注册多账户，但信号分发和风控聚合仅针对单账户。

---

## 验收标准参考

| 功能 | 验收条件 |
|------|---------|
| 2A Regime-Aware TP/SL | 回测显示 RANGING 环境盈亏比提升；Sharpe 无显著下降 |
| 2B 净敞口控制 | 单品种同方向手数上限在 precheck 中被拦截，日志可见 |
| 2C 权益曲线过滤 | 模拟连续亏损 3 笔后信号被过滤；权益恢复后自动解除 |
| 2D WF 结果持久化 | API 重启后 `/v1/backtest/results` 仍可查询历史 WF 结果 |

---

## 技术债清单

| 项目 | 位置 | 优先级 |
|------|------|--------|
| WF 结果内存缓存 | `src/backtesting/api.py` | 中（见 2D）|

## 60/20/20 模块化执行（进行中）

### P6: SignalRuntime 职责收敛（2026-04-10）

已完成：

- 在 `src/signals/orchestration/runtime.py` 引入 `QueueRunner / RuntimeStatusBuilder / SignalLifecyclePolicy`，职责切到 `runtime_components.py`。
- 清理 `SignalRuntime.start()/stop()` 队列生命周期，`stop()` 对 WAL 队列保留重放语义，不做 in-memory 清理。
- `runtime.py` 中的队列入队、出队、状态快照、状态机更新入口已走组件化端口。

验收前置（本里程碑）：

- 私有属性越界访问：`SignalRuntime._on_snapshot`、`_enqueue` 等不再直接承担队列与状态分拆逻辑，改为组件调用。
- 已消除已知 `SignalRuntime` 内部收口残留（`@staticmethod` 引用 `self`、`else` 缩进/清队列语义错误）。

下一步（本阶段收尾）：

- 对 `TradeExecutor` 做同级别拆分（`PreTradePipeline / ExecutionDecisionEngine / ExecutionResultRecorder`）
- 与 `backtesting` 对齐 `capability` 语义前，补齐 `SignalModule` 能力索引只读视图，进入 60/20/20 阶段 B。

### P6A: TradeExecutor 预拆分（2026-04-10）

已落地：

- 新增 `src/trading/execution/pre_trade_pipeline.py`
- 新增 `src/trading/execution/decision_engine.py`
- 新增 `src/trading/execution/result_recorder.py`
- `executor.py` 已继续收口：
  - `confirmed` 过滤改走 `PreTradePipeline.run_confirmed`
  - `intrabar` 前置门禁改走 `PreTradePipeline.run_intrabar`
  - 执行动作（市价/挂单）改走 `ExecutionDecisionEngine.execute`
- confirmed 溢出事件由 `ExecutionResultRecorder.record_overflow` 统一记录，状态统计与持久化失败策略集中管理。

收尾目标（本阶段）：

- 将 `_handle_confirmed` 与 `_handle_intrabar_entry` 中剩余“参数构建后状态分发”职责再下沉，减少 `executor.py` 逻辑体量。

### P6A 收尾（2026-04-10）

- 已补齐 `SignalModule` 策略能力索引（`strategy_capability_catalog` / `strategy_capability_contract`），形成只读能力快照：`needs_intrabar`、`valid_scopes`、`needed_indicators`、`needs_htf`、`regime_affinity`、`htf_requirements`。  
- `SignalRuntime` 已接入 `SignalPolicy.set_strategy_capability_contract()`，运行时从 `SignalPolicy` 查询能力快照进行 scope/指标门控。
- 回测层已改为消费能力快照：
  - `src/backtesting/engine/runner.py`：基于能力索引构建 `confirmed` 目标策略、指标需求集合；
  - `src/backtesting/engine/signals.py`：基于能力索引补齐 scope/needed-indicators 门控。
- 继续收口：`SignalRuntime` 已将能力快照绑定到 `SignalPolicy`，下一步补齐 status/策略治理口径对齐和回归验证。

### P6B: 可扩展声明化路径（进行中）

- 先决：能力快照已落地，回测与策略管理共享同一能力语义。
- 已完成（2026-04-10）：
  - `SignalRuntime` 已接入 `SignalModule.strategy_capability_catalog()`：启动时构建策略能力索引；
  - `evaluate_strategies` 运行时门控改用能力快照（`valid_scopes`、`needed_indicators`），与回测确认路径语义对齐。
- 新增 `SignalModule.strategy_capability_matrix()`：提供只读能力清单视图（`valid_scopes/needed_indicators/needs_*`）；
  `SignalRuntime` status 与回测评估均使用该能力视图，不再走分散的 `required_indicators` 推断路径。
- 当前下一步补齐：
- 已完成（2026-04-10）：能力清单统一口收口，`SignalPolicy` 新增 `strategy_capability_contract()` 与 `get_warmup_required_indicators()`，`runtime_warmup` 与 `runtime_status` 全链路改走能力口；`dispatch_operation` 已补齐能力清单视图。
- `SignalRuntime` 状态快照已包含 `strategy_capability_reconciliation`，形成 `SignalModule` 与 `SignalPolicy` 能力对账入口。
- 这一阶段补齐：`/admin/strategies/capability-contract` 提供 module/runtime 完整能力快照；对账已覆盖 `regime_affinity / htf_requirements`。
- 已完成（2026-04-11）：新增 `strategy_capability_execution_plan` 统一快照（runtime/readmodel），显式输出 `configured/scheduled/filtered` 目标计数、scope 覆盖、`needed_indicators` 汇总与 `strategy_timeframes` 过滤原因。
- 已完成（2026-04-11）：`BacktestEngine` 增加同语义 `strategy_capability_execution_plan()`，并写入 `BacktestResult.strategy_capability_execution_plan`，回测与实盘可直接对齐能力调度计划。
- 已完成（2026-04-11）：新增 `GET /admin/strategies/capability-execution-plan`，提供 `module/runtime/backtest` 三方执行计划快照与差异对账（支持可选 `run_id`）。
- 下一步：把能力快照治理点做成 12 点可观测项（`intrabar`、`scope`、`丢弃率`、`回放一致性`）并形成统一回归检查用例，继续推进“新增扩展路径 = 改声明 + 改配置”。

### P6B.1: 能力清单标准化（完成）

- 新增 `SignalModule.strategy_capability_catalog()`，作为标准输入契约，替代零散的能力构造路径。
- `SignalRuntime` 与 `BacktestEngine` 初始化均改为只读该口：
  - `src/signals/orchestration/runtime.py`：`_refresh_strategy_capabilities` 改走 `strategy_capability_catalog`。
  - `src/backtesting/engine/runner.py`：能力索引改走 `strategy_capability_catalog`。
- 产物约束：新增策略改 `SignalStrategy` 声明（`preferred_scopes`、`required_indicators`、`htf_required_indicators`、`regime_affinity`）即可，无需额外 runtime 分支。

#### 下一步建议

- 补一条“能力快照对账”检查点（`SignalModule.strategy_capability_contract()` ↔ `SignalPolicy.strategy_capability_contract()`）已放入 `service_diagnostics` 与 admin 诊断视图（`/admin/strategies/capability-reconciliation`、`/admin/strategies/capability-contract`），作为回放一致性的最小回归。

### P5: PendingEntry 职责收敛

已完成（进行中）：把 `src/trading/pending/manager.py` 的大块私有逻辑切到子模块，目标是让 `PendingEntryManager` 保持门面职责：  

- `src/trading/pending/lifecycle.py`：MT5 挂单生命周期（track/check state/check expiry/cancel）  
- `src/trading/pending/monitoring.py`：监控循环、价格检查、入场填单、超时降级、队列消费  

当前状态图（关键节点）：

```text
TradeExecutor.submit_pending_entry
  → PendingEntryManager.submit
  → [monitor_thread] PendingEntryMonitoringService.check_all_entries
      ├─ check_price / get_spread_points
      ├─ fill_entry（入队 _FillResult）
      └─ expire_entry（支持 timeout_fallback_atr）
  → [fill_worker] PendingEntryMonitoringService.run_fill_worker
      → execute_fn(TradeExecutor)
  → [lifecycle] PendingOrderLifecycleManager.check_mt5_order_state
  → [lifecycle] PendingOrderLifecycleManager.check_mt5_order_expiry
  → [snapshot] PendingEntrySnapshotService (status / active_execution_contexts)
```

> 采用“状态流可观测”原则：`status()` 与 `active_execution_contexts()` 只读聚合 `_pending` 与 `_mt5_orders`，不再直接承载执行算法。

> 2026-04-10 已继续：将状态快照聚合下沉到 `src/trading/pending/snapshot.py`，`PendingEntryManager` 不再保留 status 投影计算路径。

> 以下已清理（2026-04-07）：旧路由兼容层（已不存在）、`get_ohlc()` 别名（已移除）、manager.py 转发方法（已精简至合理门面）、SignalRuntime 1409→1036 行（warmup/metadata 提取）
