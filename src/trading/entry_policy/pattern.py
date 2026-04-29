"""PatternType — 信号触发的形态类型枚举（结构化标识）。

策略在 evaluate() 时显式写入 metadata[MK.PATTERN_TYPE]，EntryPolicy 用枚举
match 而非字符串解析 why_reason。这是 ADR-013 的反补丁纪律核心：策略 → policy
之间的契约是结构化的、类型安全的，不依赖任何 reason 字符串字面量。

每个枚举值对应一类 K 线/结构信号，PullbackEntryPolicy 用它选回踩锚点（影线 50% /
prev bar extreme / body 50% 等），其他 policy 也可按需扩展分支表。

新增 pattern：
1. 在此追加枚举值（snake_case lower-case）
2. 在 PullbackEntryPolicy.BRANCH_TABLE / 配置 ini 加对应 offset
3. 信号策略在 _detect_pattern() 返回该枚举值
"""

from __future__ import annotations

from enum import Enum


class PatternType(str, Enum):
    """信号触发的形态类型。"""

    NONE = "none"

    # 反转/平衡形态
    PIN_BULL = "pin_bull"
    PIN_BEAR = "pin_bear"
    HAMMER = "hammer"
    SHOOTING_STAR = "shooting_star"

    # 吞没/动量形态
    ENGULFING_BULL = "engulfing_bull"
    ENGULFING_BEAR = "engulfing_bear"
    BIG_BAR_BULL = "big_bar_bull"
    BIG_BAR_BEAR = "big_bar_bear"

    # 连续形态
    THREE_SOLDIERS = "three_soldiers"
    THREE_CROWS = "three_crows"

    # 拒绝/扫荡形态
    REJECTION_BULL = "rejection_bull"
    REJECTION_BEAR = "rejection_bear"
    SWEEP_REVERSAL = "sweep_reversal"

    # 区间/趋势线/突破形态
    BREAKOUT_BULL = "breakout_bull"
    BREAKOUT_BEAR = "breakout_bear"
    TRENDLINE_TOUCH_BULL = "trendline_touch_bull"
    TRENDLINE_TOUCH_BEAR = "trendline_touch_bear"
