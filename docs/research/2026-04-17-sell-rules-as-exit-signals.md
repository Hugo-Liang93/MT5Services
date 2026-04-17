# 2026-04-17 Step 2.1 Sell Rules 的 Exit Timing 重解读

> 关联：[ADR-007 Research 与 Backtesting 的职责边界](../design/adr.md#adr-007)
> 场景：Step 2.1 M15/M30 基线挖掘 Top 10 规则全部是 sell 方向，但 XAUUSD 12 个月 +45.5% 强牛市——直接做空=爆仓。
> 本文档是 **ADR-007 协作契约的第一个实际案例**：Research 的 descriptive finding 不做 entry，改喂给 exit timing 规则。

## 核心判断

12 个月 XAUUSD 走势 **+45.5%**（145 上涨日 / 109 下跌日），挖掘却输出"sell 胜率 52-63%"。这不是算法错误，也不是市场特性误解——**这是强牛市中短期回调的统计规律**：

- 牛市中每周都有回调段
- Rule mining 的 time_barrier（20-120 bar）内 sell 触及 TP 的概率确实稳定偏高
- 但每次回调后趋势继续——**sell 作为独立 entry 的净期望是负的**

**规则的真实价值 = 多头持仓的 exit timing 信号**，而不是空头入场信号。

## Top 10 规则分类 + Exit 语义映射

### 类型 A：动量衰竭（Momentum Exhaustion）

| # | TF | 规则 | test 胜率 |
|---|----|------|---------|
| 1 | M15 | `macd.hist > -3.84 AND range_ratio <= 1.37 AND roc_accel <= 0.97` | **62.8%** / 4654 |
| 6 | M15 | `macd.hist > -3.84 AND 1.37 < range_ratio <= 4.84` | 56.5% / 1050 |

**在 buy 持仓中的语义**：
- `macd.hist > -3.84`：MACD 柱脱离深度空头（动量从负转暧昧）
- `range_ratio <= 1.37`：bar 范围收窄（非冲击性行情）
- `roc_accel <= 0.97`：ROC 二阶加速度走平或转负

→ **上涨动量正在衰减但尚未反转**

**推荐动作**：`Chandelier trail_atr` 从默认 2.5 降至 1.5（trail tighten），保护既得利润

### 类型 B：过度延伸（Parabolic Extension）

| # | TF | 规则 | test 胜率 |
|---|----|------|---------|
| 9 | M30 | `macd_fast.hist > -4.89 AND volume_ratio <= 1.95 AND macd_fast.hist > 6.21` | 53.7% / 203 |
| 10 | M30 | `macd.hist > 6.75` | 52.1% / 238 |

**在 buy 持仓中的语义**：
- MACD hist 深度正值（≥ 6.21 / 6.75）——多头动量"已经很充沛"
- 配合 volume_ratio <= 1.95（量能未再放大）——**走势加速阶段的末期**

→ **典型的 parabolic climax 预警**

**推荐动作**：部分止盈 40-50%，剩余仓位维持原 trail

### 类型 C：阻力位接触（Resistance Touch）

| # | TF | 规则 | test 胜率 |
|---|----|------|---------|
| 7 | M30 | `vwap_gap_atr > -4.35 AND dist_to_swing_high_atr > -0.12 AND body_ratio <= 0.39` | 52.1% / 898 |
| 8 | M30 | `vwap_gap_atr > -4.35 AND dist_to_swing_high_atr <= -0.12 AND roc12 > 0.41` | 54.9% / 173 |

**在 buy 持仓中的语义**：
- #7: 接近 swing high（距离 > -0.12）+ 小实体 bar → **到顶迹象**
- #8: 已突破 swing high 但突破质量可疑 → **假突破警告**

**推荐动作**：
- #7：部分止盈 30-50%（触及阻力）
- #8：trail 收紧到 swing_high 下方（防假突破回踩）

### 类型 D：支撑测试失败前兆（Support Failure Warning）

| # | TF | 规则 | test 胜率 |
|---|----|------|---------|
| 4 | M15 | `macd_fast.hist > -4.80 AND vwap_gap_atr > -4.52 AND dist_to_swing_low_atr <= 2.54` | 52.6% / 4958 |

**在 buy 持仓中的语义**：接近 swing low（距离 ≤ 2.54 ATR）+ 动量中性 → **测试支撑位，可能失败**

**推荐动作**：
- 若持仓尚在盈利 → trail 立即移至保本位
- 若持仓已小幅亏损 → 考虑主动平仓（不等 SL）

### 类型 E：震荡区顶部（Range-bound Top）

| # | TF | 规则 | test 胜率 |
|---|----|------|---------|
| 2 | M15 | `minus_di <= 44.36 AND macd.hist > -4.55 AND cci <= 312.91` | 55.2% / 6179 |
| 3 | M30 | `macd_fast.hist ∈ (-4.89, 6.21]` | 57.9% / 2692 |
| 5 | M15 | `macd_fast.hist > -4.80 AND vwap_gap_atr > -4.52 AND dist_to_swing_low_atr > 2.54` | 55.6% / 1391 |

**在 buy 持仓中的语义**：动量中性 + 既非超买也非深度空头 → **震荡区域特征**

**推荐动作**：配合 regime=RANGING 时，整体 trail 收紧（不适合继续持仓）

## 汇总：可转化为 Exit Rule 的候选（**需进一步验证再实施**）

| Exit Rule 名 | 来源规则 | 条件 | 动作 | 优先级 |
|-------------|---------|------|------|-------|
| `momentum_exhaustion_tighten` | #1, #6 | MACD hist 从 <-4 转到 >-3.84 AND range_ratio<=1.37 AND roc_accel<=1 | trail_atr × 0.6 | **高**（Top1 62.8% 胜率） |
| `parabolic_partial_tp` | #9, #10 | MACD hist > 6.5 | 部分止盈 40% | 中（样本小） |
| `swing_high_touch_tighten` | #7 | dist_to_swing_high_atr 近 0 AND body_ratio<0.4 | 部分止盈 30% | 中 |
| `support_test_breakeven` | #4 | dist_to_swing_low_atr<=2.54 AND 持仓盈利中 | trail 移至保本 | 中 |

## 边界与禁止事项

- ❌ **禁止**将上述任一规则作为**独立 sell 入场策略**——牛市中会系统性亏损
- ❌ **禁止**不经回测直接加入 Chandelier Exit——必须先验证净期望 > 现有规则
- ❌ **禁止**跨品种移植——本分析基于 XAUUSD 12 月 +45.5% 背景，其他品种/时期可能反向
- ✅ **允许**在单独的 `docs/research/*.md` 中扩展其他"sell-as-exit"场景（如熊市中 buy-as-exit）

## 下一步（非紧急）

1. 在 `backtest_runner` 中添加 `--compare-exit-rules` 开关，对比"现有 Chandelier" vs "Chandelier + momentum_exhaustion_tighten"
2. 若回测提升（PF +5% / MDD -10%），纳入 `config/exit.ini` 作为可选 exit profile
3. Paper Trading 验证 2 周后考虑全量启用

## ADR-007 协作契约的实证

本案例展示：Research 的 descriptive finding（sell rules with 52-63% hit rate）**不能**直接变成 Backtesting 的 entry 策略 spec，但可以变成 Chandelier Exit 的**扩展规则 spec**。这是 ADR-007 "Research 终点 = 结构完整的策略 spec" 的**非典型但重要**的应用：**目标载体不一定是新策略，也可以是现有策略的参数化扩展**。
