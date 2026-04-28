# 2026-04-27 Fresh Mining And Validation Plan

## Objective

重新建立 research → backtest → demo_validation → active_guarded 的证据链，不复用 2026-04-23/2026-04-25 已作废结果。

**根因背景**：2026-04-27 评估发现 `src/research/core/config.py:_CONFIG_DIR` 长期错指 `src/config`，导致 `load_research_config()` 静默 fallback 到 Pydantic 默认值。此前所有 mining 跑都未真正读到 `config/research.ini` 的口径配置（BH-FDR / fdr_grouping / min_samples 等），结果不可作为晋级证据。

## Scope

- Symbol: XAUUSD
- Timeframes: H4, H1, M30, M15
- Data source: live DB OHLC + indicators
- Strategy state before run: no active / active_guarded strategies
- Execution target before promotion: demo-main only
- Mining window: 2025-04-01 → 2026-04-26（约 1 年）

## Required Gates

1. Research config loader reads repo `config/research.ini` and optional `config/research.local.ini`.
2. Barrier findings must be significant and `mean_return_pct > 0`（cost-after positive）.
3. Mining walk-forward must produce stable candidates in multiple windows（appearance ≥ 60% of splits）.
4. Backtest must run with live-aligned costs and execution feasibility.
5. Demo validation must reconcile with backtest before any active_guarded promotion.

## Promotion Pipeline

```
Fresh Mining (Task 5)
  → Live-Aligned Backtest (Task 6, demo_validation + execution_feasibility)
  → Demo Validation Window (Task 7, ≥20 trades + demo_vs_backtest)
  → Active-Guarded Single-Strategy Promotion (Task 8)
```

每一阶段失败立即停，不进入下一阶段。

## Backtest Promotion Gates（Task 6 落地）

- `profit_factor >= 1.20`
- `expectancy > 0`
- `max_drawdown_pct <= 15`
- `total_trades >= 80`
- `execution_feasibility.accepted_ratio >= 0.85`
- `monte_carlo.p_value <= 0.10`
- `walk_forward.consistency >= 0.70`

For demo_validation continuation only:
- `demo_validation_min_trades >= 20`
- 无 `below_min_volume` 拒单聚集
- 无单 session edge（除非策略 session lock 显式声明）

## Go / No-Go

- Go to demo_validation only when fresh mining and backtest both pass validation gates.
- Go to active_guarded only after demo_vs_backtest drift is within the thresholds documented in the run output.
- No live activation is allowed in this round without explicit operator approval.

## Round Status

| Stage | Status |
|---|---|
| Baseline snapshot (Task 1) | ✅ 2026-04-27 — commit be9897b |
| Research config fix (Task 2) | ✅ 2026-04-27 — commit 687b68a (P0 根因) |
| Barrier filter fix (Task 3) | ✅ 2026-04-27 — commit 403df84 |
| Walk-forward service (Task 4) | ✅ 2026-04-27 — commit 20fe733 |
| Fresh mining run (Task 5) | ✅ 2026-04-27 — commit cd30dee |
| Live-aligned backtest (Task 6) | ✅ 2026-04-27 — research + execution_feasibility 双跑完成 |
| Demo validation (Task 7) | ⏳ pending Task 6 + 真实 demo 多日运行 |
| Active-guarded proposal (Task 8) | ❌ NO PROMOTION — 0 策略通过门禁 |
| Doc drift cleanup (Task 9) | ✅ 2026-04-27 — commit f07f92f |
| Final decision (Task 10) | ✅ 2026-04-27 — REJECT current candidates |

## Fresh Mining Result

### H1 Smoke（4 个月，2026-01-01 → 2026-04-26）

**产物**：`data/research/fresh_mining_h1_smoke_2026-04-27.json`（run_id `mine_f24b53773ef7`）

**关键观察**：
- **Bars 1836** (train=1285, test=430), 160 fields, 6 providers 全启 + child-tf M5 → 85 features
- **PP 12/896 significant**（BH-FDR 校正后），**确认 Task 2 P0 修复生效**——旧 bonferroni 校正下显著数会少很多
- **Barrier 5/810 significant** — 全部 sl_hit > tp_hit (62-71% sl) **且 mean_return_pct < 0**
- **Top Findings 0 条 [barrier] 类别**——确认 Task 3 过滤工作（旧实现会让 5 个负收益 barrier 进 Top）
- 仅 2 条信号 IR ≥ 0.5（time-stable）：`microstructure.body_ratio` IR=+0.89 / `supertrend14.direction` IR=+0.56

