# R-Based 分阶段仓位管理设计

> **📐 状态：规划方案（未落地到代码）**
>
> 本文件是**出场机制的未来方案**——将 breakeven + trailing_SL + trailing_TP 三套独立机制
> 统一为 R 尺度的分阶段系统。**尚未实现**，代码中仍是三套独立机制。
>
> **当前实际出场逻辑权威引用**：
> - 执行代码：[`src/trading/positions/exit_rules.py`](../../src/trading/positions/exit_rules.py)
> - 已实现的出场一致性：[`position-state-consistency.md`](position-state-consistency.md)
> - 研究层 Triple-Barrier：[`../research-system.md`](../research-system.md) §F-12a/b
>
> 落地前路计划：见 [`next-plan.md`](next-plan.md)（若有）或 TODO 中 P3+ 阶段。

## 问题

当前四套出场机制独立运作，各自用不同 ATR 参数：

| 机制 | 参数 | 问题 |
|------|------|------|
| 固定 SL | N×ATR | 亏损固定为 N×ATR |
| Breakeven | 0.8×ATR 触发 | 浮盈仅 0.27R 就保本，太早 |
| Trailing SL | 1.0×ATR 跟踪距离 | 与 trailing TP 竞争 |
| Trailing TP | 1.2×ATR 激活，0.6×ATR 回撤 | 截断盈利在 ~0.4R |

结果：**亏损固定（1R），盈利被截断（~0.4R），盈亏比 W/L ≈ 0.40，无论怎么调参都无法盈利。**

## 设计

### 核心概念

**R = 初始风险 = |entry_price - initial_stop_loss|**

所有出场决策用 R 表达，保证盈亏比有结构性下限。

### 分阶段出场

```
Phase 0 (风险期)     0 ~ 1R       SL = 初始位置          最差: -1R
Phase 1 (保本期)     ≥ 1R         SL = entry + 0.1R      最差: +0.1R
Phase 2 (锁利期)     ≥ 1.5R       SL = entry + 0.5R      最差: +0.5R
Phase 3 (跟踪期)     ≥ 2R         SL = peak - trail_r    最差: +1R
Phase 4 (收紧期)     ≥ 3R         SL = peak - tight_r    最差: +2.25R
硬上界 TP            max_tp_r      强制平仓               固定天花板
```

**Phase 只升不降**——一旦进入 Phase 2，即使价格回撤到 1R 利润，SL 仍保持在 0.5R 锁利位。

### SL 只上不下

每次 Phase 检查计算的 new_sl 必须 ≥ current_sl（多头）或 ≤ current_sl（空头）。
这保证了已锁定的利润不会因为 Phase 回退而丢失。

### 替代关系

| 旧机制 | 新机制 |
|--------|--------|
| `check_breakeven()` | Phase 1（但阈值从 0.27R 提升到 1R） |
| `check_trailing_stop()` | Phase 3/4 的 trailing 逻辑 |
| `check_trailing_take_profit()` | **移除**。由 Phase 3/4 的 SL 上移替代 |
| 固定 TP | 保留为 `max_tp_r` 硬上界 |

### 策略类别默认参数

| Category | breakeven_r | lock_r | trail_activation_r | trail_distance_r | extended_r | extended_trail_r |
|----------|:-----------:|:------:|:------------------:|:----------------:|:----------:|:----------------:|
| trend | 1.0 | 1.5 | 2.0 | 1.0 | 3.0 | 0.75 |
| reversion | 0.8 | 1.2 | 1.5 | 0.75 | 2.5 | 0.5 |
| breakout | 1.2 | 1.8 | 2.5 | 1.5 | 4.0 | 1.0 |

### Regime 缩放

Phase 3/4 的 trail 距离按 regime 缩放：

| Regime | trail_distance 缩放 | 原因 |
|--------|:------------------:|------|
| trending | ×1.25 | 趋势中给更多空间 |
| ranging | ×0.75 | 震荡市早锁利 |
| breakout | ×1.50 | 突破行情空间大 |
| uncertain | ×1.00 | 默认 |

### 时间衰减（可选）

Phase 3 中连续 N 根 bar 无新高 → 自动进入 Phase 4（收紧）。
默认 N = 该 TF 的 bar 数（如 H1 = 10 bars = 10 小时无进展就收紧）。

## 配置

```ini
[position_management]
# R-Based 分阶段出场（替代 breakeven + trailing_stop + trailing_tp）
enabled = true

# Phase 阈值（R 倍数）
breakeven_r = 1.0
lock_profit_r = 1.5
lock_profit_amount_r = 0.5
trail_activation_r = 2.0
trail_distance_r = 1.0
extended_move_r = 3.0
extended_trail_r = 0.75
max_tp_r = 5.0

# 时间衰减（0 = 禁用）
stale_bars_to_tighten = 0

# Per-category 覆盖
[position_management.reversion]
breakeven_r = 0.8
lock_profit_r = 1.2
trail_activation_r = 1.5
trail_distance_r = 0.75

[position_management.breakout]
breakeven_r = 1.2
lock_profit_r = 1.8
trail_activation_r = 2.5
trail_distance_r = 1.5
```

## 动态感知层（叠加在 R-Based Phase 之上）

R-Based Phase 是纯机械的价格出场，**不感知策略状态和市场变化**。动态感知层补充这个能力。

