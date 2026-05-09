from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.market.tick_features.models import TickFeatureSnapshot

from .models import RecoveryCycleState, RecoveryDecision, RecoveryMarketSnapshot, RecoveryPolicy


@dataclass(frozen=True)
class RecoveryDirectionDecision:
    direction: str | None
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryCostDecision:
    allowed: bool
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryEntryConfirmationDecision:
    allowed: bool
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


class RecoveryEntrySignalConfirmer:
    """Require a direction signal to persist across adjacent tick snapshots."""

    def __init__(self) -> None:
        self._direction: str | None = None
        self._count = 0
        self._last_observed_at: datetime | None = None

    def reset(self) -> None:
        self._direction = None
        self._count = 0
        self._last_observed_at = None

    def status(self) -> dict[str, Any]:
        return {
            "direction": self._direction,
            "confirmation_count": int(self._count),
            "last_observed_at": (
                self._last_observed_at.isoformat()
                if self._last_observed_at is not None
                else None
            ),
        }

    def assess(
        self,
        *,
        direction: str,
        observed_at: datetime,
        required_snapshots: int,
        max_gap_seconds: float,
    ) -> RecoveryEntryConfirmationDecision:
        normalized = _normalize_direction(direction)
        required = max(1, int(required_snapshots))
        max_gap = max(0.0, float(max_gap_seconds))
        metadata = {
            "direction": normalized,
            "required_snapshots": required,
            "max_gap_seconds": max_gap,
            "previous_direction": self._direction,
            "previous_count": self._count,
        }
        if normalized is None:
            self.reset()
            metadata["confirmation_count"] = 0
            return RecoveryEntryConfirmationDecision(
                False,
                "entry_confirmation_direction_invalid",
                metadata,
            )
        if required <= 1:
            self._direction = normalized
            self._count = 1
            self._last_observed_at = observed_at
            metadata["confirmation_count"] = 1
            return RecoveryEntryConfirmationDecision(
                True,
                "entry_confirmation_not_required",
                metadata,
            )

        gap_seconds = _elapsed_seconds(self._last_observed_at, observed_at)
        same_direction = self._direction == normalized
        gap_ok = gap_seconds is None or max_gap <= 0 or gap_seconds <= max_gap
        metadata["gap_seconds"] = gap_seconds
        metadata["gap_ok"] = gap_ok

        if same_direction and gap_ok:
            self._count += 1
        else:
            self._direction = normalized
            self._count = 1
        self._last_observed_at = observed_at
        metadata["confirmation_count"] = self._count

        if self._count >= required:
            return RecoveryEntryConfirmationDecision(
                True,
                "entry_confirmation_confirmed",
                metadata,
            )
        return RecoveryEntryConfirmationDecision(
            False,
            "entry_confirmation_pending",
            metadata,
        )


