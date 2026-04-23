# TODO

> **📋 文档职责**（和兄弟文档的分工，新 session 开读前需理解）：
>
> | 文档 | 职责 |
> |---|---|
> | **本文（TODO.md）** | **当前 sprint 活跃待办**（可动作、本周~本月内推进）|
> | [`docs/design/next-plan.md`](docs/design/next-plan.md) | **长期技术方案**（Phase 级规划，跨 sprint）|
> | [`docs/codebase-review.md`](docs/codebase-review.md) | **已完成归档** + 风险台账 + 历史整改记录 |
> | [`docs/research/<日期>-*.md`](docs/research/) | **时点数据快照**（baseline 表、gap 分析等）|
> | [`CLAUDE.md`](CLAUDE.md) | AI 操作规则 + 配置 / 策略 / SOP 速查 |
>
> **内容迁移规则**（防止再次混乱）：
> - 任务完成 → 本文条目打勾；大块阶段完成后**整段移**到 `codebase-review.md §F` 归档
> - baseline / 回测快照 → **不写本文**，写 `docs/research/<日期>-<主题>.md`
> - Phase 级长期规划 → 写 `docs/design/next-plan.md`，本文只列"本 sprint 要推进的子任务"
>
> 按优先级分组。核心原则：**先验证再优化再上线**。

---

## 📍 当前状态（2026-04-23）

**阶段定位**：P0 **当前闭环**（挖掘基线 2026-04-22）——架构调整持续期，用单闭环
模式避免过去"每次架构改动都作废 paper 数据"的循环混乱。本轮完成（无论成败）
才启动下一轮。

**架构基线 snapshot**：
- P4 research↔backtesting 解耦（commit `3379ec8`）
- 挖掘方法学 Gap 1/2/3 修复 + BarrierPredictivePower analyzer（commits `f16f053..231804a`）
- B-1 trend_continuation htf_adx_upper 门控（commit `d5bc224`）

**最新 mining 快照**：
- 2026-04-22 weekly mining（H1+M30 / 6.5mo / 已 persist research_mining_runs）
- 见 docs/codebase-review.md §F 2026-04-22 和 2026-04-23 两段

**冻结策略**（不在本轮动）：
- `structured_trend_continuation`：置信度负相关（P6 长期项，B-1 已验证 adx_upper 不能替代）
- `structured_lowbar_entry` / `structured_session_breakout`：入场质量不足

**历史 baseline/paper 快照**（已过期，仅供溯源）：
- [2026-04-19-h1-p7-baseline.md](docs/research/) — H1 P7 架构快照
- [2026-04-20-tf-baseline-review.md](docs/research/) — 三 TF + 污染事件
- 2026-04-21 paper session `ps_423ffdbf133f` —— 架构已变，**不作评估依据**

---

## P0: 当前闭环（2026-04-23 挖掘基线）

> **反循环纪律**（根治 2026-04 之前"每次挖掘→重跑回测→paper 任务堆积"）：
>
> - **单闭环原则**：Step 1 开始后，Step 1~3 作为单一闭环，期间不启动其他挖掘/回测任务
> - **架构改动中断规则**：若中途发现架构 bug 必须修，先回滚本轮 snapshot 记"因架构调整中断"，修完重启新一轮
> - **完成即归档**：每轮闭环完成（无论成败）整段移到 `codebase-review.md §F`，本文只保留"P0 下一轮"空 header
> - **Paper 任务不累积**：本文不再维护多轮 paper 任务；过期 baseline 一律丢弃

### Step 1: 编码挖掘候选 → 回测验证 ⏳ 本周

**候选池**（2026-04-22 mining top findings）：
- H1 Rule #3: `adx<=41.74 AND macd<=8.66 AND minus_di<=20.26 → buy` (test 74.6% / n=114)
- M30 Rule #1: `macd>-13.32 AND cci>-241.14 AND minus_di>20.56 → sell` (test 59.4% / n=955)

**动作**：
- [ ] 选 1-2 条候选编码成新策略（或加参数到现有活跃策略）
- [ ] 单策略 3 月 backtest，记录 PF/Sharpe/trades 到 `codebase-review.md §F`
- [ ] **判定门槛**：solo PF > 1.2 且 trades > 50 → 进 Step 2；否则记"未过门槛"结束本轮

