# Next Plan — 下一阶段开发规划

> **📐 文档类型：长期技术方案（Phase 级规划）**
>
> 首版：2026-04-05 · 最近维护：见 git log
>
> **职责分工**（和兄弟文档的边界，严禁重复）：
> - 本文 → **长期技术路线 + 方案细节**（Phase FP / 架构演进 / 跨 sprint 工作）
> - `TODO.md` → **当前 sprint 活跃待办 + 本期系统现状快照**
> - `docs/codebase-review.md` → **已完成项归档 + 风险台账 + 历史整改记录**
> - `docs/architecture.md` / `design/full-runtime-dataflow.md` → **当前运行时事实源**
>
> 涉及"已实现"结论必须给 commit hash + 代码位置；本文的任何内容都不代表当前代码状态——
> 以 `codebase-review.md` 为准。

---

## Phase FP：Feature Providers 后续优化（2026-04-17 新增）

> 上下文：已完成 Research Feature Providers 架构（6 个 Provider，~85 特征），
> 首次挖掘发现 `regime_transition.bars_in_regime` 成为 H1 决策树根节点，
> 基于挖掘结果创建 `structured_regime_exhaustion` 策略（12 个月回测 Sharpe=2.51，PF=5.50）。

### FP.1 — 长窗口挖掘验证 (P0，30min)

**目标**：用 12 个月完整数据重跑挖掘，验证 H1 的 `bars_in_regime` 规则是否跨时间段稳定。

```bash
python -m src.ops.cli.mining_runner \
    --environment live --tf M30,H1,H4 \
    --providers temporal,microstructure,regime_transition \
    --start 2025-04-10 --end 2026-04-15 \
    --compare --emit-candidates
```

**成功标准**：
- `bars_in_regime` 在 12 个月窗口中仍是 H1 决策树关键节点
- 至少在 2 个 TF 上找到 robust 跨 TF 规则
- 生成 2+ 个 StrategyCandidateSpec

**决策点**：若规则不稳定 → 3 个月数据的巧合，重新评估 regime_exhaustion 部署

### FP.2 — CrossTFProvider 数据加载实现 (P1，代码)

**当前状态**：`src/research/orchestration/runner.py:_prepare_extra_data()` 是存根，CrossTF 默认关闭。

**需实现**：
- 在 Runner 中加载父 TF 历史 bars（通过 `components.data_loader`）
- 在父 TF bars 上计算所需指标（通过 `components.indicator_manager`）
- 构造 `extra_data` dict 传给 Hub

**关键文件**：`src/research/orchestration/runner.py` 的 `_prepare_extra_data()` 方法

**验证**：启用 cross_tf 后挖掘，观察 `ctf_parent_trend_dir`、`ctf_tf_trend_align` 是否进入 Top Findings

**预期价值**：跨 TF 共振/冲突是经典交易信号，应有显著预测力

### FP.3 — 诊断新特征未进入决策树 (P2)

**疑问**：首次挖掘中 temporal (34) + microstructure (21) 特征都未进入 Top 规则，需确认是：
- (A) 这些特征的 IC 确实弱
- (B) IC 有价值但被决策树 max_depth=3 限制（base 指标先占分裂位）

**执行**：
```bash
python -m src.ops.cli.mining_runner --environment live --tf H1,M30 \
    --providers temporal,microstructure \
    --analysis predictive_power --template default \
    --json-output data/research/mining_new_features_only.json
```

**决策**：
- 若 (A) → 调整窗口（temporal 用 `20` 代替 `3,5,10`）或增加新派生公式
- 若 (B) → 增加 `max_depth=4`，或实现序列模式分析器（P4）

### FP.4 — Demo Validation 验证 regime_exhaustion (P3，观察) — ADR-010 后

**当前状态**：策略已绑定 `live_exec_a` 和 `demo_main`，deployment=`demo_validation`（ADR-010 后命名，原 `paper_only`）

**观察指标**（demo-main 真实下单运行 1-2 周）：
- 实际触发次数 vs 回测预期（回测 53 笔/年 ≈ 每周 1 笔）
- Demo WR 是否接近 49%
- SL/TP 触发分布（回测：25 TP / 26 SL / 2 timeout）