class RecoveryDirectionPolicy:
    """Choose an initial recovery direction from a public tick feature snapshot."""

    def choose_direction(
        self,
        policy: RecoveryPolicy,
        snapshot: TickFeatureSnapshot,
        *,
        fallback_direction: str,
    ) -> RecoveryDirectionDecision:
        fallback = _normalize_direction(fallback_direction)
        mode = str(policy.direction_mode or "fixed").strip().lower()
        metadata = {
            "direction_mode": mode,
            "fallback_direction": fallback,
            "price_change_points": snapshot.price_change_points,
            "buy_pressure": snapshot.buy_pressure,
            "sell_pressure": snapshot.sell_pressure,
            "min_directional_move_points": float(policy.min_directional_move_points),
            "max_directional_move_points": float(policy.max_directional_move_points),
            "min_pressure_delta": float(policy.min_pressure_delta),
        }
        if fallback is None:
            return RecoveryDirectionDecision(None, "fallback_direction_invalid", metadata)
        if mode == "fixed":
            return RecoveryDirectionDecision(fallback, "fixed_direction", metadata)

        price_change = snapshot.price_change_points
        if price_change is None:
            return RecoveryDirectionDecision(None, "direction_signal_missing", metadata)
        if abs(float(price_change)) < float(policy.min_directional_move_points):
            return RecoveryDirectionDecision(None, "direction_signal_too_weak", metadata)
        max_move = float(policy.max_directional_move_points)
        if max_move > 0 and abs(float(price_change)) > max_move:
            return RecoveryDirectionDecision(
                None,
                "direction_signal_overextended",
                metadata,
            )

        pressure_delta = _pressure_delta(snapshot)
        metadata["pressure_delta"] = pressure_delta
        min_pressure_delta = float(policy.min_pressure_delta)
        if min_pressure_delta > 0 and pressure_delta is None:
            return RecoveryDirectionDecision(None, "direction_pressure_missing", metadata)

        if float(price_change) > 0:
            if pressure_delta is not None and pressure_delta < min_pressure_delta:
                return RecoveryDirectionDecision(None, "direction_pressure_mismatch", metadata)
            return RecoveryDirectionDecision("buy", "auto_direction_buy", metadata)

        if pressure_delta is not None and pressure_delta > -min_pressure_delta:
            return RecoveryDirectionDecision(None, "direction_pressure_mismatch", metadata)
        return RecoveryDirectionDecision("sell", "auto_direction_sell", metadata)


