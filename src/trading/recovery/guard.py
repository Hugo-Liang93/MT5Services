from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import PositionScalingIntent, RecoveryCycleState, RecoveryPolicy


@dataclass(frozen=True)
class RecoveryPreTradeDecision:
    allowed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


class RecoveryPreTradeGuard:
    """Execution-boundary hard caps for recovery scaling intents."""

    def assess(
        self,
        policy: RecoveryPolicy,
        cycle: RecoveryCycleState,
        intent: PositionScalingIntent,
    ) -> RecoveryPreTradeDecision:
        details = {
            "cycle_id": cycle.cycle_id,
            "intent_cycle_id": intent.cycle_id,
            "account_key": cycle.account_key,
            "intent_account_key": intent.account_key,
            "cycle_status": cycle.status,
            "current_step_count": cycle.step_count,
            "actual_step_index": intent.step_index,
            "intent_volume": intent.volume,
            "current_total_volume": cycle.total_volume,
            "projected_total_volume": cycle.total_volume + intent.volume,
            "max_steps": policy.max_steps,
            "max_next_volume": policy.max_next_volume,
            "max_total_volume": policy.max_total_volume,
        }
        if not policy.enabled:
            return RecoveryPreTradeDecision(False, "policy_disabled", details)
        if cycle.account_key != intent.account_key:
            return RecoveryPreTradeDecision(False, "account_key_mismatch", details)
        if cycle.cycle_id != intent.cycle_id:
            return RecoveryPreTradeDecision(False, "cycle_id_mismatch", details)
        if cycle.status != "open":
            return RecoveryPreTradeDecision(False, "cycle_not_open", details)
        if cycle.symbol != intent.symbol:
            return RecoveryPreTradeDecision(False, "symbol_mismatch", details)
        if cycle.direction != intent.direction:
            return RecoveryPreTradeDecision(False, "direction_mismatch", details)

        expected_step_index = cycle.step_count + 1
        details["expected_step_index"] = expected_step_index
        if intent.step_index != expected_step_index:
            return RecoveryPreTradeDecision(False, "step_index_mismatch", details)
        if intent.step_index > policy.max_steps:
            return RecoveryPreTradeDecision(False, "max_steps_exceeded", details)
        if policy.max_next_volume is not None and intent.volume > policy.max_next_volume:
            return RecoveryPreTradeDecision(False, "max_next_volume_exceeded", details)
        if details["projected_total_volume"] > policy.max_total_volume:
            return RecoveryPreTradeDecision(False, "max_total_volume_exceeded", details)
        return RecoveryPreTradeDecision(True, "allowed", details)