**决策**：
- `demo_vs_backtest` 对账通过（成交率 > 70% + PF drift < 30% + 信号密度漂移 < 30%）→ 升级 `active_guarded` 加入 live 路径
- 显著偏离 → 排查 slippage / spread / 时序 gap

### FP.5 — 序列模式分析器 (P4，新能力)

**场景**：temporal/microstructure 真正价值可能在**序列模式**（N 根 bar 演化），非单点值。决策树捕捉不到。

**设计**：实现 `SequencePatternAnalyzer` — 查找 "条件 A 在 bar[t-k] 满足 AND 条件 B 在 bar[t] 满足" 这类规则。

**接入点**：已预留 `MiningAnalyzer` 协议（`src/research/orchestration/runner.py`），只需实现协议 + 注册到 `_analyzers` 列表。

**难度**：高（1-2 会话）。

### FP.6 — 推荐执行顺序

```
下一 session:
  ① FP.1 长窗口挖掘验证 (30min)  ← 快速、高价值
  ② 若挖掘发现新的好规则 → 创建第 2 个挖掘驱动策略
     否则 → 做 FP.2 CrossTF 数据加载实现

后续 sessions:
  Demo 观察 1-2 周（FP.4，demo-main 真实下单） → regime_exhaustion 实盘数据
  FP.3 诊断 temporal/micro 特征价值
  视情况实现 FP.5 序列模式分析器
```

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

### 0.3 Demo Validation 验证（ADR-010 后）

**配置**：策略状态改为 `status = demo_validation`（写 `signal.local.ini [strategy_deployment.<name>]`），启动 `demo-main` 实例

```bash
python -m src.entrypoint.instance --instance demo-main
```

装配层（`_filter_strategies_for_environment`）会自动把 demo_validation 策略加入 demo-main 的 SignalRuntime，live-main 拒绝装配。候选策略走 demo broker 真实下单，经过完整 11 层风控。

**验证清单**：
1. 信号是否正常接收（`/v1/signal/recent`，environment=demo）
2. 实际持仓是否正确管理 SL/TP（`/v1/trade/state`，看 demo-main）
3. 日终是否自动平仓（ExposureCloseoutController）
4. 持久化是否完整（`db.demo.signal_events` / `db.demo.trade_outcomes` / `db.demo.pipeline_trace_events`）
5. 跨库对账：`python -m src.ops.cli.demo_vs_backtest --backtest-run-id <X> --demo-window 7d --strategies <name>`
   - 信号密度漂移 < 30%、actionability_rate > 0.5、trades_drift < 30%、pf_drift < 30% 全部通过 → 升级 `active_guarded`

### 0.4 实验链路闭环（ADR-010 后更新）

**已完成**：Research → Backtest → Demo Validation → Live 的数据契约和链路打通。

- **experiment_id 穿透**：`MiningResult`、`BacktestResult`、`Recommendation` 均支持 `experiment_id` 可选字段（`paper_session_id` 字段已 ADR-010 删除）
- **demo_validation 流程**：策略 status=demo_validation 自动被 demo-main 装配，无需独立 sidecar
- **跨库对账 CLI**：`python -m src.ops.cli.demo_vs_backtest`（替代被删除的 `paper_vs_backtest`）
- **Experiment Registry**：`experiments` 表 + `/v1/experiments` API，`status` enum 已迁移 `paper_trading → demo_validation`
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
  ↓ 改 signal.local.ini status = demo_validation
启动 demo-main → 自动装配候选策略 → 真实下单 7+ 天
python -m src.ops.cli.demo_vs_backtest      → 跨库对账
GET  /v1/experiments/{id}/timeline          → 查看全链路
  ↓ 对账通过 → 改 status = active_guarded
启动 live-main → 小手数实盘验证 1-2 周
  ↓ 人工确认
改 status = active                          → 完整手数上线
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


---

## Phase P4：架构演进（从 TODO.md 迁入，2026-04-21）

> 这些任务跨 sprint，属于"实盘稳定后"阶段。从 TODO.md 搬迁到此，统一长期规划视角。
> 完成后按规范搬到 `docs/codebase-review.md §F` 归档。

### P4.1 配置热重载

- [ ] `signal.ini` 交易成本阈值偏紧（base_spread_points=30, max_spread_to_stop_ratio=0.33）启动持续警告
- [ ] `[regime_detector]` / `[strategy_params]` 热重载支持（当前需重启）
- [ ] `market.ini` 应用壳层配置热重载决策

