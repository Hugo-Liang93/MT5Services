"""OcoEntryPolicy — A+B 组合（限价回踩 + 突破止损二选一）。

核心场景：price_action / pullback_window 这类"既怕追高又怕错过单边"的策略，
两手准备：
  - LIMIT pullback 成员：等回踩到形态影线/前 bar 极端成交（最优 R:R）
  - STOP breakout 成员：等创新高/低突破成交（不漏单边）
任一成交 → PendingEntryManager 撤其他 sibling（OCO any_fill 语义）。

实现：内部组合调用 PullbackEntryPolicy + BreakoutEntryPolicy 各产 1 个 member，
合并到同一 EntrySpecGroup。每个 sub-policy 的参数从 policy_params.pullback /
policy_params.breakout 读，OCO policy 自身无独立参数。

参数（policy_params.oco_pullback_breakout）：
  - cancellation_policy: any_fill / all_or_none（默认 any_fill）

注意：sub-policy 实例由 OcoEntryPolicy 在构造时持有；factory 装配时需注入。
为简化 P2，sub-policy 直接 new（不依赖 registry）——它们都是无状态的。
"""

from __future__ import annotations

from typing import Any

from src.trading.entry_policy.intent import EntryIntent, MarketSnapshot
from src.trading.entry_policy.policies.breakout import BreakoutEntryPolicy
from src.trading.entry_policy.policies.pullback import PullbackEntryPolicy
from src.trading.entry_policy.port import PolicyParams
from src.trading.entry_policy.specs import (
    CancellationPolicy,
    EntrySpecGroup,
    EntrySpecMember,
    new_group_id,
)


def _resolve_cancellation_policy(params: PolicyParams) -> CancellationPolicy:
    raw = str(params.get("cancellation_policy", "any_fill")).strip().lower()
    if raw == "all_or_none":
        return "all_or_none"
    return "any_fill"


class OcoEntryPolicy:
    """A+B 组合（OCO any-fill）入场策略。

    持有 PullbackEntryPolicy / BreakoutEntryPolicy 实例（无状态，可共享）。
    每次 derive 时分别拿子 policy 的 sub_params（由 registry 注入）来计算 sub group，
    再合并到同一 group_id。
    """

    name = "oco_pullback_breakout"

    def __init__(
        self,
        pullback_policy: PullbackEntryPolicy | None = None,
        breakout_policy: BreakoutEntryPolicy | None = None,
    ) -> None:
        self._pullback = pullback_policy or PullbackEntryPolicy()
        self._breakout = breakout_policy or BreakoutEntryPolicy()

    def derive(
        self,
        intent: EntryIntent,
        market: MarketSnapshot,
        params: PolicyParams,
    ) -> EntrySpecGroup:
        cancellation = _resolve_cancellation_policy(params)

        # OCO 子参数从扁平 params 中按前缀分离
        # 约定：pullback_<key> / breakout_<key>
        # 没有前缀的键被 sub-policy 默认值覆盖（兼容只想覆写 OCO 顶层的场景）
        pullback_sub = _filter_prefix(params, "pullback_")
        breakout_sub = _filter_prefix(params, "breakout_")

        pullback_group = self._pullback.derive(intent, market, pullback_sub)
        breakout_group = self._breakout.derive(intent, market, breakout_sub)

        # 抽取各 sub group 的单 member（PullbackEntryPolicy / BreakoutEntryPolicy
        # 当前都返回 1 member 的 group）
        if len(pullback_group.members) != 1 or len(breakout_group.members) != 1:
            raise RuntimeError(
                "OcoEntryPolicy expects sub-policies to return single-member groups; "
                f"got pullback={len(pullback_group.members)} "
                f"breakout={len(breakout_group.members)}"
            )

        merged_members: tuple[EntrySpecMember, ...] = (
            pullback_group.members[0],
            breakout_group.members[0],
        )

        return EntrySpecGroup(
            group_id=new_group_id(),
            members=merged_members,
            cancellation_policy=cancellation,
            metadata={
                "policy_name": self.name,
                "branch": "oco_a_plus_b",
                "pattern_type": intent.pattern_type.value,
                "pullback_meta": dict(pullback_group.metadata),
                "breakout_meta": dict(breakout_group.metadata),
            },
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": "oco_two_member",
            "params_schema": {
                "cancellation_policy": "any_fill | all_or_none (default any_fill)",
                "pullback_<key>": "PullbackEntryPolicy params with prefix",
                "breakout_<key>": "BreakoutEntryPolicy params with prefix",
            },
            "description": (
                "OCO group: A. LIMIT pullback (pattern-aware) + B. STOP breakout, "
                "any fill cancels sibling."
            ),
            "sub_policies": [
                self._pullback.describe(),
                self._breakout.describe(),
            ],
        }


def _filter_prefix(params: PolicyParams, prefix: str) -> dict[str, Any]:
    """提取键以 prefix 开头的子参数，剥掉 prefix。

    例：{"pullback_zone_atr": 0.1, "breakout_buffer_atr": 0.05}
        → prefix="pullback_" 得 {"zone_atr": 0.1}
    """
    result: dict[str, Any] = {}
    for key, value in params.items():
        if not key.startswith(prefix):
            continue
        sub_key = key[len(prefix) :]
        if not sub_key:
            continue
        result[sub_key] = value
    return result
