from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.trading.recovery.execution import RecoveryExecutionCanaryPolicy
from src.trading.recovery.models import RecoveryPolicy

from .recovery import RecoveryReplayReport


@dataclass(frozen=True)
class RecoveryCanaryGatePolicy:
    min_tick_count: int = 1
    min_tick_coverage: float = 0.95
    max_blocked_ratio: float = 0.05
    require_closed: bool = True
    ignored_block_reasons_for_ratio: tuple[str, ...] = (
        "max_steps_reached",
        "max_next_volume_exceeded",
        "max_total_volume_exceeded",
    )

    def __post_init__(self) -> None:
        if self.min_tick_count < 0:
            raise ValueError("min_tick_count must be >= 0")
        if not 0.0 <= self.min_tick_coverage <= 1.0:
            raise ValueError("min_tick_coverage must be between 0 and 1")
        if not 0.0 <= self.max_blocked_ratio <= 1.0:
            raise ValueError("max_blocked_ratio must be between 0 and 1")


@dataclass(frozen=True)
class RecoveryCanaryGateDecision:
    allowed: bool
    stage: str
    reasons: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


class RecoveryCanaryGate:
    """Promotes offline recovery replay facts into demo canary readiness."""

    def __init__(self, policy: RecoveryCanaryGatePolicy | None = None) -> None:
        self._policy = policy or RecoveryCanaryGatePolicy()

    def assess(
        self,
        *,
        report: RecoveryReplayReport,
        recovery_policy: RecoveryPolicy,
        canary_policy: RecoveryExecutionCanaryPolicy,
    ) -> RecoveryCanaryGateDecision:
        reasons: list[str] = []
        if not canary_policy.enabled:
            reasons.append("recovery_canary_not_enabled")
        if not canary_policy.dry_run:
            reasons.append("real_order_requires_reviewed_dry_run")
        if report.tick_count < self._policy.min_tick_count:
            reasons.append("tick_count_below_minimum")
        if report.tick_coverage < self._policy.min_tick_coverage:
            reasons.append("tick_coverage_below_minimum")
        if self._policy.require_closed and not report.closed:
            reasons.append("replay_cycle_not_closed")
        if report.max_step_count > recovery_policy.max_steps:
            reasons.append("replay_exceeded_max_steps")
        if report.max_total_volume > recovery_policy.max_total_volume:
            reasons.append("replay_exceeded_max_total_volume")

        event_count = len(report.events)
        ignored_reasons = {
            str(reason).strip()
            for reason in self._policy.ignored_block_reasons_for_ratio
            if str(reason).strip()
        }
        blocked_events = [
            event
            for event in report.events
            if str(event.action).strip().lower() == "block"
        ]
        ignored_blocked_count = sum(
            1 for event in blocked_events if event.reason in ignored_reasons
        )
        effective_blocked_count = max(0, len(blocked_events) - ignored_blocked_count)
        blocked_ratio = (
            effective_blocked_count / event_count if event_count > 0 else 0.0
        )
        if blocked_ratio > self._policy.max_blocked_ratio:
            reasons.append("blocked_ratio_above_maximum")

        details = {
            "symbol": report.symbol,
            "direction": report.direction,
            "tick_count": report.tick_count,
            "tradable_tick_count": report.tradable_tick_count,
            "tick_coverage": report.tick_coverage,
            "blocked_count": report.blocked_count,
            "effective_blocked_count": effective_blocked_count,
            "ignored_blocked_count": ignored_blocked_count,
            "ignored_block_reasons_for_ratio": sorted(ignored_reasons),
            "blocked_ratio": round(blocked_ratio, 8),
            "closed": report.closed,
            "close_reason": report.close_reason,
            "max_step_count": report.max_step_count,
            "max_total_volume": report.max_total_volume,
            "policy_max_steps": recovery_policy.max_steps,
            "policy_max_total_volume": recovery_policy.max_total_volume,
            "canary_enabled": canary_policy.enabled,
            "canary_dry_run": canary_policy.dry_run,
        }
        allowed = not reasons
        return RecoveryCanaryGateDecision(
            allowed=allowed,
            stage="dry_run_canary" if allowed else "blocked",
            reasons=tuple(reasons),
            details=details,
        )