### P4.2 Intrabar 工程化后续（2026-04-09 提出）

> 目标：把 intrabar 从"best-effort 可用"提升到"可量化、可降级、可审计"的交易级链路。

- [ ] **SLO 与告警基线**
  - 定义并落地 3 个核心 SLO：`intrabar_drop_rate_1m`、`intrabar_queue_age_p95`、`intrabar_to_decision_latency_p95`
  - 在 `/signals/monitoring` 或健康端点暴露 intrabar 溢出统计（wait_success / replace_success / overflow_failures）
  - 设定分级告警阈值（INFO/WARN/CRITICAL）并写入运行文档
- [ ] **调度策略从固定配额升级为自适应配额**
  - 将 confirmed:intrabar 固定让路策略改为基于 backlog/queue_age 的动态配额
  - 增加"高拥塞模式"下的自动限流与恢复条件
  - 补充回放测试：低波动 / 高波动 / 极端爆量 3 组场景对比
- [ ] **链路降级矩阵（Degrade Ladder）**
  - 定义 L0-L3 四档降级：全量 intrabar → 白名单策略 → 核心 TF → 仅 confirmed
  - 建立降级触发器（queue_age、drop_rate、CPU 负载）与回升条件
  - 将"随机丢弃"转为"可预期降级"，并在状态接口中可见
- [ ] **启动前置校验与运行态自愈**
  - intrabar_trading=true 时，启动阶段校验 listener/策略白名单/trigger 映射完整性
  - "无 listener 跳过"持续超阈值时触发自愈动作
  - 补充 runbook：排查步骤、指标看板、回滚开关
- [ ] **端到端可审计 Trace**
  - 统一 trace_id 贯通：`intrabar_bar → indicator_snapshot → strategy_decision → vote → gate → order`
  - 在 trade audit 视图增加"本次下单来自哪个 intrabar 快照"的可追踪字段
  - 增加 1 条失败链路示例（被 filter/gate 阻断）用于回归测试与演示

### P4.3 结构化策略架构适配

> 策略变聪明后，管线应变简单。横切关注点（performance/calibrator）通过**装饰器**接入，不在各模块重复。

#### A8: PerformanceTracker 按信号等级追踪（装饰器接入）

- [ ] 回测验证结构化策略有效后，通过装饰器接入 PerformanceTracker
- [ ] PerformanceTracker key 从 `strategy_name` 改为 `(strategy_name, signal_grade)`
- [ ] 乘数按 grade 分别计算：A 级信号可获更高 multiplier，C 级被压制
- [ ] `signal_grade A/B/C` 已在 `StructuredStrategyBase.evaluate()` 中自动计算

#### A11: regime_affinity 乘数审视（待回测数据验证）

> `_why()` 已用 ADX/HTF 做 regime 检查（硬门控），regime_affinity 在 `service.py` 再乘一次（软门控）。
> 两者对同一件事做了两次判断，可能需要移除或简化。

- [ ] 对比 `regime_affinity=1.0`（禁用）vs 当前值的回测差异
- [ ] 如果差异不大，移除 regime_affinity 乘法，管线简化为纯策略评分

### P4.4 架构解耦

- [ ] A5: PendingOrderManager 从 TradeExecutor 提升为独立组件
- [ ] A2: 关键交易操作改为 Event Sourcing

---

## Telegram 通知模块 Phase 2.5+（从 TODO.md 迁入，2026-04-21）

> Phase 1 + Phase 2（scheduler + alerts 适配）已完成，见 `codebase-review.md §F`。

### Phase 2.5：daily_report 内容 + 运维工具

- [ ] **daily_report 内容生成器**：复用 RuntimeReadModel 组装 health/positions/executor 摘要 → `info_daily_report` 模板
- [ ] **INFO 模板补齐**：`info_daily_report`、`info_mode_changed`、`info_startup_ready`、`info_position_closed`
- [ ] **CLI 测试工具**：`python -m src.ops.cli.test_notification --event <type>` 支持本地触发样本事件验证部署

### Phase 3：入站查询命令