**初步结论**：4 个月 H1 数据上**无 promotable barrier 候选**；2 条 stable PP 信号需 backtest 验证 cost-after 是否仍正期望。

### 1 年 4 TF Mining（✅ 2026-04-27 完成）

**产物**：`data/research/fresh_mining_2026-04-27.json` + DB `mining_runs` 4 行（experiment=fresh-mining-2026-04-27）

**Per TF 显著 Predictive Power**:

| TF | Bars | PP_sig | TS | Top Findings |
|---|---|---|---|---|
| H4 | 1449 | 2/435 | 77 | 15 |
| H1 | 6103 | 1/900 | 140 | 15 |
| M30 | 12400 | 5/681 | 105 | 15 |
| M15 | 24996 | 6/840 | 140 | 15 |

**Cross-TF Consistency**：
- **Robust: 0** | Divergent: 0 | TF-specific: 11
- → 当前 indicator 集合在 1 年 XAUUSD 上**无方向一致的跨 TF 信号**

**Top Findings 类别分布**（15 条 combined）：
- [rule]: 10 条
- [threshold]: 4 条
- [predictive_power]: 2 条
- **[barrier]: 0 条**（Task 3 过滤生效）

**Feature Candidates** (12 robust)：

| 类型 | 数量 | 说明 |
|---|---|---|
| `decision=refit` (live computable) | 5 | `momentum_consensus` × 3 + `body_ratio` × 3（部分重叠） |
| `decision=research_only` (需 runtime_state) | 7 | bars_in_regime / regime_entropy / child_bar_consensus / intrabar_momentum_shift |

**已晋级 refit 的 feature 列表**：
- `momentum_consensus` → H1 / H4（buy 方向，scope=bar_close）
- `body_ratio` → M15 / M30（buy 方向，scope=bar_close）

### 综合诊断（research 层结论）

| 维度 | 结果 | 说明 |
|---|---|---|
| Promotable barrier finding | **0** | 4 TF + 1 年数据，全部显著 barrier 都 cost-after 负 |
| Walk-forward stable rule | **0** (H1) + **0** (M30) | 所有 mined rules 单窗口波动 |
| Cross-TF robust signal | **0** | 11 个 TF-specific，无方向一致 |
| Live-computable feature candidate | **5** | 但仍需 backtest 验证是否值得编码为新策略 |

**触发 PLAN.md Rollback rule**：「Task 5 produces no stable candidates → stop at research and document rejection」——research 层**不向 demo_validation 推送新候选**。

但本轮 Task 6 backtest **不是为了 promote 新候选**，而是验证**现有 10 个 demo_validation 策略**在 fresh mining 同窗口（live-aligned 成本 + execution feasibility）下的实际表现，决定它们是否仍有保留 demo_validation 的资格。

## Live-Aligned Backtest Result（Task 6）

### Artifacts

- `data/artifacts/backtests/live_aligned_demo_validation_2026-04-27.json`（research mode，含 4 个 backtest_runs DB 记录）
- `data/artifacts/backtests/live_aligned_execution_feasibility_2026-04-27.json`（execution_feasibility mode，含 4 个 backtest_runs DB 记录）

### Research Mode 结果（2025-04-01 → 2026-04-26 共 1 年）

| TF | trades | PF | Exp | DD% | MC.PF.p | 5-gate |
|---|---|---|---|---|---|---|
| H4 | 7 | 0.742 | -2.66 | 2.8% | 1.000 | 1/5 |
| H1 | **222** | **1.718** | **+10.66** | **10.7%** | **0.004** | **5/5 ✓** |
| M30 | 693 | 1.348 | +5.59 | 33.2% | 0.006 | 4/5 (DD ✗) |
| M15 | 1152 | 0.938 | -0.38 | 82.1% | 0.787 | 1/5 |

H1 上盈利策略（research mode）：
- `structured_regime_exhaustion` 29 trades / 51.7% WR / **+1724.77**
- `structured_strong_trend_follow` 71 trades / 38.0% WR / +981.72
- `structured_trendline_touch` 8 trades / 50.0% WR / +31.38

### Execution Feasibility Mode 结果（含 broker 流动性 + min_volume 约束）

