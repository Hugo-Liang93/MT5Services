"""EntrySpecGroup / EntrySpecMember — EntryPolicy 输出契约。

替代旧的 EntrySpec 单值对象。设计目标：

1. **多成员组（OCO 原生）**：一个 group 可以包含 N 个 member（LIMIT + STOP + ...），
   PendingEntryManager 按 group_id 维护反向索引，any-fill 时撤其他 sibling。
2. **trigger_price 由 policy 算好**：member 直接给出最终触发价 + entry_low/high
   监控区间，pending_orders.py 不再做 ±zone×ATR 的二次计算。
3. **不可变值对象**：frozen dataclass，不持有任何运行时引用（ADR-013 边界）。

EntryType 枚举仍复用 signals.strategies.structured.base 的定义，P2 删除策略 _entry_spec
时一并搬迁到本模块。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping, Tuple


class EntryType(str, Enum):
    """入场方式枚举（ADR-013 owner = trading.entry_policy）。"""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


CancellationPolicy = Literal["any_fill", "all_or_none"]
GroupRole = Literal["limit", "stop", "market"]


@dataclass(frozen=True)
class EntrySpecMember:
    """OCO 组内单个挂单成员。

    member_id 在组内唯一，用于审计/trace（如 "limit_pullback" / "stop_breakout"）。
    trigger_price 是 policy 算好的最终触发价，pending_orders 直接下单不做二次平移。
    entry_low/high 是 PendingEntryManager 价格监控区间（fill 判定用）。
    validity_bars=None 时使用全局 timeout 配置。
    """

    member_id: str
    entry_type: EntryType
    trigger_price: float
    entry_low: float
    entry_high: float
    validity_bars: int | None = None

    @property
    def group_role(self) -> GroupRole:
        """从 entry_type 派生 group_role（持久化与 trace 用）。"""
        # EntryType.value 已被声明为 str；GroupRole 是 Literal["limit","stop","market"]
        # mypy 不能推断 enum value 的字面量类型，但运行时三个值与 GroupRole 一致。
        value = self.entry_type.value
        if value not in ("limit", "stop", "market"):
            raise ValueError(f"unsupported entry_type for group_role: {value!r}")
        return value  # type: ignore[return-value]


@dataclass(frozen=True)
class EntrySpecGroup:
    """EntryPolicy 输出的多成员组（含单 member 退化场景）。

    cancellation_policy:
      - any_fill: 任一成员成交即撤其他（OCO 默认语义）
      - all_or_none: 必须全部成交，单成员失败整组撤（暂不实现，预留扩展点）

    metadata 字段供 trace / 审计用，记录 policy_name / branch /
    pattern_type / fingerprint 等决策依据，不参与运行时分派。
    """

    group_id: str
    members: Tuple[EntrySpecMember, ...]
    cancellation_policy: CancellationPolicy = "any_fill"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.members:
            raise ValueError("EntrySpecGroup.members must contain at least 1 member")
        member_ids = [m.member_id for m in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError(
                f"EntrySpecGroup.members has duplicate member_id: {member_ids}"
            )

    @property
    def is_single_member(self) -> bool:
        return len(self.members) == 1

    @property
    def is_oco(self) -> bool:
        return len(self.members) > 1

    def all_market(self) -> bool:
        """全部成员都是 MARKET 类型——pre_trade_checks 用作 require_pending_entry 判定。"""
        return all(m.entry_type == EntryType.MARKET for m in self.members)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写 trace 的字典。"""
        return {
            "group_id": self.group_id,
            "cancellation_policy": self.cancellation_policy,
            "metadata": dict(self.metadata),
            "members": [
                {
                    "member_id": m.member_id,
                    "entry_type": m.entry_type.value,
                    "trigger_price": m.trigger_price,
                    "entry_low": m.entry_low,
                    "entry_high": m.entry_high,
                    "validity_bars": m.validity_bars,
                }
                for m in self.members
            ],
        }


def new_group_id() -> str:
    """生成 OCO group 唯一标识。multi-account 场景下与 account_key 组成复合 PK。"""
    return uuid.uuid4().hex


__all__ = [
    "CancellationPolicy",
    "EntrySpecGroup",
    "EntrySpecMember",
    "EntryType",
    "GroupRole",
    "new_group_id",
]
