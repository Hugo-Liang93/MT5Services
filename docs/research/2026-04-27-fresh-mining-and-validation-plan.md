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
| Baseline snapshot (Task 1) | ✅ 2026-04-27 |
| Research config fix (Task 2) | ⏳ pending |
| Barrier filter fix (Task 3) | ⏳ pending |
| Walk-forward service (Task 4) | ⏳ pending |
| Fresh mining run (Task 5) | ⏳ pending |
| Live-aligned backtest (Task 6) | ⏳ pending |
| Demo validation (Task 7) | ⏳ pending |
| Active-guarded proposal (Task 8) | ⏳ pending |
| Doc drift cleanup (Task 9) | ⏳ pending |
| Final decision (Task 10) | ⏳ pending |