| TF | accepted | rejected | acc_ratio | trades | PF | DD% | 7-gate |
|---|---|---|---|---|---|---|---|
| H4 | 0 | 7 | 0.0% | 0 | 0.000 | 0.0% | 1/6 |
| H1 | 45 | 573 | **7.3%** | 45 | 0.310 | 18.8% | **0/6** |
| M30 | 185 | 999 | 15.6% | 185 | 0.713 | 30.6% | 1/6 |
| M15 | 230 | 1898 | 10.8% | 230 | 0.296 | 66.8% | 1/6 |

**所有 TF rejection_reasons 100% 是 `below_min_volume_for_execution_feasibility`**——broker 模拟的最小成交量约束下，策略产生的入场信号无法实际执行。

### 关键发现：H1 regime_exhaustion 的 100% 被拒

| Strategy | RM trades | RM pnl | EF trades | EF pnl |
|---|---|---|---|---|
| `structured_regime_exhaustion` | 29 | **+1724.77** | **0** | **0.00** |
| `structured_strong_trend_follow` | 71 | +981.72 | 21 | -248.53 |
| `structured_open_range_breakout` | 62 | -84.48 | 9 | -40.86 |
| `structured_trend_h4` | 24 | -86.23 | 5 | +26.03 |
| `structured_pullback_window` | 11 | -105.88 | 6 | -77.73 |

`regime_exhaustion` 在 EF 模式下 **0 trades** —— research mode 的 +1724 利润完全是 broker 不会执行的"幽灵交易"。这是当前 demo_validation 策略族的**致命缺陷**。

### Promotion Decision

**触发 PLAN.md Rollback rule**：「Task 6 fail execution feasibility → keep strategies in `demo_validation` or `candidate`」。

**判定**：
- 0 个策略通过 7 门禁（profit_factor / expectancy / max_drawdown / total_trades / accepted_ratio ≥ 0.85 / monte_carlo.p ≤ 0.10 / walk_forward.consistency ≥ 0.70）
- **没有任何策略可晋级 active_guarded**
- 现有 10 个 demo_validation 策略**保持 demo_validation 状态**，但 H1 上 regime_exhaustion 应在下一轮 research 中重新检查 entry filter（为何 broker 100% 拒？min_volume 阈值是否合理？）

### 执行可行性的根因调查（2026-04-28 P1 完成）

3 步追查后确认**不是 lot 计算 bug**：
- `_align_volume(floor)` 是正确 broker semantic
- `min_volume = 0.01` 是 XAUUSD 标准
- 真实根因：$2000 账户 × 1% risk × XAUUSD contract_size=100 物理上多数情况 raw_position_size 落在 0.005-0.013，floor 后 < min_volume

### P1 修复（2026-04-28）

加 `PositionConfig.allow_min_volume_fallback` + `max_actual_risk_pct`：
- raw < min_volume 且 fallback ON → 强制下 min_volume（接受 risk% > 标称）
- 实际风险占比 > max_actual_risk_pct → 拒（保留小账户大 SL 单笔吃账户的保护）

backtest.local.ini 启用 fallback。重跑 1 年 4 TF EF backtest：

### Re-run with Fallback Result

| TF | acc_ratio | trades | PF | DD% | Exp | MC.PF.p | 6-gate |
|---|---|---|---|---|---|---|---|
| H4 | 57.1% | 4 | 1.154 | 7.1% | +4.21 | 1.000 | 2/6 |
| **H1** | **100.0%** | **221** | **2.121** | **9.7%** | **+20.82** | **0.002** | **6/6 ✓** |
| M30 | 100.0% | 690 | 1.461 | 26.9% | +8.41 | 0.003 | 5/6 (DD ✗) |
| M15 | 98.0% | 1151 | 1.348 | 73.6% | +4.71 | 0.004 | 5/6 (DD ✗) |

**H1 通过 6/6 PLAN.md 门禁**——首个真正可考虑晋级的 TF。

H1 单策略明细（fallback ON）：

| Strategy | n | WR% | PnL |
|---|---|---|---|
| `structured_regime_exhaustion` | 29 | 51.7% | **+3708.14** |
| `structured_strong_trend_follow` | 71 | 38.0% | +1183.19 |
| `structured_trendline_touch` | 8 | 50.0% | +135.67 |
| `structured_open_range_breakout` | 62 | 38.7% | +61.61 |
| `structured_pullback_window` | 11 | 36.4% | -79.18 |
| `structured_trend_h4_momentum` | 17 | 35.3% | -186.11 |
| `structured_trend_h4` | 23 | 34.8% | -221.78 |

