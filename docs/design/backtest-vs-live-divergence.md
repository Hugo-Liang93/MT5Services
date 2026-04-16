# 回测 ↔ 实盘差异白皮书

本文档记录回测模块 (`src/backtesting/`) 和实盘交易模块 (`src/trading/` + `src/signals/`)
在**无法完全消除**的物理差异。配合已收口的一致性修复使用，供研究迭代时校准回测数字到实盘期望。

## 完全一致（已收口）

| 模块 | 共用契约 |
|---|---|
| 信号评估 | `SignalModule.evaluate()` |
| 过滤器链 | `SignalFilterChain`（回测通过 `BacktestFilterSimulator` 直接构造同一 class） |
| 仓位计算 | `compute_trade_params()` |
| 出场规则 | `evaluate_exit()` |
| Regime 检测 | `MarketRegimeDetector` |
| Chandelier / Breakeven / Trailing 规则参数 | `ChandelierConfig` |
| **StrategyDeployment 契约** | `allows_runtime_evaluation()` / `locked_timeframes` / `locked_sessions` / `max_live_positions` / `require_pending_entry`（回测已于 2026-04-16 补齐 gate） |
| **Equity Curve Filter** | `EquityCurveFilter` 同一 class；回测采样同 `record_equity()` 契约 |
| 周末强平 / EOD | `check_weekend_flat` / `check_end_of_day` 纯函数 |
| PnL 熔断 | 连败触发语义一致（cooldown 单位 bars↔minutes 可按 TF 换算） |

## 无法完全消除的物理差异

### 1. Peak 价更新粒度

| | 实盘 | 回测 |
|---|---|---|
| 更新源 | `PositionManager._reconcile_loop`（`position_reconcile_interval=10s`） | 每根 bar 收盘一次 |
| 数据来源 | `mt5_pos.price_current`（MT5 端 10s 轮询） | `bar.high`（M15 = 每 15 分钟） |
| 精度 | 每 10 秒一次"快照" | 每 bar 一次但拿到**该 bar 真实最高点** |

**讽刺点**：实盘"看起来"更精细（10s 轮询），但**可能漏掉 bar 内瞬时高点**；回测 bar.high 反而是**该 bar 最高价的真值**。

**偏差方向**：不确定。两者都是对 tick-level peak 的近似，误差方向相反但量级相近。

**推荐校准**：不校准。对绝大多数策略 < 5% Sharpe 影响。

### 2. 入场成交价

| | 实盘 | 回测 |
|---|---|---|
| market 单 | MT5 real-time ask/bid | `bar.close + slippage + 半 spread` |
| pending 单 | broker tick-level 触发 + broker 真实成交 | `_resolve_pending_fill()` 按边界价+跳空处理 |

**偏差方向**：
- 正常时段：回测 mid + 半 spread ≈ 实盘 ask，差异 < 1 pt，几乎可忽略
- 高波动时段（非农/议息）：回测固定 slippage=3 vs 实盘尾部 20+，**回测偏乐观 10-50 pt**
- 已规避场景：`FilterSimulator.filter_economic_enabled=true` 把非农前后屏蔽（默认开启）

**推荐校准**：不校准（已用过滤器规避最坏场景）。如果策略要穿越非农开仓，预留 20% buffer。

### 3. MarginAvailability

| | 实盘 | 回测 |
|---|---|---|
| 检查 | `PreTradeRiskService.MarginAvailabilityRule` 查 MT5 free margin | **无**（物理上回测没有 broker margin 数据） |

**偏差方向**：保证金不足时实盘拒单，回测仍开仓 → **回测仓位/频率可能高估 1-5%**（通常可忽略，除非高杠杆）。

**推荐校准**：不修复（物理缺失）。实盘配置合理 `max_volume_per_day` 能近似覆盖。

### 4. Intrabar 模拟

| | 实盘 | 回测 |
|---|---|---|
| 启用后行为 | 子 TF close 驱动父 TF intrabar → IntrabarTradeCoordinator → `_handle_intrabar_entry` | **完全无** intrabar 模拟 |

**偏差方向**：若实盘启用 `intrabar_trading=true`，回测会**系统性低估**信号频率 20-50%。

**推荐校准**：当前 `intrabar_trading` 默认关闭 → 两边行为一致。**启用前**必须补齐回测侧 intrabar 模拟（独立工作量 1-2 周）。

## 实盘 Sharpe 折算建议

基于上述物理差异，回测 Sharpe 到实盘期望的经验折算：

```
expected_live_sharpe = backtest_sharpe × 0.85 ~ 0.95
```

折算系数偏离 1.0 的主因：
- 非农时段成交价偏差（0.02-0.05）
- 保证金拒单（0.01-0.03）
- Tick vs bar 细节损耗（0.02-0.05）

**重要提示**：这不是"回测一定虚高"，只是"不确定性 buffer"。当回测 Sharpe > 1.5 时，实盘期望 1.3-1.4；当回测 Sharpe ≈ 1.0 时，实盘**可能 0.85 也可能 1.15**——此时不应依赖单一回测数字决策。

## 用于 nightly_wf 的自动折算（可选）

`NightlyWFReport` 可以在 `to_dict()` 中增加 `expected_live_sharpe_ceiling` 字段：
```python
expected_live_sharpe_ceiling = round(metric.sharpe * 0.85, 3)
```
——但不自动修改现有字段；只作为参考。

## 审计时间线

| 日期 | 事件 |
|---|---|
| 2026-04-16 | 首次系统性审计，发现 deployment gate / equity curve / PnL circuit 三项 P0-P1 分歧 |
| 2026-04-16 | Deployment gate 收口：回测 `_target_strategies` + `evaluate_strategies` + `execute_entry` 分别在 P0 级别对齐 |
| 2026-04-16 | Equity Curve Filter 接入回测 `portfolio.can_open_position` |
| 2026-04-16 | 核验 PnL 熔断实盘连败语义与回测等价，无需重写 |

## 结论

回测当前"可信度" ≈ 85-95%，足以支撑 6 周的 alpha 探索迭代。若发现某策略 `paper_vs_backtest` 分歧 > 20%，优先检查：
1. 策略 `deployment.status` 是否正确（是否被 CANDIDATE 意外屏蔽）
2. `equity_curve_filter_enabled` 是否与实盘一致
3. 是否启用了 intrabar 或 pending entry 等物理差异链路
