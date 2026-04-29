"""MarketEntryPolicy — 立即市价入场。

P1 阶段唯一实现：单成员 group，entry_type=market，trigger_price = current_close。
适用：默认 fallback / 低 TF scalp / mined_rule 等明确不需要回踩的场景。

参数：无（market policy 不读 params）。
"""

from __future__ import annotations

from typing import Any

from src.trading.entry_policy.intent import EntryIntent, MarketSnapshot
from src.trading.entry_policy.port import PolicyParams
from src.trading.entry_policy.specs import (
    EntrySpecGroup,
    EntrySpecMember,
    EntryType,
    new_group_id,
)


class MarketEntryPolicy:
    """市价入场（默认 / 兜底）。"""

    name = "market"

    def derive(
        self,
        intent: EntryIntent,
        market: MarketSnapshot,
        params: PolicyParams,  # noqa: ARG002 — 留 signature 一致
    ) -> EntrySpecGroup:
        trigger_price = market.current_close
        member = EntrySpecMember(
            member_id="market",
            entry_type=EntryType.MARKET,
            trigger_price=trigger_price,
            entry_low=trigger_price,
            entry_high=trigger_price,
            validity_bars=None,
        )
        return EntrySpecGroup(
            group_id=new_group_id(),
            members=(member,),
            cancellation_policy="any_fill",
            metadata={
                "policy_name": self.name,
                "branch": "market",
                "pattern_type": intent.pattern_type.value,
            },
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": "single_member_market",
            "params_schema": {},
            "description": "Immediate market entry at current close (fallback / low-friction).",
        }