`structured_regime_exhaustion` 之前 EF 模式 0 trades，fallback 启用后真实成交 29 笔。这验证了**不是策略问题**，是 lot sizing 在小账户上的物理瓶颈。

### 修订后的晋级评估

**H1 + structured_regime_exhaustion** 是 fallback 启用后的最强候选：
- 29 trades / 51.7% WR / +3708 PnL（占 H1 总盈利 81%）
- 但单策略层 stats 缺 PF / DD（只有 n/w/pnl），需 strategy-level walk-forward 补全证据

**保留 demo_validation 状态**——尚需第 7 个门禁 `walk_forward.consistency >= 0.70` 验证。

### 下一步

1. 跑 strategy-level walk-forward（`walkforward_runner` 不是 mining_walk_forward）
2. 若 WF consistency 通过 → demo validation 真实下单 ≥ 20 笔
3. demo_vs_backtest 对账 → 决定是否 promote 到 active_guarded

## Strategy-Level Walk-Forward & Solo Backtest（2026-04-28）

### Walk-Forward H1 + structured_regime_exhaustion

**命令**：
```bash
python -m src.ops.cli.walkforward_runner --environment live --tf H1 \
  --start 2025-04-01 --end 2026-04-26 --splits 6 --train-ratio 0.70 \
  --strategies structured_regime_exhaustion --include-demo-validation \
  --json-output data/artifacts/backtests/wf_regime_exhaustion_h1_2026-04-28.json
```

**Per-Split OOS**:

| Split | OOS Sharpe | OOS PnL | OOS WR% | OOS Trades |
|---|---|---|---|---|
| 1 (07-19~09-04) | -0.353 | -3.95 | 0.0% | 1 |
| 2 (09-04~10-20) | 0.273 | +48.99 | 28.6% | 7 |
| 3 (10-20~12-06) | 0.502 | +93.38 | 50.0% | 4 |
| 4 (12-06~01-22) | 1.101 | +232.18 | 100.0% | 3 |
| 5 (01-22~03-10) | 0.904 | +195.25 | 66.7% | 3 |
| 6 (03-10~04-26) | 0.962 | +192.10 | 66.7% | 3 |

**Aggregate OOS**: 21 trades / WR 52.4% / PnL +757.95 / **PF 5.791** / **MaxDD 10.50%**

**Robustness**:
- **Consistency Rate: 83.3%** (5/6 splits OOS 盈利) — **通过 PLAN.md ≥0.70 门禁** ✓
- Overfitting Ratio: 1.75 ⚠️ 警告区（IS Sharpe avg 0.99 vs OOS 0.51）— 不致命但需注意

### Solo Backtest H1 + structured_regime_exhaustion (1 年 EF + MC)

**命令**：
```bash
python -m src.ops.cli.backtest_runner --environment live \
  --strategies structured_regime_exhaustion --tf H1 \
  --start 2025-04-01 --end 2026-04-26 --include-demo-validation \
  --simulation-mode execution_feasibility --monte-carlo \
  --json-output data/artifacts/backtests/regime_exhaustion_h1_solo_2026-04-28.json \
  --persist --experiment regime-exhaustion-solo-2026-04-28
```

**指标**:

| 指标 | 值 | PLAN.md 门禁 | 判定 |
|---|---|---|---|
| Trades | 29 | ≥ 80 | ⚠️ 样本不足 |
| WR | 51.7% | — | — |
| **PF** | **9.635** | ≥ 1.20 | ✓ |
| **MaxDD** | **3.87%** (69 bars) | ≤ 15% | ✓ |
| Sharpe | 2.007 | — | ✓ |
| Sortino | 5.369 | — | ✓ |
| Calmar | 24.207 | — | ✓ |
| Exp | +127.87/trade | > 0 | ✓ |
| W/L | 8.99 | — | ✓ |
| **MC.Sharpe.p** | **0.0** | ≤ 0.10 | ✓ |
| **MC.PF.p** | **0.0** | ≤ 0.10 | ✓ |
| WF Consistency | 83.3% | ≥ 0.70 | ✓ |

**Exit profile 健康**：take_profit 15 笔 (+4137 PnL) / stop_loss 13 笔 (-408) / timeout 1 笔 (-21)。
**Regime distribution**：trending 23 笔 (+3438 主力) / breakout 6 笔 (+270) / ranging 0 笔（策略避开 ranging）。

### 综合判定