class RecoveryCostGate:
    """Gate initial recovery entries against spread and explicit cost budget."""

    def assess_entry(
        self,
        policy: RecoveryPolicy,
        *,
        direction: str,
        snapshot: TickFeatureSnapshot,
    ) -> RecoveryCostDecision:
        return self._assess_market_entry(
            policy,
            direction=direction,
            snapshot=snapshot,
            scope="recovery_initial",
            ok_reason="entry_cost_ok",
            spread_reason="spread_too_wide",
            target_reason="net_target_below_cost",
        )

    def assess_step(
        self,
        policy: RecoveryPolicy,
        *,
        cycle: RecoveryCycleState,
        decision: RecoveryDecision,
        snapshot: TickFeatureSnapshot,
    ) -> RecoveryCostDecision:
        metadata = {
            "cycle_id": cycle.cycle_id,
            "current_step_count": int(cycle.step_count),
            "step_index": decision.step_index,
            "decision_reason": decision.reason,
            "decision_volume": decision.volume,
        }
        if decision.action != "open_step":
            return RecoveryCostDecision(
                False,
                "step_decision_invalid",
                {
                    "scope": "recovery_step",
                    "action": decision.action,
                    **metadata,
                },
            )
        return self._assess_market_entry(
            policy,
            direction=cycle.direction,
            snapshot=snapshot,
            scope="recovery_step",
            ok_reason="step_cost_ok",
            spread_reason="step_spread_too_wide",
            target_reason="step_net_target_below_cost",
            metadata=metadata,
        )

    @staticmethod
    def _assess_market_entry(
        policy: RecoveryPolicy,
        *,
        direction: str,
        snapshot: TickFeatureSnapshot,
        scope: str,
        ok_reason: str,
        spread_reason: str,
        target_reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> RecoveryCostDecision:
        normalized_direction = _normalize_direction(direction)
        spread_points = _spread_points(policy, snapshot)
        payload = {
            "scope": scope,
            "direction": normalized_direction,
            "spread_points": spread_points,
            "max_entry_spread_points": policy.max_entry_spread_points,
            "recovery_target_points": float(policy.recovery_target_points),
            "slippage_budget_points": float(policy.slippage_budget_points),
            "commission_points": float(policy.commission_points),
            "min_net_profit_points": float(policy.min_net_profit_points),
            **dict(metadata or {}),
        }
        if normalized_direction is None:
            return RecoveryCostDecision(False, "direction_invalid", payload)
        if spread_points is None:
            return RecoveryCostDecision(False, "spread_unavailable", payload)

        required_points = (
            spread_points
            + float(policy.slippage_budget_points)
            + float(policy.commission_points)
            + float(policy.min_net_profit_points)
        )
        expected_net_points = float(policy.recovery_target_points) - (
            spread_points
            + float(policy.slippage_budget_points)
            + float(policy.commission_points)
        )
        net_margin_points = float(policy.recovery_target_points) - required_points
        payload["required_points"] = round(required_points, 10)
        payload["expected_net_points"] = round(expected_net_points, 10)
        payload["net_margin_points"] = round(net_margin_points, 10)
        payload["target_shortfall_points"] = round(
            max(0.0, -net_margin_points),
            10,
        )

        max_spread = policy.max_entry_spread_points
        if max_spread is not None and spread_points > float(max_spread):
            return RecoveryCostDecision(False, spread_reason, payload)

        if float(policy.recovery_target_points) <= required_points:
            return RecoveryCostDecision(False, target_reason, payload)
        return RecoveryCostDecision(True, ok_reason, payload)


class RecoveryExitModel:
    """Evaluate cycle exits using net points after explicit cost budget."""

    def evaluate_exit(
        self,
        policy: RecoveryPolicy,
        cycle: RecoveryCycleState,
        snapshot: RecoveryMarketSnapshot,
    ) -> RecoveryDecision:
        if cycle.status != "open":
            return _decision("hold", "cycle_not_open")
        if snapshot.symbol != cycle.symbol:
            return _decision(
                "block",
                "symbol_mismatch",
                metadata={"expected": cycle.symbol, "actual": snapshot.symbol},
            )
        if not snapshot.has_bid_ask():
            return _decision("block", "quote_side_missing")

        exit_price = snapshot.exit_price(cycle.direction)
        assert exit_price is not None
        favorable_points = _favorable_points(policy, cycle, exit_price=exit_price)
        cost_buffer_points = round(
            float(policy.slippage_budget_points) + float(policy.commission_points),
            10,
        )
        net_profit_points = round(favorable_points - cost_buffer_points, 10)
        metadata = {
            "exit_price": exit_price,
            "favorable_points": favorable_points,
            "cost_buffer_points": cost_buffer_points,
            "net_profit_points": net_profit_points,
            "target_net_points": float(policy.recovery_target_points),
        }
        if policy.recovery_target_points > 0 and net_profit_points >= float(
            policy.recovery_target_points
        ):
            return _decision(
                "close_cycle",
                "net_recovery_target_reached",
                exit_price=exit_price,
                metadata=metadata,
            )

        max_loss = float(policy.max_cycle_loss_points)
        loss_points = round(max(0.0, -net_profit_points), 10)
        metadata["loss_points"] = loss_points
        metadata["max_cycle_loss_points"] = max_loss
        if max_loss > 0 and loss_points >= max_loss:
            return _decision(
                "close_cycle",
                "cycle_loss_limit_reached",
                exit_price=exit_price,
                metadata=metadata,
            )

        max_duration = float(policy.max_cycle_duration_seconds)
        cycle_age_seconds = _cycle_age_seconds(cycle, snapshot)
        metadata["cycle_age_seconds"] = cycle_age_seconds
        metadata["max_cycle_duration_seconds"] = max_duration
        if (
            max_duration > 0
            and cycle_age_seconds is not None
            and cycle_age_seconds >= max_duration
        ):
            return _decision(
                "close_cycle",
                "max_cycle_duration_reached",
                exit_price=exit_price,
                metadata=metadata,
            )

        metadata["step_count"] = int(cycle.step_count)
        metadata["max_steps"] = int(policy.max_steps)
        metadata["max_steps_exit_mode"] = str(policy.max_steps_exit_mode)
        max_steps_hold = float(policy.max_steps_hold_seconds)
        max_steps_hold_elapsed = _last_step_age_seconds(cycle, snapshot)
        metadata["max_steps_hold_seconds"] = max_steps_hold
        metadata["max_steps_hold_elapsed_seconds"] = max_steps_hold_elapsed
        if (
            max_steps_hold > 0
            and cycle.step_count >= policy.max_steps
            and max_steps_hold_elapsed is not None
            and max_steps_hold_elapsed >= max_steps_hold
        ):
            return _decision(
                "close_cycle",
                "max_steps_hold_time_reached",
                exit_price=exit_price,
                metadata=metadata,
            )
        if (
            str(policy.max_steps_exit_mode) == "close_cycle"
            and cycle.step_count >= policy.max_steps
        ):
            return _decision(
                "close_cycle",
                "max_steps_exit_reached",
                exit_price=exit_price,
                metadata=metadata,
            )

        if policy.recovery_target_points <= 0:
            return _decision(
                "hold",
                "recovery_target_disabled",
                exit_price=exit_price,
                metadata=metadata,
            )
        return _decision(
            "hold",
            "net_recovery_target_not_reached",
            exit_price=exit_price,
            metadata=metadata,
        )


def _normalize_direction(value: str) -> str | None:
    direction = str(value or "").strip().lower()
    return direction if direction in {"buy", "sell"} else None


def _pressure_delta(snapshot: TickFeatureSnapshot) -> float | None:
    if snapshot.buy_pressure is None or snapshot.sell_pressure is None:
        return None
    return round(float(snapshot.buy_pressure) - float(snapshot.sell_pressure), 10)


def _elapsed_seconds(previous: datetime | None, current: datetime) -> float | None:
    if previous is None:
        return None
    if previous.tzinfo is None and current.tzinfo is not None:
        previous = previous.replace(tzinfo=current.tzinfo)
    if current.tzinfo is None and previous.tzinfo is not None:
        current = current.replace(tzinfo=previous.tzinfo)
    return round(max(0.0, (current - previous).total_seconds()), 10)


def _spread_points(
    policy: RecoveryPolicy,
    snapshot: TickFeatureSnapshot,
) -> float | None:
    if snapshot.spread_points is not None:
        return round(float(snapshot.spread_points), 10)
    if snapshot.bid is None or snapshot.ask is None:
        return None
    return round(max(0.0, float(snapshot.ask) - float(snapshot.bid)) / policy.point, 10)


def _favorable_points(
    policy: RecoveryPolicy,
    cycle: RecoveryCycleState,
    *,
    exit_price: float,
) -> float:
    if cycle.direction == "buy":
        raw = float(exit_price) - float(cycle.average_entry_price)
    else:
        raw = float(cycle.average_entry_price) - float(exit_price)
    return round(raw / float(policy.point), 10)


def _cycle_age_seconds(
    cycle: RecoveryCycleState,
    snapshot: RecoveryMarketSnapshot,
) -> float | None:
    started_at = cycle.started_at
    observed_at = snapshot.time
    if started_at is None or observed_at is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=observed_at.tzinfo)
    if observed_at.tzinfo is None and started_at.tzinfo is not None:
        observed_at = observed_at.replace(tzinfo=started_at.tzinfo)
    return round(max(0.0, (observed_at - started_at).total_seconds()), 10)


def _last_step_age_seconds(
    cycle: RecoveryCycleState,
    snapshot: RecoveryMarketSnapshot,
) -> float | None:
    last_step_at = cycle.last_step_at
    observed_at = snapshot.time
    if last_step_at is None or observed_at is None:
        return None
    if last_step_at.tzinfo is None and observed_at.tzinfo is not None:
        last_step_at = last_step_at.replace(tzinfo=observed_at.tzinfo)
    if observed_at.tzinfo is None and last_step_at.tzinfo is not None:
        observed_at = observed_at.replace(tzinfo=last_step_at.tzinfo)
    return round(max(0.0, (observed_at - last_step_at).total_seconds()), 10)


def _decision(
    action: str,
    reason: str,
    *,
    exit_price: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> RecoveryDecision:
    return RecoveryDecision(
        action=action,
        reason=reason,
        exit_price=exit_price,
        metadata=dict(metadata or {}),
    )
