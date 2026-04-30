# 2026-04-30 Day-Trading Edge 策略实施 + P0 工具修复

承接 2026-04-29 mined-rule 验证的"mining ROI 不行"结论，本轮按 first-principles
回到"手工策略 + 修回测可信度"路径。完成两项 P0 + 两条 day-trading edge 策略。

## P0 — 回测可信度修复

### P0-A: EF mode lot fallback 验证（已生效）

| 指标 | 修前 | 修后 |
|---|---|---|
| `below_min_volume_for_execution_feasibility` 拒单率 | 84-100% | **0** |
| accepted_entries / total | ~0% | 28/50 = 56% |
| 剩余拒单 | n/a | `exceeds_max_actual_risk_pct` (22)，源于 $2000 账户 + H1 高 ATR 物理约束（非 bug） |

`backtest.local.ini` 已设 `allow_min_volume_fallback=true`，core baseline 保持 false（默认模拟真实 broker 行为）。CLI 不暴露 flag — `.local.ini` 控制已足。

### P0-B: demo_vs_backtest CLI 修复（commit `414c495`）

工具之前**完全不可运行**——SQL 用了不存在的字段：
- demo `trade_outcomes.entry_time` → `recorded_at`（close 时间）
- demo `trade_outcomes.realized_pnl` → `won` (bool) + `price_change`
- demo `signal_events.signal_state` → `direction IN ('buy','sell')`
- backtest `summary['per_strategy']` → `run['metrics_by_strategy']` JSONB

`pf_drift` 删除（demo `price_change` 是 raw 价差，backtest `total_pnl` 是 USD，单位不可比）→ 改 `win_rate_drift` (unit-free)。15 sentinel + 单元测试覆盖。

**实测对账被阻塞**：demo 服务停了 — 30 天仅 7 笔成交（5 笔 price_action / 1 笔 trend_h4 / 1 笔 trendline_touch），**样本不足以做有效对账**。

## P1b — 手工 day-trading edge 策略

按"首先实施成本最低 + alpha 最直接"原则实施 2 条：

### #4 Prior Day H/L Retest（commit `713b934`）

- **alpha 来源**: retail trader 普遍参考"昨日 H/L"作 SR levels
- **入场**: close 触及 prev_day_low/high (≤ 0.5 ATR) + 反转 K 线 → 反向交易
- **出场**: BARRIER mode (sl 1.0 ATR / tp 2.0 ATR / time 12 bars)
- **新 indicator**: `prior_day_levels`（9 单元测）
- **Backtest 1y (2025-04-01~2026-04-26)**:

| TF | trades | WR | PF | PnL | 决策 |
|---|---|---|---|---|---|
| H1 | 23 | 30.4% | 0.52 | -$101 | ❌ 移除（H1 timing 与日级 SR 不匹配） |
| H4 | 6 | 50.0% | 1.66 | +$27.69 | ⚠️ demo_validation（n 太小） |

部署：`status=demo_validation, locked_timeframes=H4, locked_sessions=london,new_york`

### #1 Daily Pivot Reaction（commit `ea44abb`）

- **alpha 来源**: floor pivots R1/R2/S1/S2（计算自昨日 OHLC，retail 第二大关注 SR）
- **入场**: close 触及 nearest level (≤ 0.3 ATR, level ∈ R/S 非 P) + 反转 K 线
- **出场**: 同 prior_day_retest
- **新 indicator**: `daily_pivots`（6 单元测）
- **Backtest 1y H1**: 9 trades / WR 22.2% / PF 0.36 / -$58
- 部署：`status=demo_validation, locked_timeframes=H1`

**与 prior_day_retest 的互补性（防补丁）**：
- prior_day_retest 用 prev_day_H/L（H4 timing）
- daily_pivot_reaction 用 R1/R2/S1/S2（H1 timing）
- 二者 alpha 来源不同（不同 retail SR 概念），不应合并

## 关键发现 — 1 年 backtest 不足以验证 day-trading edge mean-reversion 策略

两条策略加起来 1 年 backtest 仅 38 trades（H1 23+9 + H4 6）。问题：
1. mean reversion 触发条件严格 → 入场频次低
2. day-level SR 一年仅 ~250 次 setup，能形成 confluence 的进一步少
3. 需要 5-10 年数据 + walk-forward 才能 statistical significance

实事求是的下一步路径：
- **不延伸 backtest 调参**（避免 in-sample tuning 陷阱）
- **demo forward**：让两条策略在 demo 上跑 6+ 个月积累 forward evidence
- **同时实施 #2 Pre-news / #3 NY Reversal**（不同 alpha 来源 → 增加 candidate 池）

## 现状决策矩阵

| 类别 | 状态 | 下一动作 |
|---|---|---|
| Mining-derived 策略 | h4_buy_1 唯一 Stage-2 通过但 PF 1.04 < PLAN.md 1.20 | 🟡 框架保留，不再投 mining 计算时间 |
| 现有 14 策略 demo 数据 | demo 30天仅 7 笔，无对账数据 | ⏸ Blocked — 待 demo 重启或扩窗 |
| Day-trading edge 策略 | 2 条上线（demo_validation） | ▶ demo forward 验证 + 实施 #2/#3 |
| backtest EF lot bug | 修复已生效（acc_ratio 56%） | ✅ |
| demo_vs_backtest CLI | 重写 + 15 测覆盖 | ✅ 待 demo 数据足才能跑 |
| max_actual_risk_pct 5% 偏紧 | 22% 入场被拒 | 🟡 见 [account capital 决策点](#account-capital-决策点) |

## Account capital 决策点

EF mode 22 笔被 `exceeds_max_actual_risk_pct=5%` 拒：$2000 × 5% = $100 SL distance ceiling，H1/H4 高 ATR 场景常见 SL > $100。三条出路：

- **B1**：保持 $2000，提 max_actual_risk_pct 到 10%（更激进）
- **B2**：调 default balance 到 $5000+（让正常 1% risk lot 自然 ≥ 0.01；需要实际 capital）
- **B3**：保持现状 + 改 PLAN.md acc_ratio 门禁阈值（认账 demo 时段考核而非 backtest 单点）

这一项**用户决策**，未在本轮实施。

## 关键文件

新增：
- `src/indicators/core/prior_day_levels.py`
- `src/indicators/core/daily_pivots.py`
- `src/signals/strategies/structured/prior_day_retest.py`
- `src/signals/strategies/structured/daily_pivot_reaction.py`

修改：
- `config/indicators.json`（注册 2 个新 indicator）
- `config/signal.ini`（strategy_timeframes / sessions / deployment 块）
- `src/signals/strategies/catalog.py`（注册 2 个新策略，catalog 从 14 → 16 实例）
- `src/signals/strategies/structured/__init__.py`
- `src/ops/cli/demo_vs_backtest.py`（重写 SQL + drift 度量）

测试新增：22 + 18 + 15 = 55 个新单元测试。

## 留给下一轮

按用户战略路径排序：
1. 实施 #2 Pre-news flat / Post-news trend follow（economic_calendar 已有，需要 lead/lag 时间 indicator）
2. 实施 #3 NY Reversal（无需新 indicator，session-aware logic）
3. demo 重启决策（解 demo_vs_backtest 阻塞 + 解 P1a 现有策略审视阻塞）
4. account capital B1/B2/B3 决策
5. 5-10 年历史数据扩展（如有 broker 数据源）让 backtest 真有统计意义
