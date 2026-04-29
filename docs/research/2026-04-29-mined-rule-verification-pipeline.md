# 2026-04-29 Mined-Rule 双阶段晋级管线（per-TF lockdown + backtest verification）

## Objective

把 `MinedRuleStrategy` 框架（2026-04-28 commit 9cee05c 引入）从"会跑"演进到"经得起 demo binding 决策"。本快照记录两个 first-principles 修复的实证与下一步建议。

## 背景

2026-04-28 成功跑出 7 条通过 `PROMOTION_GATES` 的 mined specs（`fresh_mining_with_candles_2026-04-28.json`）。第一次回测结果 PnL +124K USD / W/L=7.0（$2K balance 6000% return）触发 anomaly 调查 → 定位 `src/backtesting/engine/portfolio.py` barrier mode TP 走错 chandelier `max_tp_r × initial_risk` 公式（commit 333c1e6 修复）。修复后再次回测出现两个新问题：

1. **Cross-TF 信号语义错位**：H4 spec 在 H1/M30 pipeline 上跑产生 -1081 / -1221 USD，76-95% DD —— 信号 payload 在错误 TF 上失真但运行时无门控
2. **Mining-vs-backtest WR 显著背离**：`structured_mined_h4_sell_2` mining test_wr=58.1% 但 backtest WR=33.3%，差 25pp —— `PROMOTION_GATES` 仅基于 mining 统计，无法预测真实交易引擎下的退化

## Fix 1 — Per-TF lockdown（commit 693e99f）

### 根因

`register_mined_rule_strategies` 仅写 `catalog[name] = MinedRuleStrategy(spec)`，未把 `spec.timeframe` 注入 `BacktestConfig.strategy_timeframes` 白名单。`BacktestEngine` 按 `config.timeframe` 跑全部 strategy，无 per-strategy TF 过滤——所以同一组 specs 被强制跑遍 H4/H1/M30 全部 TF。

### 修复

`register_mined_rule_strategies` 返回 `dict[str, list[str]]`（`{spec.name: [spec.timeframe]}`）。`build_backtest_components` 透出此 map；`backtest_runner` CLI 在构建 `BacktestConfig` 前合并进 `strategy_timeframes` 白名单——复用既有 per-TF 过滤机制（`runner.py:356`）。

### 验证

[`data/artifacts/backtests/mined_specs_per_tf_2026-04-29.json`](../../data/artifacts/backtests/mined_specs_per_tf_2026-04-29.json)

| TF | 出现的 spec | 总 trades | PnL | WR | PF | MaxDD |
|---|---|---|---|---|---|---|
| H4 | h4_buy_1 + h4_sell_2（仅 H4 specs） | 112 | +32.54 | 43.8% | 1.04 | 6.25% |
| H1 | h1_buy_0/3/6（仅 H1 specs） | 436 | -631 | 45.2% | 0.80 | 34.3% |
| M30 | （0 spec — m30_sell_2 0 trades） | 0 | 0 | — | — | — |

跨 TF 泄漏完全消除：H4 表只见 H4 specs，H1 表只见 H1 specs。

## Fix 2 — Stage-2 backtest verification gate（commit cda19f0）

### 根因

`PROMOTION_GATES`（`mined_rule_loader.py:23-30`）基于 mining JSON 中的 train/test hit-rate + barrier hit-rate + cost-after mean_return 做筛选。这是**纯统计推算**，不含 BacktestEngine 的 filter chain / position management / time_exit / signal_exit / breakeven 等真实交易语义。

7 条 mining-promoted specs 的实测背离：

| spec | mining test_wr | backtest WR | backtest PnL | 备注 |
|---|---|---|---|---|
| h4_buy_1 | 63% | 50.7% | +108 | 12pp 衰减，唯一净盈利 |
| h4_sell_2 | 58% | 30.8% | -76 | **27pp 衰减**，最严重 |
| h1_buy_0 | 65% | 46.2% | -227 | 19pp 衰减 |
| h1_buy_3 | 55% | 44.3% | -40 | 11pp 衰减 |
| h1_buy_5 | 53% | N/A | 0 | 条件极少触发，0 trades |
| h1_buy_6 | 53% | 44.8% | -365 | 8pp 衰减但 PnL 大负 |
| m30_sell_2 | 59% | N/A | 0 | 条件极少触发，0 trades |

### 修复

新增 `BACKTEST_VERIFICATION_GATES` + `filter_by_backtest()`（`mined_rule_loader.py`）：

