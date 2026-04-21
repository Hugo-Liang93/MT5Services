# 2026-04-20 三 TF Baseline Review + M30 污染事件

> **⚠️ 时点快照**：记录 **2026-04-20** 在 P7 delta 修复基础上完成 M30/H4 回测后的真实 baseline。
> **不回改正文**。本文件是 [2026-04-19 H1 baseline](2026-04-19-h1-p7-baseline.md) 的后续扩展。
>
> **数据窗口**：2025-04-17 ~ 2026-04-15（12 个月），XAUUSD
>
> **重要事件**：M30 原始回测暴露 `structured_price_action` 污染问题（paper_only 策略被全量评估）。

---

## 三 TF 真实 Baseline

| TF | Trades | WR | PF | Sharpe | MaxDD | 投产价值 |
|----|--------|-----|-----|--------|-------|---------|
| **H1** | 346 | 46.2% | **2.041** | **2.508** | 6.93% | ✅ 唯一可投产 |
| M30（剔除 price_action）| 206 | 39.8% | 1.019 | 0.108 | 13.57% (228 bars) | ❌ break-even |
| H4 | 16 | 43.8% | 1.048 | 0.076 | 3.22% | ⚠️ 频率过低 |

## M30 污染事件（架构警示）

**现象**：M30 原始回测（含 `structured_price_action`）= **1,463 trades / PnL $1.38M / Calmar 118** — 完全假象。

**根因**：
- `structured_price_action` `deployment=paper_only`，在 `signal.local.ini [account_bindings.*]` 里**没绑定任何账户** → 生产中不跑
- 但 `build_backtest_components()` 按 `signal.ini [strategy_timeframes]` **全量注册**，回测评估该策略
- M30 触发 1,258 笔 ≈ 5 笔/天，统计被绑架（compound $10k → $1.4M = 139× 假象）

**剔除 price_action 后**：M30 真实 baseline 只有 206 笔 / PF 1.019 / Sharpe 0.108 —— 几乎 break-even。

## 生产可投产结论

- **H1 是唯一真正有 alpha 的 TF**。Paper Trading 应聚焦 H1
- M30 `breakout_follow` 203 笔 / per-trade avg $1.28 覆盖不了 spread 成本
- M30 `sweep_reversal` / `range_reversion` 各 1-2 笔（几乎不触发）
- H4 `trendline_touch` 16 笔 / 月 1-2 笔（低频辅助信号，保留但非主力）

## 架构缺口：P8 回测注册与账户绑定对齐

**问题**：`build_backtest_components()` 按 `signal.ini [strategy_timeframes]` 全量注册策略，**不读** `signal.local.ini [account_bindings.*]`。后果：`deployment=paper_only` 且未绑定账户的策略（如 `structured_price_action`）在回测中被评估并计入统计，但生产中不跑。

**影响**：回测数据误导决策。M30 baseline 1,463 笔对应 M30 真实生产 206 笔，差 7×。

**候选方向**（未决）：
- 方案 A：`BacktestConfig` 增加 `respect_account_bindings: bool = False` 选项，True 时只注册至少被一个 account_binding 引用的策略
- 方案 B：工具层（`scratch/full_strategy_tf_baseline.py`）固定传入 exclude 列表，明示哪些策略被排除
- 方案 C：在 `strategy_deployment.<name>` 合同里加 `visible_in_backtest: bool = true`，`false` 的策略默认跳过

**前置**：次优先级。H1 baseline 已不受影响（price_action 只绑 M15/M30），生产决策不依赖 M30 数据。

## 旧 baseline 失效清单

以下数据均不应作为未来参数调优/策略评估参考：

- 2026-04-08 M30 16 trades / PF 2.47（delta 失效前）
- 2026-04-19 P5 bugfix 后 473 trades / PF 2.361（delta 失效）
- 2026-04-19 P7 前 201 trades / PF 2.595（HTF bug 修复前）
- 任何 2026-04-19 之前的回测数据

## 后续跟进

- `breakout_follow` solo PF 1.23（H1）仍低于调参目标 1.3+。若要调优，参数空间 ADX 已证实可覆盖（`strategy_params_per_tf`）
- FP.2 `strong_trend_follow` Paper 观察数据需以新回测（WR 55.6% 而非旧 50%）为对照
- M30 策略组合需重审：`sweep_reversal` / `range_reversion` 在 M30 几乎零信号，可冻结 M30 绑定
- H4 `trendline_touch` 保留（低频辅助信号，无副作用）