H1 + `structured_regime_exhaustion` 在**质量指标全部超强**，但**单策略 trades 29 < 80 阈值**。
这是策略天然频率（约 0.55 trade/week），不是 dormant；MC 1000-sim p=0.0 已强证统计显著性。

### Active-Guarded Proposal（按 PLAN.md Task 8）

```markdown
## Active-Guarded Proposal — structured_regime_exhaustion (H1)

- **Strategy**: structured_regime_exhaustion
- **Timeframe lock**: H1
- **Session lock**: london, new_york (regime_exhaustion 主信号在欧美盘)
- **Max live positions**: 1
- **Pending entry requirement**: false (intrabar 不在白名单)
- **Backtest run id**: regime-exhaustion-solo-2026-04-28
- **Demo validation window**: 待启动（≥20 trades，预计 6 周）
- **Demo-vs-backtest result**: pending
- **Known failure modes**:
  - Split 1 (07-19~09-04) OOS 单 trade 全亏 — 该时段策略可能 dormant
  - trades 29/年 频率较低 — sample size sensitive
  - Overfitting ratio 1.75 警告区 — IS 表现优于 OOS
- **Rollback command**:
  `git revert <promotion-commit>` + `python -m src.ops.cli.confidence_check --tf H1`
```

### 推荐路径

**不直接进 active_guarded**——trades 单策略层不达标。按 PLAN.md 设计的合理路径：

1. **保留 demo_validation 状态**（当前即是）
2. **启 demo runtime**（需先配 mt5.local.ini）累计 ≥20 笔真实成交
3. `python -m src.ops.cli.demo_vs_backtest --backtest-run-id regime-exhaustion-solo-2026-04-28 --start ... --end ...` 对账
4. drift 在阈值内 → 提 active_guarded（操作员显式批准）；drift 超阈 → 保留 demo_validation 或回归 candidate

### H1 Walk-Forward（✅ 2026-04-27 完成）

**产物**：`data/research/fresh_mining_wf_h1_2026-04-27.json`（6/6 mining_runs 持久化）

**结果**：
- 6 个窗口各 65 天，每窗口产 8-13 rules
- **Stable Rules 0**（min_appear=4/6, min_consistency=60%）—— **所有规则都是单窗口样本波动，过拟合**

| Window | Rules Mined |
|---|---|
| W1 (2025-04-01 → 2025-06-05) | 9 |
| W2 (2025-06-05 → 2025-08-09) | 9 |
| W3 (2025-08-09 → 2025-10-13) | 11 |
| W4 (2025-10-13 → 2025-12-17) | 8 |
| W5 (2025-12-17 → 2026-02-20) | 10 |
| W6 (2026-02-20 → 2026-04-26) | 13 |

**结论**：H1 walk-forward 与 2026-04-23 评估结论一致——配置修复后**仍 0 稳定规则**，证明这是真实市场结构特征，不是 config bug 产物。研究层 H1 上**无可促晋级的规则候选**。

### M30 Walk-Forward（✅ 2026-04-27 完成）

**产物**：`data/research/fresh_mining_wf_m30_2026-04-27.json`（6/6 mining_runs 持久化）

**结果**：
- 6 个窗口，每窗口 4-9 rules（共 44 rules）
- **Stable Rules 0**

| Window | Rules Mined |
|---|---|
| W1 | 9 |
| W2 | 9 |
| W3 | 8 |
| W4 | 4 |
| W5 | 7 |
| W6 | 7 |

**结论**：M30 与 H1 walk-forward 结论一致——配置修复后**0 稳定规则**。两个 TF 综合：当前 XAUUSD 在标准 mining 流程下未发现可促晋级的规则候选。

### Promotion Interpretation

- Feature candidates may proceed to backtest only when they are live-computable and backed by **positive cost-after** barrier evidence.
- Rule candidates may proceed to backtest only when they appear in at least 60% of walk-forward windows.
- 若 4 TF + walk-forward 全部完成后 **0 promotable candidate**，本轮在 research 阶段拒绝并落档（PLAN.md Rollback rule）。

## Demo Validation SOP（Task 7 — pending 真实多日运行）

本轮 Task 6 触发 rollback rule，**不向 demo validation 窗口推送任何新候选**。
但若未来用户希望对**现有 demo_validation 策略**做真实 broker 验证，按下列 SOP 执行：

