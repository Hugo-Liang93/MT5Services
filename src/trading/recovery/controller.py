from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from .entry_policies import RecoveryExitModel
from .models import (
    RecoveryCycleState,
    RecoveryDecision,
    RecoveryMarketSnapshot,
    RecoveryPolicy,
)


class BoundedRecoveryController:
    """Pure bounded recovery state machine.

    The controller has no MT5, database, or executor dependency. It receives
    already-owned state plus a bid/ask snapshot and returns an explicit decision.
    """

    def evaluate_next_step(
        self,
        policy: RecoveryPolicy,
        cycle: RecoveryCycleState,
        snapshot: RecoveryMarketSnapshot,
        *,
        now: datetime | None = None,
    ) -> RecoveryDecision:
        observed_at = now or snapshot.time
        if not policy.enabled:
            return self._decision("hold", "policy_disabled")
        if cycle.status != "open":
            return self._decision("hold", "cycle_not_open")
        if snapshot.symbol != cycle.symbol:
            return self._decision(
                "block",
                "symbol_mismatch",
                metadata={"expected": cycle.symbol, "actual": snapshot.symbol},
            )
        if not snapshot.has_bid_ask():
            return self._decision("block", "quote_side_missing")
        if cycle.step_count >= policy.max_steps:
            return self._decision(
                "block",
                "max_steps_reached",
                metadata={
                    "step_count": cycle.step_count,
                    "max_steps": policy.max_steps,
                },
            )

        elapsed_ms = self._elapsed_ms(cycle.last_step_at, observed_at)
        if elapsed_ms is not None and elapsed_ms < policy.min_step_interval_ms:
            return self._decision(
                "hold",
                "step_cooldown_active",
                metadata={
                    "elapsed_ms": elapsed_ms,
                    "min_step_interval_ms": policy.min_step_interval_ms,
                },
            )

        adverse_move_points = self._adverse_move_points(policy, cycle, snapshot)
        if adverse_move_points < policy.step_distance_points:
            return self._decision(
                "hold",
                "adverse_move_below_trigger",
                metadata={
                    "adverse_move_points": adverse_move_points,
                    "step_distance_points": policy.step_distance_points,
                },
            )
        max_step_adverse = float(policy.max_step_adverse_move_points)
        if max_step_adverse > 0 and adverse_move_points > max_step_adverse:
            return self._decision(
                "hold",
                "step_adverse_move_overextended",
                metadata={
                    "adverse_move_points": adverse_move_points,
                    "step_distance_points": policy.step_distance_points,
                    "max_step_adverse_move_points": max_step_adverse,
                },
            )

        step_index = cycle.step_count + 1
        next_volume = self._round_volume(
            cycle.base_volume * (policy.multiplier**step_index),
            policy.volume_step,
        )
        if policy.max_next_volume is not None and next_volume > policy.max_next_volume:
            return self._decision(
                "block",
                "max_next_volume_exceeded",
                step_index=step_index,
                volume=next_volume,
                metadata={
                    "next_volume": next_volume,
                    "max_next_volume": policy.max_next_volume,
                },
            )
        total_after = self._round_volume(
            cycle.total_volume + next_volume, policy.volume_step
        )
        if total_after > policy.max_total_volume:
            return self._decision(
                "block",
                "max_total_volume_exceeded",
                step_index=step_index,
                volume=next_volume,
                metadata={
                    "total_after": total_after,
                    "max_total_volume": policy.max_total_volume,
                },
            )

        entry_price = snapshot.entry_price(cycle.direction)
        return self._decision(
            "open_step",
            "adverse_move_triggered",
            step_index=step_index,
            volume=next_volume,
            entry_price=entry_price,
            metadata={
                "adverse_move_points": adverse_move_points,
                "total_after": total_after,
            },
        )

    def evaluate_cycle_exit(
        self,
        policy: RecoveryPolicy,
        cycle: RecoveryCycleState,
        snapshot: RecoveryMarketSnapshot,
    ) -> RecoveryDecision:
        return RecoveryExitModel().evaluate_exit(policy, cycle, snapshot)

    def apply_open_step(
        self,
        cycle: RecoveryCycleState,
        decision: RecoveryDecision,
        *,
        now: datetime,
    ) -> RecoveryCycleState:
        if decision.action != "open_step":
            raise ValueError("decision must be open_step")
        if (
            decision.volume is None
            or decision.entry_price is None
            or decision.step_index is None
        ):
            raise ValueError(
                "open_step decision requires volume, entry_price, and step_index"
            )
        new_total = cycle.total_volume + decision.volume
        new_average = (
            (cycle.average_entry_price * cycle.total_volume)
            + (decision.entry_price * decision.volume)
        ) / new_total
        return replace(
            cycle,
            total_volume=round(new_total, 8),
            step_count=decision.step_index,
            average_entry_price=round(new_average, 10),
            last_entry_price=decision.entry_price,
            last_step_at=now,
            updated_at=now,
        )

    @staticmethod
    def estimate_pnl(
        policy: RecoveryPolicy,
        cycle: RecoveryCycleState,
        *,
        exit_price: float,
    ) -> float:
        if cycle.direction == "buy":
            raw = (exit_price - cycle.average_entry_price) * cycle.total_volume
        else:
            raw = (cycle.average_entry_price - exit_price) * cycle.total_volume
        return round(raw * policy.contract_size, 10)

    @staticmethod
    def _adverse_move_points(
        policy: RecoveryPolicy,
        cycle: RecoveryCycleState,
        snapshot: RecoveryMarketSnapshot,
    ) -> float:
        exit_price = snapshot.exit_price(cycle.direction)
        assert exit_price is not None
        if cycle.direction == "buy":
            adverse = max(0.0, cycle.last_entry_price - exit_price)
        else:
            adverse = max(0.0, exit_price - cycle.last_entry_price)
        return round(adverse / policy.point, 10)

    @staticmethod
    def _elapsed_ms(previous: datetime | None, now: datetime) -> int | None:
        if previous is None:
            return None
        return max(0, int((now - previous).total_seconds() * 1000))

    @staticmethod
    def _round_volume(value: float, step: float) -> float:
        if step <= 0:
            return round(value, 8)
        return round(round(value / step) * step, 8)

    @staticmethod
    def _decision(
        action: str,
        reason: str,
        *,
        step_index: int | None = None,
        volume: float | None = None,
        entry_price: float | None = None,
        exit_price: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RecoveryDecision:
        return RecoveryDecision(
            action=action,
            reason=reason,
            step_index=step_index,
            volume=volume,
            entry_price=entry_price,
            exit_price=exit_price,
            metadata=dict(metadata or {}),
        )
