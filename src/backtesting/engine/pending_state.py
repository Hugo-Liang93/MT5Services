"""回测引擎 OCO Pending Group 数据结构（ADR-013 P4-residual）。

与 live 端 PendingEntryManager 的 `_pending` + `_groups` 反向索引等价：

- ``BacktestPendingGroup`` ↔ live 端 ``EntrySpecGroup``：持 N 个 member
  与统一 expiry_bar、cancellation_policy。
- 一个 member fill → 整 group 弹出（与 live ``_on_member_filled`` 任一成交
  撤 sibling 语义对齐；回测无 broker 反馈，直接整组移除）。
- 同 bar 多 member 都触发时 tie-break 由 ``EntryPolicyRegistry.fill_semantics_tie_break``
  指定（``limit_first`` / ``stop_first`` / ``alpha``）。

单 member 路径同样走此数据结构（``members`` 长度 1），保持单路径无兼容分支。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from src.signals.models import SignalDecision


@dataclass
class BacktestPendingMember:
    """OCO group 内的单个挂单成员。

    与 live 端 ``EntrySpecMember`` 等价：member_id / entry_type 决定触发语义；
    entry_low / entry_high 是 fill 判定区间。``status`` 字段在回测中用于
    "整组成员中只有一个 fill" 的状态机演进（其它成员标 cancelled）。
    """

    member_id: str
    entry_type: str  # "limit" / "stop" / "market"
    entry_low: float
    entry_high: float
    status: str = "active"  # active / filled / cancelled / expired


@dataclass
class BacktestPendingGroup:
    """OCO group 在回测中的运行时状态。

    ``decision`` 是触发该 group 的信号决策（用于 fill 时的 ``execute_entry``
    回放）；``expiry_bar`` 是统一过期 bar 索引（与 live ``compute_timeout``
    对齐，由 ``[pending_entry] expiry_bars`` 配置驱动）。
    """

    decision: SignalDecision
    members: List[BacktestPendingMember]
    expiry_bar: int
    cancellation_policy: str = "any_fill"
    status: str = "active"  # active / filled / expired

    def active_members(self) -> List[BacktestPendingMember]:
        return [m for m in self.members if m.status == "active"]


__all__ = [
    "BacktestPendingGroup",
    "BacktestPendingMember",
]