- [ ] `TelegramPoller`（long polling getUpdates，独立线程）
- [ ] `CommandRouter` + chat_id allowlist + 命令白名单
- [ ] 查询 handlers（调用 `RuntimeReadModel`，只读）：
  - `/health` → 3 行摘要
  - `/positions` → 仓位一览（symbol/direction/R/unrealized_pnl）
  - `/signals` → 信号引擎 running + queue depth + drop rate
  - `/executor` → enabled + circuit_open + 今日统计
  - `/pending` → 待执行信号列表
  - `/mode` → FULL/OBSERVE/RISK_OFF/INGEST_ONLY 当前状态
  - `/daily-report` → 手动触发日报

### Phase 4：入站控制命令（仅低风险）

- [ ] `/halt [reason]` / `/resume [reason]` → `OperatorCommandService.enqueue(command_type=set_trade_control)`
- [ ] `/reload-config` → `reload_configs()`
- [ ] 审计链：actor=`telegram:{username}:{chat_id}`，回执含 `action_id` + `audit_id`
- [ ] 入站速率限制（`inbound_per_chat_per_minute`）防命令轰炸

### Phase 5（可选）

- [ ] `/trace <signal_id>` 深度链路（多段分块，处理 4096 字节限制）
- [ ] `/v1/admin/notifications/dlq` DLQ 查询端点
- [ ] `/v1/admin/notifications/reload-templates` 模板热重载
- [ ] `/v1/admin/notifications/test` 手动触发事件端点

---

## Phase P5：前端 Dashboard（从 TODO.md 迁入，2026-04-21）

- [ ] 技术栈选型（React/Vue/Svelte + 实时更新方案）
- [ ] 总览控制塔读模型补齐（为 QuantX 首页提供一等交易指导数据）
  - [ ] 账户级资金快照：`balance / equity / free_margin / margin / margin_level / floating_pnl / daily_realized_pnl / currency / updated_at`
  - [ ] 组合级资金聚合：`total_equity / total_free_margin / total_margin / accounts_with_funding / margin_blocked_accounts`
  - [ ] 多币种聚合语义：明确 `base_currency` 或 `fx_normalized_totals`，否则返回 `aggregation_mode = mixed_currency`
  - [ ] 当前交易条件摘要：`risk_action / risk_reason / primary_blocker / margin_check_details / order_protection_requirements`
  - [ ] 总览焦点账户：`focus_account_alias / focus_account_label / focus_basis(active|default|selected)`
  - [ ] 数据节奏元信息：`recommended_poll_interval_seconds / supports_stream / last_quote_at / last_account_risk_at / last_trade_state_at`
  - [ ] 模块级刷新语义：区分 `capital_snapshot` 与 `analysis_context` 的 cadence
  - [ ] 行情快照原生读模型：`symbol / bid / ask / spread / quote_updated_at / quote_freshness_state / recommended_poll_interval_seconds`
  - [ ] 建议下沉为原生总览读模型，避免前端再从 `trade/accounts + trade/state + entry_status` 拼装
- [ ] 核心监控页面（行情/信号/持仓/绩效）
- [ ] 接入后端 API（交易 trace / SL TP 历史 / 投票详情 / 绩效追踪 / 相关性分析）
- [ ] Studio 模块暂保留，新 dashboard 不依赖 Studio 的 agent 模型


---

## Phase R：Research/Mining 模块性能优化（2026-04-21 立项）

> **背景**：当前 `src/ops/cli/mining_runner` 单 run 12mo M5/M15/M30 with workers=3 约 25-30 min。
> 用户需求：在**功能完整、结果一致**前提下加速（不接受减少 provider/analyzer 的做法）。
>
> **总目标**：
> - 单 run 加速 **40-60%**（25min → 12-15min）
> - 跨 run / 增量场景加速 **80-90%**（25min → 30s-5min）
> - 所有 6 provider × 3 analyzer × 全特征全规则保持完整

### R.1 — DataMatrix + Feature 缓存（P1，最高 ROI）

**问题**：每次 mining 都重建 DataMatrix + 重算 feature。即使 `(symbol, tf, window, providers, barrier_configs)` 完全相同的相邻 run 也不复用。占总耗时 50-65%。

**实现**：
- `src/research/core/cache.py` 新模块：`DataMatrixCache` 类
- cache key = `sha256(symbol|tf|start|end|sorted(providers)|barrier_configs_hash|indicator_set_hash)`
- cache 路径：`data/research/cache/datamatrix_<key>.pkl`
- 失效规则：`max(ohlc.updated_at, indicator_series.updated_at) > cache_mtime` 即重建
- `build_data_matrix()` 入口加 `use_cache: bool = True` 参数（测试可禁用）
- LRU 上限：100 文件 / 总大小 5GB（满了 evict 最旧）