核心原则：**动态层只能加速平仓或收紧保护，不能放松 R-Based 已锁定的利润。**

### 1. 策略信号反转退出

每根 confirmed bar，检查开仓策略的最新信号方向：

```
position.strategy = "macd_momentum"
position.direction = "buy"

当前 bar confirmed 评估：macd_momentum.evaluate() → direction = "sell"
→ 策略反转 → close_reason = "signal_reversal" → 立即平仓
```

**只看开仓策略自身的信号**，不看其他策略。只有 confirmed scope 的信号触发反转检查（intrabar 可能是噪声）。

### 2. ATR 自适应 SL 收紧

持仓期间 ATR 可能变化。SL 可以跟随 ATR 收缩而**收紧**，但不能因 ATR 扩大而放松：

```
开仓时 ATR=10, SL=entry-20 (2×ATR), R=20

后续 bar ATR=7（波动收缩）:
  adaptive_sl = entry - 2×7 = entry-14
  adaptive_sl 比当前 SL 更紧 → 可以收紧
  但不能突破 R-Based Phase 已锁定的 SL 位置

后续 bar ATR=15（波动扩大）:
  adaptive_sl = entry - 2×15 = entry-30
  比当前 SL 更宽 → 忽略（不放松）
```

规则：`new_sl = max(r_based_sl, atr_adaptive_sl, current_sl)`（多头）

### 3. Regime 实时缩放（已在 R-Based 设计中）

`regime_trail_scale` 参数在每次 `check_r_based_exit` 调用时传入当前 regime 的缩放值。不需要固定在开仓时——Phase 3/4 的 trail 距离会实时响应 regime 变化。

### 三层交互

```
每根 bar 的出场检查顺序：

1. 更新 peak_price + bars_since_peak
2. check_r_based_exit()          → 机械出场（SL/TP/Phase trailing）
   if should_close → 平仓，结束
3. check_signal_reversal()       → 策略信号是否反转
   if reversed → 平仓 (reason="signal_reversal")，结束
4. check_atr_adaptive_tighten()  → ATR 收缩时收紧 SL
   if new_sl tighter → 更新 SL（仅收紧方向）
```

### 回测中的策略反转检查

回测引擎在 `check_exits` 之后、`evaluate_strategies` 之后可以交叉检查：
- 拿到当前 bar 的策略评估结果
- 对每个持仓，检查其 `strategy` 在当前 bar 的 direction
- 如果方向反转 → 平仓

这不需要额外的数据或计算——评估结果已经在主循环中产出了。

## 实现

### 纯函数接口

```python
@dataclass(frozen=True)
class RBasedPhaseConfig:
    breakeven_r: float = 1.0
    lock_profit_r: float = 1.5
    lock_profit_amount_r: float = 0.5
    trail_activation_r: float = 2.0
    trail_distance_r: float = 1.0
    extended_move_r: float = 3.0
    extended_trail_r: float = 0.75
    max_tp_r: float = 5.0
    stale_bars_to_tighten: int = 0

@dataclass(frozen=True)
class PhaseResult:
    phase: int                    # 0-4
    new_stop_loss: float | None   # None = 不变
    should_close: bool            # True = 触及 SL 或 TP
    close_reason: str             # "stop_loss" / "take_profit_r" / ""
    current_r_multiple: float     # 当前浮盈的 R 倍数

def check_r_based_exit(
    action: str,               # "buy" / "sell"
    entry_price: float,
    current_price: float,      # bar.close（用于 Phase 判断）
    current_high: float,       # bar.high（用于 SL 触发检查）
    current_low: float,        # bar.low（用于 SL 触发检查）
    current_stop_loss: float,
    initial_risk: float,       # R = |entry - initial_sl|
    peak_price: float,         # 持仓期间最高/最低价
    bars_since_peak: int,      # 距上次新高的 bar 数
    config: RBasedPhaseConfig,
    regime_trail_scale: float = 1.0,  # Regime 缩放
) -> PhaseResult:
    """统一的 R-Based 出场检查。替代 breakeven + trailing_stop + trailing_tp。"""
```

### 调用位置

**回测**：`src/backtesting/engine/portfolio.py` 的 `check_exits()` 中，
用 `check_r_based_exit()` 替换 `check_breakeven()` + `check_trailing_stop()` + `check_trailing_take_profit()`。

**实盘**：`src/trading/positions/manager.py` 的持仓监控循环中，同样替换。

两者共用 `src/trading/positions/rules.py` 中的纯函数。

### Position 状态扩展

需要在持仓对象上新增：
- `initial_risk: float` — R 值（|entry - initial_sl|，开仓时计算，不变）
- `peak_price: float` — 持仓期最高价（多头）/最低价（空头），已有
- `bars_since_peak: int` — 距上次新高/新低的 bar 数
- `current_phase: int` — 当前 Phase（0-4），用于日志/监控

### 向后兼容

新增 `[position_management] enabled = true` 开关：
- `true`：使用 R-Based Phase 系统
- `false`：回退到旧的 breakeven + trailing_stop + trailing_tp（默认值，不破坏现有行为）

## 验证计划

1. 实现纯函数 + 单元测试（覆盖每个 Phase 转换边界）
2. 回测对比：H1 旧体系 vs R-Based（相同策略和信号）
3. 预期改善：W/L 从 ~0.40 提升到 ≥0.80，Sharpe 转正