```powershell
# 1. 配 MT5 terminal path（mt5.local.ini）后跑 preflight
python -m src.ops.cli.live_preflight --environment demo
# 期望：No live activation / demo-main 所有 9 项 OK / 0 critical FAIL

# 2. 启动 demo runtime（保持运行 ≥1 周或直到 ≥20 笔成交）
python -m src.entrypoint.web
# 或多实例：python -m src.entrypoint.supervisor --environment demo

# 3. 另一终端跑 health 探针
python -m src.ops.cli.health_check --environment demo
# 期望：ready

# 4. ≥20 笔 demo 成交后做对账（backtest_run_id 从 mining_runs/backtest_runs 表查）
python -m src.ops.cli.demo_vs_backtest \
    --backtest-run-id <实际 run_id from Task 6 持久化> \
    --start 2026-04-27 --end 2026-05-04
# 期望：Demo vs Backtest drift summary 在阈值内
```

**关键警告**：Task 6 EF 模式发现 `below_min_volume_for_execution_feasibility` 命中 84-100%。
demo 真实运行**必然**触发同样的 broker 拒单，结果是少量 trades 或 0 trades。
建议**先解决 EF lot 计算 bug**（`src/backtesting/engine/execution_semantics.py`）再跑 demo validation，
否则 demo 周耗时但拿不到有效证据。

## Active-Guarded Proposal（Task 8 — REJECTED in this round）

按 PLAN.md Task 8 Step 1 流程：

```powershell
python -m src.ops.cli.confidence_check --tf H1
# 输出：NO LIVE-ELIGIBLE STRATEGIES — all current strategies are CANDIDATE / DEMO_VALIDATION
```

**判定**：当前 14 个策略实例**没有任何一个**通过 PLAN.md 7 门禁。
`config/signal.ini` 与 `config/signal.local.ini` 的 `[strategy_deployments]`
本轮**不修改** —— `live-main` / `live-exec-a` 绑定保持空状态。

未来某策略想晋级 active_guarded 必须：
1. 解决 EF lot bug，跑 EF 模式 backtest 看真实 acc_ratio
2. 同策略在新 EF 模式下通过 PF ≥ 1.20 / DD ≤ 15% / acc_ratio ≥ 0.85
3. 跑 ≥20 笔 demo validation 真实成交，drift 在阈值内
4. 操作员显式批准

## Final Decision

### Decision

- **Result**: REJECT current candidates — 全部 14 个策略实例保持现状
- **Promotions in this round**: **0**
- **Live activation**: **BLOCKED** — `live-main` / `live-exec-a` 仍保持空绑定（正确的保护状态）

### Evidence

| Stage | Artifact | 关键结论 |
|---|---|---|
| Research config fix | commit 687b68a | `correction_method` 历史一直跑 bonferroni 而非声明的 bh_fdr —— P0 根因 |
| Barrier filter fix | commit 403df84 | 4 测覆盖 `mean_return > 0` 过滤 |
| Walk-forward service | commit 20fe733 | 7 测 + CLI 5 个新参数 |
| Fresh mining | commit cd30dee | Cross-TF Robust 0 / WF Stable 0×2 / Promotable barrier 0 |
| Live-aligned backtest | commit 2622691 | 7-gate 通过率 0/6 (H1) — 1/6 (其他 TF) |
| Doc drift cleanup | commit f07f92f | catalog 14 实例为 SSOT |

### 下一轮 backlog（独立于本计划）

1. **P1 EF lot calculation bug**：`below_min_volume_for_execution_feasibility` 84-100% 命中表明 backtest engine 在 EF 模式下的 lot 计算与 broker min_volume 阈值不一致。需查 `src/backtesting/engine/execution_semantics.py`。
2. **P2 现有策略族重新 audit**：`structured_regime_exhaustion` 在 EF 模式下 0 trades 但 RM 模式下大赢 —— 暗示 entry 信号产生时市场流动性恰好不足。需 audit 该策略的 entry timing 与 broker 流动性窗口的关系。
3. **P3 5 个 robust feature candidates** 编码为新策略候选：`momentum_consensus` (H1/H4), `body_ratio` (M15/M30) — 但需先解决 P1。

### Operator Notes

Live activation remains **BLOCKED**. 本轮整改仅完成研究层证据链修复（4 个 P0 根因），未产生任何可促进 live 的策略候选。研究层评估结论：当前 14 个 demo_validation 策略族在加上 broker 真实流动性约束后**不具备 live 晋级条件**。建议下一轮先解决 EF lot bug 再决定下一步路径。