**收益**：
- 跨 run **30-50%**（A1 跑完，A2 重叠 close 部分直接复用）
- 单 run 0%（首次仍需算）

**工作量**：1-2 天
**风险**：低（cache miss fallback 到原逻辑）
**测试**：cache hit/miss/失效/size eviction 4 类测试

### R.2 — Polars 替换 Pandas 重热点 provider（P2）

**问题**：`temporal` / `microstructure` / `cross_tf` provider 大量 `df.rolling().apply()`、`df.shift()`，Pandas 实现因 GIL + Python 循环慢。

**实现**：
- 重写 `src/research/features/temporal/provider.py`、`microstructure/provider.py`、`cross_tf/provider.py` 用 Polars 替换 Pandas
- 接口签名不变（输入 `DataMatrix`，输出 `Dict[Tuple[str,str], List[Optional[float]]]`）
- 加 `tests/research/test_polars_equivalence.py`：固定输入下 Polars 输出与 Pandas 输出 numerical equivalence（容差 1e-9）
- `requirements.txt` 加 `polars>=1.0.0`

**收益**：单 run **25-40%**

**工作量**：1 周（3 个 provider × ~200 行 + 测试）
**风险**：中（浮点精度差异）—— 必须 equivalence 测试守护

### R.3 — 增量挖掘 `--incremental`（P3）

**问题**：日常增量场景下，每次都从 12 个月起点重新跑。

**实现**：
- mining_runner CLI 加 `--incremental` flag
- 启动时查 `research_mining_runs` 表找同 `(symbol, tf, experiment_prefix)` 最新 run 的 `end_time`
- 新 run 仅挖 `[last_end, today]` 数据
- 历史 finding 与新 finding 在 `MiningResult.merge_with_historical()` 合并（重排 IC、滚动稳定性更新）
- DataMatrix 用 R.1 cache，only compute 新增段

**收益**：
- 首次跑无变化
- 日常增量 **80-90%**（25min → 30s-5min）

**工作量**：3 天
**风险**：中（merge 语义复杂）—— 加 merge 单元测试 + 等价性测试（incremental + merge ≡ full rerun）

### R.4 — Numba JIT 热路径（P4，**2026-04-22 实测后下调**）

**初版估计（已下调）**：单 run 5-15%
**实测重估**：**< 3%（不值做）**

**实测结论**（2026-04-22 阅码后）：
- `_vectorized_barrier_scan` 前段（窗口矩阵 + tp/sl 命中判断 + argmax）已经是**完全 NumPy 向量化在 C 层执行**，JIT 不会带来加速
- 后段是 `for i in range(n-1)` Python 循环构造 `BarrierOutcome` NamedTuple list —— Numba 对 `Optional[NamedTuple]` list 支持差，强行 JIT 需要重构 BarrierOutcome 为 numpy structured array（高侵入）
- threshold sweep 的 `_evaluate_threshold` 内层循环占 mining 总耗时 < 5%，即使 JIT 达成 5x 加速，整体收益 < 1%
- predictive_power 的 bootstrap permutation 已用 NumPy + scipy 在 C 层
- **真正 Python 层瓶颈在 temporal/microstructure provider 的 pandas `rolling().apply()`** —— 这是 R.2 (Polars) 的目标

**最终决策**：**搁置 R.4**。如果未来要做，必须先把 `BarrierOutcome` 重构为 numpy structured dtype（脱离 NamedTuple），工作量从 1-2 天升到 4-5 天，ROI 反不如优先做 R.2。

### R.5 — 多进程共享内存 base data（P5）

**问题**：3 个 worker 各自从 PG 加载 OHLC + indicators —— 重复 3x I/O + 反序列化（M5 12mo 单 worker 50-100MB）。

**实现**：
- 主进程预加载 base data 到 `multiprocessing.shared_memory`
- worker fork 时通过 shm name attach（零拷贝读）
- `mining_runner.py` 主进程逻辑：先 load → 构造 shm → fork workers
- worker 入口：`shared_memory.SharedMemory(name=...)` attach

**收益**：单 run **5-10%**（I/O 时间减 70%）