### Step 2: Paper Trading 启动 ⏳ 下周

- [ ] Step 1 通过门槛的策略注册 `status = paper_only`，写 `signal.local.ini`
- [ ] OBSERVE 模式启动，记录**启动时间戳 + 参数 snapshot 的 git commit hash**
- [ ] 运行满 7 天（不足也等满，不提前结论）

### Step 3: Paper 评估 ⏳ 再下周

- [ ] `python -m src.ops.cli.paper_vs_backtest` 对比 paper 实际 vs 回测预期
- [ ] **判定**：成交率 > 70% + PF drift < 30% → 升级 `active_guarded`；否则回 Step 1 调参
- [ ] 结果写入 `codebase-review.md §F`，**不在 TODO.md 累积残留**

---

## P3: 实盘试运行（P0 Step 3 通过后）

> 目标：最小手数 + 最保守风控。非当前 sprint 动作。

- [ ] `risk.local.ini`: `daily_loss_limit_pct = 3.0`, `max_volume_per_order = 0.01`
- [ ] FULL 模式运行 1-2 周（逐日查信号质量 / 交易审计 / 持仓 / errors.log）
- [ ] 确认无异常后逐步放大手数

---

## 🔴 长期未决工作（独立于 P0 闭环，跨多 session）

### P6：trend_continuation 置信度重设计（长，跨多 session）

**问题**：`raw_confidence = base + why×0.15 + when×0.15 + where×0.10 + vol×0.05` 与实盘胜率**负相关**——高 confidence 反而触发过拟合场景。

**候选方向**：
- [ ] 分析哪些 (regime, direction) 组合的高 confidence 对应低 WR
- [ ] 考虑加入 **in-sample confidence 再校准**（基于历史 bucket 的 calibration）
- [ ] 或重写 Why/When/Where 评分逻辑
- [ ] 目标：solo min_conf=0.45 时 PF > 1.2（当前 0.61）

### P8：回测注册与账户绑定对齐 ✅ 已完成（2026-04-22）

