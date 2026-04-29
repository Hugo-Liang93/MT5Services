"""EntryPolicy 模块 — 可插拔入场决策端口（ADR-013）。

公开 API：
  - EntryPolicy / PolicyParams（端口）
  - EntrySpecGroup / EntrySpecMember / EntryType / new_group_id（输出契约）
  - EntryIntent / MarketSnapshot / BarSnapshot / Direction（输入契约）
  - PatternType（结构化形态枚举）
  - EntryPolicyRegistry / EntryPolicyNotFoundError（解析中枢）
  - MarketEntryPolicy（P1 内置实现；P2 加 Pullback/Breakout/Oco/FibPullback）

边界（参 ADR-013）：
  - Policy 是纯函数式，不持有运行时状态
  - 不允许从 Policy 反向访问 SignalRuntime / PendingEntryManager / 策略实例
  - 输入/输出全为 frozen dataclass
"""

from __future__ import annotations

from src.trading.entry_policy.intent import (
    BarSnapshot,
    Direction,
    EntryIntent,
    MarketSnapshot,
)
from src.trading.entry_policy.pattern import PatternType
from src.trading.entry_policy.policies import (
    BreakoutEntryPolicy,
    FibPullbackEntryPolicy,
    MarketEntryPolicy,
    OcoEntryPolicy,
    PullbackEntryPolicy,
)
from src.trading.entry_policy.port import EntryPolicy, PolicyParams
from src.trading.entry_policy.registry import (
    EntryPolicyNotFoundError,
    EntryPolicyRegistry,
)
from src.trading.entry_policy.specs import (
    CancellationPolicy,
    EntrySpecGroup,
    EntrySpecMember,
    EntryType,
    GroupRole,
    new_group_id,
)

__all__ = [
    "BarSnapshot",
    "BreakoutEntryPolicy",
    "CancellationPolicy",
    "Direction",
    "EntryIntent",
    "EntryPolicy",
    "EntryPolicyNotFoundError",
    "EntryPolicyRegistry",
    "EntrySpecGroup",
    "EntrySpecMember",
    "EntryType",
    "FibPullbackEntryPolicy",
    "GroupRole",
    "MarketEntryPolicy",
    "MarketSnapshot",
    "OcoEntryPolicy",
    "PatternType",
    "PolicyParams",
    "PullbackEntryPolicy",
    "new_group_id",
]