**工作量**：1-2 天
**风险**：中（Windows shm 行为差异，要 cross-platform 测试；进程异常退出后 shm 泄漏需 cleanup hook）

### R.6 — Pipeline 流水线化（P6）

**问题**：当前 4 阶段顺序执行：data load → feature compute → barrier → analyzer。analyzer 等 feature 全跑完才启动。

**实现**：
- 把 4 阶段改成生产者-消费者模型
- `feature compute` 完成一个 provider → 立即送 queue → analyzer 消费
- 注意：rule_mining 需要多 provider 特征同时在场，要等 feature 全到齐才能启动；但 predictive_power 可以 per-provider 独立跑
- 用 `concurrent.futures.ThreadPoolExecutor` 协调

**收益**：单 run **20-30%**（消除阶段间等待）

**工作量**：3-5 天
**风险**：中高（数据依赖图复杂，rule_mining 与 feature dependency）—— 必须详细设计 + e2e 测试

### R.7 — SQL 查询优化（P7）

**问题**：mining 启动加载 OHLC + indicator_series。可能存在 chunk pruning 失效或索引未覆盖。

**初版估计（已下调）**：单 run 3-5%
**实测重估（2026-04-22）**：**0%（DB 层已最优，无可优化空间）**

**EXPLAIN ANALYZE 实测结果**（M5 12mo 70K bars）：
```
Custom Scan (ChunkAppend) on ohlc - actual time=3.519..266.622 rows=70293
  ├─ ColumnarScan + Vectorized Filter on _hyper_3_53_chunk
  ├─ Index Scan Backward using compress_hyper_..._symbol_timeframe__ts_meta_min_1_idx
  └─ Buffers: shared hit=3694（全部 cache hit，零 disk I/O）
```

**已具备的优化**（无需新增）：
- ✅ TimescaleDB hypertable chunk pruning 完美生效
- ✅ Columnar 压缩 + Vectorized Filter
- ✅ 现有索引 `idx_ohlc_symbol_tf_time` 完全覆盖 `(symbol, timeframe, time)` 范围查询
- ✅ Buffer cache 命中率 100%（无 disk I/O）

**最终决策**：**搁置 R.7**。SQL 总加载耗时仅 266ms（占 mining 25min < 0.02%），不是瓶颈。

---

### Phase R 综合估算（**2026-04-22 实测后修订**）

| 实施组合 | 单 run 加速 | 跨 run 加速 | 总工作量 | 状态 |
|---|---|---|---|---|
| **R.1 已完成** | 0% | **30-50%** | 0（已交付） | ✅ commit a74beb0 |
| ~~R.4 + R.7~~ | ~~8-20%~~ → **<3%** | 0% | ~~3 天~~ | ❌ 实测搁置（详见各节）|
| **R.2 (Polars)** | **25-40%** | 0% | 1 周 | 🔥 真正的 quick win |
| **R.3 (incremental)** | 0% | **80-90%** | 3 天 | 大改造，收益巨大 |
| R.5 (共享内存) | 5-10% | 0% | 1-2 天 | 可选 |
| R.6 (Pipeline) | 20-30% | 0% | 3-5 天 | 复杂度高 |

**修订后实施顺序**：

1. ~~R.1~~ ✅ **已完成**（跨 run 30-50% 已实现）
2. **R.2 Polars 重写**（次轮重点，单 run 25-40%）
3. **R.3 increment**（+3 天，日常场景 80-90%）
4. R.5 / R.6 视情况
5. ~~R.4 / R.7~~ ❌ **搁置**（实测 ROI < 3% 或 = 0%）

### 实施顺序原版（保留作为方案演进证据）

原序为 R.1 → R.4 → R.7 → R.3 → R.2 → R.5 → R.6（"快赢优先"）。
2026-04-22 R.1 完成后实测 R.4/R.7：发现两者在当前代码 + DB 状态下 ROI < 3%，
原"快赢"假设不成立。修订序为 R.1 → R.2 → R.3。

### 验收标准

- 每个 R.x 独立 PR + 独立测试
- 加 `tests/research/test_mining_perf.py` 性能基准（固定 12mo M5 单 TF 跑 3 次取平均）
- benchmark 报告进 `docs/research/<日期>-mining-perf-baseline.md` 时点快照
- 整体跑通后更新 `codebase-review.md §F` 归档