```python
BACKTEST_VERIFICATION_GATES = {
    "min_realized_wr": 0.45,    # 实测 WR floor（含成本，比 mining 宽一档）
    "min_realized_pnl": 0.0,    # 实测净盈利
    "min_realized_n": 20,       # 实测样本至少 20 笔
}
```

晋级管线现为两阶段：

```
Mining JSON
  → Stage 1: filter_promotable() —— PROMOTION_GATES（mining 统计）
  → BacktestEngine（spec 仅在自己 TF 跑，per-TF lockdown）
  → Stage 2: filter_by_backtest() —— BACKTEST_VERIFICATION_GATES（实测）
  → demo binding candidate
```

### 验证

应用 Stage 2 到 7 specs：

| Stage 1 通过 | Stage 2 通过 | drop 原因 |
|---|---|---|
| 7 | **1**（h4_buy_1） | 5 因 PnL 负或 WR<45%；2 因 0 trades |

## h4_buy_1 vs PLAN.md 硬门禁对比

唯一通过 Stage 2 的 spec 是否可入 demo binding？

| PLAN.md gate | h4_buy_1 | 通过？ |
|---|---|---|
| profit_factor ≥ 1.20 | 1.04 | ❌ |
| expectancy > 0 | +1.48 USD/trade | ✅ |
| max_drawdown_pct ≤ 15 | 6.25% | ✅ |
| total_trades ≥ 80 | 73 | ❌ |
| execution_feasibility.accepted_ratio ≥ 0.85 | 未测 | — |
| monte_carlo.p_value ≤ 0.10 | 未测 | — |
| walk_forward.consistency ≥ 0.70 | 未测 | — |

通过 2/4 已测门、4 项未测。**未达 active_guarded 标准。**

## 当前结论

- **Promotions to demo binding this round**: **0**
- **理由**: h4_buy_1 是唯一两阶段通过者，但 PF 1.04 + n=73 双双低于 PLAN.md 硬门禁；其余 6 条在 BacktestEngine 实测下负期望或不触发
- **生产 runtime 装配 mined-rule strategies 是单独工程**：当前 `build_named_strategy_catalog()` 不自动加载 mined specs（保护合同：仅 backtest CLI 通过 `--mined-rules-source` 显式启用）。要把 mined spec 接入 demo-main 实例需要：
  1. `builder_phases/signal.py` 添加 mining JSON path 配置
  2. `factories/signals.py` 调 `register_mined_rule_strategies`
  3. `validate_strategy_deployments` 通过后 `[strategy_deployment.<spec.name>]` 块才合法
  4. 该工程仅在某 spec 真正达 PLAN.md gate 时才值得做

## 下一步

1. **延长 mining 窗口**（2-3 年） + 多 feature 组合，扩 candidate 池——目前单年 1 个通过率 1/7=14%，即使加大窗口也只能期望几个 spec
2. **walk-forward + monte-carlo 直跑 h4_buy_1**：补 PLAN.md 缺失的 3 项门禁数据
3. **审视 h1_buy_5 / m30_sell_2 0-trade 现象**：mining 用 confirmed bar close 评估条件，BacktestEngine 用相同源——0 trades 暗示 fixture / indicator 计算路径有差异（mining 走 `data_matrix`，backtest 走 `pipeline`）
4. **审视 mining-vs-backtest 系统性 WR 衰减**：7 spec 平均衰减 ~14pp，建议在 mining 阶段加入"模拟 BacktestEngine 后退化的 confidence factor"或"使用与 BacktestEngine 等价的 entry/exit 路径打分"

## 关键文件

修改：
- `src/signals/strategies/catalog.py` — register_mined_rule_strategies 返回 timeframe map
- `src/backtesting/component_factory.py` — 透出 mined_rule_timeframes
- `src/ops/cli/backtest_runner.py` — 合并进 BacktestConfig.strategy_timeframes
- `src/signals/strategies/structured/mined_rule_loader.py` — Stage 2 gate

测试新增：
- `tests/signals/test_catalog_mined_rule_register.py::test_register_returns_per_spec_timeframe_map`
- `tests/signals/test_mined_rule_loader.py::test_filter_by_backtest_*`（5 测）

实证产物：
- `data/artifacts/backtests/mined_specs_per_tf_2026-04-29.json` — Fix 1 后的 7-spec 回测
- `data/research/fresh_mining_with_candles_2026-04-28.json` — mining 源