2026-04-22 按 ADR-009 方案 D 落地：`BacktestConfig.include_paper_only: bool = False`，默认只评估 `allows_live_execution()` 的策略（ACTIVE + ACTIVE_GUARDED），CANDIDATE + PAPER_ONLY 均默认排除。`backtest_runner --include-paper-only` 用于 paper-shadow 回测。详见 [ADR-009](docs/design/adr.md#adr-009) 与 [codebase-review.md §0f](docs/codebase-review.md)。

### P11：QuantX 策略评估页结果读口补齐（中优先，前端已落页）

**背景**：QuantX 前端已新增 `Lab / Evaluation` 策略评估页，用于判断某个 backtest run 是否值得继续给资金、给流量、给实盘权限。当前页面已能消费：
- `/api/research/backtests/summary`
- `/api/research/backtests/job/[runId]`
- `/api/research/backtests/evaluation-summary/[runId]`
- `/api/research/backtests/evaluations/[runId]`

但真实联调结果显示：`evaluation-summary / evaluations / job` 细节常为空或 fallback，walk-forward 与 correlation-analysis 只有“发起任务”的 POST，没有可稳定读取结果的 detail read model。前端当前只能用退化装配维持可读性，不能算评估闭环。

**目标**：把“策略评估页”所需结果收成稳定的后端事实源，不再依赖前端 fallback 拼图。

- [ ] 提供 `GET /v1/backtest/history/{run_id}/walk-forward` 或等价 detail read model
  - 返回每个窗口的 `is/oos/walk_forward` 分段结果
  - 至少包含：`window_label / pnl / max_drawdown / win_rate / trade_count / parameter_set`
  - 前端用途：`稳定性检视` 不再用 fallback 分段

- [ ] 为 `correlation-analysis` 提供可读取的结果口，而不只是查询型 POST
  - 允许两种实现之一：
    - `GET /v1/backtest/results/{run_id}/correlation-analysis`
    - 或先 POST 生成，再通过 `run_id / task_id` 查询结果详情
  - 返回：`correlated_features / penalty / feature_groups / summary`
  - 前端用途：后续补 `参数稳定性 / 相关性` 面板

- [ ] 规范化 `evaluation-summary` 返回字段
  - 现在前端只能猜：`total_pnl / net_profit / profit_factor / pf / max_drawdown / drawdown / expectancy / avg_rr / avg_r_multiple`
  - 需要收敛为稳定字段合同，建议至少固定：
    - `total_pnl`
    - `total_trades`
    - `win_rate`
    - `profit_factor`
    - `max_drawdown`
    - `expectancy_r`
    - `avg_r_multiple`
    - `monthly_returns[]`
  - 前端用途：核心指标板、热力图和部署建议不再靠 alias 猜字段

- [ ] 规范化 `evaluations` 序列明细
  - 当前前端只能猜：`equity / balance / pnl / total_pnl / drawdown / underwater / max_drawdown / date / timestamp / label`
  - 需要固定一条评估序列 read model，建议每点至少带：
    - `label`
    - `equity`
    - `drawdown`
    - `timestamp`
  - 前端用途：`净值与回撤` 主图不再使用退化序列

- [ ] 在 backtest detail 中补执行现实性字段
  - 建议增加可直接渲染的：
    - `spread_sensitivity`
    - `slippage_sensitivity`
    - `broker_variance`
    - `session_dependence`
  - 前端用途：`执行现实性` 现在还是启发式占位

- [ ] 在 backtest detail 中补交易结构字段
  - 建议增加：
    - `avg_hold_minutes`
    - `max_loss_streak`
    - `mfe_distribution`
    - `mae_distribution`
    - `holding_buckets`
  - 前端用途：`交易结构` 不再只是兜底卡片

**验收标准**
- [ ] 前端打开 `/lab/evaluations/{runId}` 时，不再出现“当前评估图使用退化数据装配”
- [ ] `净值与回撤 / 月度收益热力图 / 稳定性检视 / 执行现实性 / 交易结构` 都能直接来自后端 detail read model
- [ ] walk-forward 与 correlation-analysis 结果可以按 `run_id` 稳定回看，而不依赖重新发起任务
- [ ] `evaluation-summary` 与 `evaluations` 字段命名收敛，不再需要前端 alias 猜测

### P12：QuantX 高收益图表数据补齐（高优先，前端结构已落）

**背景**：QuantX 前端当前已经把真正高收益的图表结构落出来了，但其中有 3 类图还停在“过渡版本”或“无法可靠绘制”的状态。问题不是前端不会画，而是后端还没有给出稳定、可回看的事实源。

当前前端已落地的相关结构包括：
- `Cockpit` 风险矩阵：当前只能先按 `账户 x 风险来源（持仓/待执行/告警）` 渲染，等真实 `账户 x 品种` 暴露矩阵
- `Accounts` 动作台：已有 `价格阶梯图`、`Freshness 时间带`，下一张最高收益图是 `净值 / 浮盈亏 / 保证金占用` 时序图
- `Trades` 复盘台：已有 `价格带复盘`、`生命周期时间线`、`执行偏差`，但缺 `价格路径` 与 `回执时间线`

**目标**：只补那些能显著缩短交易员判断时间、且前端已经有明确落点的字段与读口，不为低收益图表扩接口。

- [x] `Cockpit`：为风险矩阵补真实 `账户 x 品种` 暴露明细 ✅ 2026-04-22
  - 扩 `GET /v1/cockpit/overview`：`exposure_map.mode = "account_symbol"` + `exposure_map.risk_matrix[]`（账户-major）
  - 每 cell 含：`symbol / gross_exposure / net_exposure / pending_exposure / position_count / pending_count / risk_score`
  - `risk_score = cell.gross / account.total_risk`（账户内占比），`net = buy - sell`
  - 新增 SQL `aggregate_pending_orders_by_account_symbol()`（status ∈ {'placed','pending'}）
  - 保留 symbol-major `entries[]` 向后兼容；前端可按 mode 分支消费
  - 详见 [quantx-canonical-ia.md P12-1 段](docs/design/quantx-canonical-ia.md)

- [ ] `Accounts`：提供账户短周期风险时序读口
  - 建议二选一：
    - 扩 `GET /v1/execution/workbench?account_alias=...&include=timeseries`
    - 或单独提供 `GET /v1/execution/workbench/{account_alias}/timeseries`
  - 返回最近 `30m / 1h / 4h` 的序列点，每点至少包含：
    - `timestamp`
    - `balance`
    - `equity`
    - `floating_pnl`
    - `margin_used`
    - `free_margin`
    - `margin_level`
  - 可选增加：
    - `positions_count`
    - `pending_count`
    - `risk_state`
  - 前端用途：绘制账户动作台里最高收益的一张图：`净值 / 浮盈亏 / 保证金占用` 时序图

- [ ] `Trades`：提供交易持有路径与回执阶段时间线
  - 建议扩 `GET /v1/trades/{trade_id}` detail read model
  - 新增 `price_path[]`：
    - `timestamp`
    - `price`
    - `running_pnl`（可选）
    - `mfe_to_date`（可选）
    - `mae_to_date`（可选）
  - 新增 `execution_receipts[]`：
    - `phase`
    - `accepted_at`
    - `completed_at`
    - `status`
    - `receipt_id`
    - `audit_id`
    - `effective_state`
  - 建议 phase 至少覆盖：
    - `signal`
    - `precheck`
    - `entry`
    - `protect`
    - `exit`
  - 前端用途：
    - `价格路径图`：回答进场后先顺还是先逆、出场是不是太早
    - `回执时间线`：回答到底卡在预检、入场、保护还是退出

- [ ] `Trades`：规范 detail 里的计划/实际价格字段命名
  - 当前前端已经能消费：
    - `plannedEntry`
    - `actualEntry`
    - `plannedExit`
    - `actualExit`
    - `slippage`
    - `mfe`
    - `mae`
  - 需要后端继续收敛，避免再靠 alias 猜测：
    - `planned_entry`
    - `actual_entry`
    - `planned_exit`
    - `actual_exit`
    - `slippage`
    - `hold_duration_seconds`
    - `mfe`
    - `mae`
    - `realized_r`
    - `fees`
    - `swap`

- [ ] `Intel`：为机会散点图预备评分维度（次优先）
  - 当前前端刻意没有硬上机会散点图，因为数据只有列表文案，不足以支撑高质量判断
  - 建议未来在 `GET /v1/intel/action-queue` 中补：
    - `expected_edge_score`
    - `guard_friction_score`
    - `freshness_score`
    - `execution_difficulty`
    - `priority_score`
    - `detected_at`
    - `fresh_until`
  - 前端用途：后续再做 `机会散点图 / 机会衰减图`，当前不阻塞主工作流

**优先级顺序**
- [x] P12-1：`Cockpit` 账户 x 品种风险矩阵 ✅ 2026-04-22
- [ ] P12-2：`Accounts` 账户短周期风险时序
- [ ] P12-3：`Trades` 价格路径 + 回执时间线
- [ ] P12-4：`Intel` 评分维度

**验收标准**
- [ ] `Cockpit` 不再使用过渡版风险矩阵，而是直接显示 `账户 x 品种` 风险热力图
- [ ] `Accounts` 能显示最近一段时间的 `equity / floating pnl / margin` 时序，而不是只有静态快照
- [ ] `Trades` 能直接显示 `价格路径图` 与 `回执时间线`，不再只靠价格带和启发式生命周期
- [ ] 前端不再需要为上述 3 类图表继续使用“待补后端数据”说明或退化推断

---

## 📋 下 sprint 候选

**P4 架构演进** / **Telegram Phase 2.5+** / **P5 前端 Dashboard** → 详见 [`docs/design/next-plan.md`](docs/design/next-plan.md)

---

## 历史归档

所有 `已完成` 段落已迁入 [`docs/codebase-review.md §F`](docs/codebase-review.md)。包括：
- 2026-04-20 P9 前端读侧 API 全套 + P10 QuantX 主控台闭环
- 2026-04-17 Telegram Phase 1 + Phase 2 + FP.2 strong_trend_follow
- 2026-04-14 Intrabar 交易链路
- 2026-04-06 架构清理 + 策略框架重构
- 2026-04-05 配置 & 基础设施清理
