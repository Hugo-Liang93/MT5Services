"""Resident demo recovery runner orchestrator.

N1: 模块结构（拆分自原 1928 行单文件）：
- ``runner_settings.py`` — RecoveryRuntimeRunnerSettings dataclass
- ``runner_helpers.py``  — 模块级工具函数（cycle id / decision payload / position
  matching / 时间格式化），无状态。
- ``runner.py`` (本文件) — DemoBoundedRecoveryRunner 主类与生命周期编排。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from src.market.tick_features.models import TickFeatureSnapshot

from .analytics import RecoveryDecisionAnalytics
from .calibration_guard import (
    RecoveryCostCalibrationGuard,
    RecoveryCostCalibrationGuardSettings,
)
from .canary import open_initial_recovery_cycle
from .controller import BoundedRecoveryController
from .entry_policies import (
    RecoveryCostGate,
    RecoveryDirectionPolicy,
    RecoveryEntrySignalConfirmer,
)
from .execution import RecoveryExecutionAdapter, RecoveryExecutionCanaryPolicy
from .exposure_ledger import RecoveryExposureLedger
from .models import (
    RecoveryCycleState,
    RecoveryExecutionPlan,
    RecoveryMarketSnapshot,
    RecoveryPolicy,
)
from .risk_budget import (
    RecoveryRiskBudgetGuard,
    RecoveryRiskBudgetSettings,
    recovery_row_is_dry_run_cycle,
)
from .runner_helpers import cleanup_result_metadata as _cleanup_result_metadata
from .runner_helpers import (
    close_result_position_absent as _close_result_position_absent,
)
from .runner_helpers import close_result_success as _close_result_success
from .runner_helpers import cycle_close_status_reason as _cycle_close_status_reason
from .runner_helpers import cycle_with_submitted_ticket as _cycle_with_submitted_ticket
from .runner_helpers import datetime_after_or_equal as _datetime_after_or_equal
from .runner_helpers import decision_counts_payload as _decision_counts_payload
from .runner_helpers import decision_payload as _decision_payload
from .runner_helpers import default_cycle_id as _default_cycle_id
from .runner_helpers import default_source_signal_id as _default_source_signal_id
from .runner_helpers import iso_or_none as _iso
from .runner_helpers import matching_recovery_positions as _matching_recovery_positions
from .runner_helpers import parse_datetime as _parse_datetime
from .runner_helpers import policy_decision_payload as _policy_decision_payload
from .runner_helpers import position_absent_message as _position_absent_message
from .runner_helpers import (
    position_identity_matches_cycle as _position_identity_matches_cycle,
)
from .runner_helpers import position_match_payload as _position_match_payload
from .runner_helpers import position_ticket as _position_ticket
from .runner_helpers import submitted_tickets as _submitted_tickets
from .runner_helpers import ticket_from_nested_payload as _ticket_from_nested_payload
from .runner_helpers import unique_positive_ints as _unique_positive_ints
from .runner_helpers import utc_now as _utc_now

# Re-export for back-compat: 外部 `from src.trading.recovery.runner import
# RecoveryRuntimeRunnerSettings` 仍可工作。
from .runner_settings import RecoveryRuntimeRunnerSettings  # noqa: F401

Clock = Callable[[], datetime]
CycleIdFactory = Callable[[datetime], str]
SourceSignalIdFactory = Callable[[str], str]


# RecoveryRuntimeRunnerSettings 已抽到 runner_settings.py（N1 拆分）。
# 以下原 dataclass 内容已删除。


class DemoBoundedRecoveryRunner:
    """Resident demo-only recovery runner driven by tick feature snapshots.

    This service is an application-layer coordinator. It does not calculate tick
    features, read market caches, own account state, or bypass the recovery
    controller. Its only inputs are public tick feature snapshots and durable
    recovery state; its only outputs are recovery state records and guarded
    trading-port dry-run dispatches.
    """

    def __init__(
        self,
        *,
        settings: RecoveryRuntimeRunnerSettings,
        account_alias: str,
        account_key: str,
        state_store: Any,
        trading_port: Any,
        position_snapshot_provider: Any | None = None,
        controller: BoundedRecoveryController | None = None,
        direction_policy: RecoveryDirectionPolicy | None = None,
        cost_gate: RecoveryCostGate | None = None,
        calibration_guard: RecoveryCostCalibrationGuard | None = None,
        risk_budget_guard: RecoveryRiskBudgetGuard | None = None,
        execution_adapter: RecoveryExecutionAdapter | None = None,
        risk_profile_contract: Mapping[str, Any] | None = None,
        clock: Clock | None = None,
        cycle_id_factory: CycleIdFactory | None = None,
        source_signal_id_factory: SourceSignalIdFactory | None = None,
    ) -> None:
        self._settings = settings
        self._account_alias = str(account_alias or "")
        self._account_key = str(account_key or "")
        self._state_store = state_store
        self._trading_port = trading_port
        self._position_snapshot_provider = position_snapshot_provider
        self._controller = controller or BoundedRecoveryController()
        self._direction_policy = direction_policy or RecoveryDirectionPolicy()
        self._cost_gate = cost_gate or RecoveryCostGate()
        self._entry_confirmer = RecoveryEntrySignalConfirmer()
        self._calibration_guard = calibration_guard or RecoveryCostCalibrationGuard()
        self._risk_budget_guard = risk_budget_guard or RecoveryRiskBudgetGuard()
        self._execution_adapter = execution_adapter or RecoveryExecutionAdapter(
            trading_port=trading_port
        )
        self._risk_profile_contract = dict(risk_profile_contract or {})
        self._exposure_ledger = RecoveryExposureLedger()
        self._clock = clock or _utc_now
        self._cycle_id_factory = cycle_id_factory or _default_cycle_id
        self._source_signal_id_factory = (
            source_signal_id_factory or _default_source_signal_id
        )

        self._running = False
        self._started_at: datetime | None = None
        self._active_cycle: RecoveryCycleState | None = None
        self._processed_snapshots = 0
        self._ignored_snapshots = 0
        self._session_cycle_count = 0
        self._decision_counts: Counter[str] = Counter()
        self._decision_analytics = RecoveryDecisionAnalytics()
        self._last_snapshot_at: datetime | None = None
        self._last_decision_at: datetime | None = None
        self._last_action: str | None = None
        self._last_reason: str | None = None
        self._last_execution_status: str | None = None
        self._last_trace_id: str | None = None
        self._last_error: str | None = None
        self._consecutive_errors = 0
        self._last_position_check_at: datetime | None = None
        self._last_position_check_error: str | None = None
        self._last_calibration_guard: dict[str, Any] | None = None
        self._blocked_dispatch_attempts: dict[tuple[str, str, int], datetime] = {}

    def start(self) -> None:
        if self._running:
            return
        if not self._settings.enabled:
            self._last_reason = "runner_disabled"
            return
        self._assert_demo_dry_run_contract()
        self._active_cycle = self._load_open_cycle()
        self._running = True
        self._started_at = self._clock()
        self._last_reason = "started"

    def stop(self) -> None:
        self._running = False
        self._last_reason = "stopped"

    def is_running(self) -> bool:
        return self._running

    def on_tick_feature_snapshot(self, snapshot: TickFeatureSnapshot) -> dict[str, Any]:
        if not self._running:
            return self._ignore("runner_not_running")
        if str(snapshot.symbol).strip().upper() != self._settings.symbol.upper():
            return self._ignore("symbol_mismatch", expected=self._settings.symbol)

        self._processed_snapshots += 1
        self._last_snapshot_at = snapshot.generated_at
        if str(snapshot.status or "").strip().lower() != "healthy":
            return self._ignore(
                f"tick_feature_{snapshot.status}",
                reasons=list(snapshot.reasons),
                counted_as_processed=True,
            )
        market_snapshot = self._market_snapshot(snapshot)
        if not market_snapshot.has_bid_ask():
            return self._ignore(
                "quote_side_missing",
                reasons=list(snapshot.reasons),
                counted_as_processed=True,
            )

        try:
            if self._active_cycle is None:
                return self._open_initial_cycle(market_snapshot, snapshot)
            exposure_reconciled = self._reconcile_submitted_cycle_exposure(
                market_snapshot
            )
            if exposure_reconciled is not None:
                return exposure_reconciled
            exit_decision = self._controller.evaluate_cycle_exit(
                self._recovery_policy,
                self._active_cycle,
                market_snapshot,
            )
            if exit_decision.action == "close_cycle":
                return self._close_dry_run_cycle(exit_decision)

            step_decision = self._controller.evaluate_next_step(
                self._recovery_policy,
                self._active_cycle,
                market_snapshot,
                now=market_snapshot.time,
            )
            if step_decision.action == "open_step":
                step_cost_decision = self._cost_gate.assess_step(
                    self._recovery_policy,
                    cycle=self._active_cycle,
                    decision=step_decision,
                    snapshot=snapshot,
                )
                step_cost_payload = _policy_decision_payload(step_cost_decision)
                if not step_cost_decision.allowed:
                    return self._record_decision(
                        action="hold",
                        reason=step_cost_decision.reason,
                        status="hold",
                        decision=step_decision,
                        execution={"cost_gate": step_cost_payload},
                    )
                step_decision = replace(
                    step_decision,
                    metadata={
                        **dict(step_decision.metadata or {}),
                        "cost_gate": step_cost_payload,
                    },
                )
                return self._open_recovery_step(step_decision, market_snapshot.time)
            return self._record_decision(
                action=step_decision.action,
                reason=step_decision.reason,
                status=step_decision.action,
                decision=step_decision,
            )
        except Exception as exc:  # noqa: BLE001 - runner health must expose failures.
            self._consecutive_errors += 1
            self._last_error = str(exc)
            self._last_reason = "runner_exception"
            return {
                "status": "error",
                "reason": "runner_exception",
                "error_message": str(exc),
            }

    def status(self) -> dict[str, Any]:
        now = self._clock()
        stale_age = self._snapshot_age_seconds(now)
        stalled = bool(
            self._running
            and stale_age is not None
            and stale_age > float(self._settings.snapshot_stale_seconds)
        )
        stall_reasons = ["tick_feature_snapshot_stale"] if stalled else []
        active = self._active_cycle
        active_submitted_tickets = _submitted_tickets(active) if active else []
        exposure_snapshot = self._exposure_snapshot(active) if active else None
        active_live_position_tickets = (
            list(exposure_snapshot.live_position_tickets)
            if exposure_snapshot is not None
            else []
        )
        return {
            "enabled": bool(self._settings.enabled),
            "running": self._running,
            "dry_run": bool(self._settings.dry_run),
            "demo_only": bool(self._settings.demo_only),
            "symbol": self._settings.symbol,
            "direction": self._settings.direction,
            "direction_mode": self._settings.direction_mode,
            "strategy": self._settings.strategy,
            "timeframe": self._settings.timeframe,
            "risk_profile": self._settings.risk_profile,
            "risk_profile_contract": self._risk_profile_contract_status(),
            "account_alias": self._account_alias,
            "account_key": self._account_key,
            "active_cycle_id": active.cycle_id if active is not None else None,
            "active_cycle_status": active.status if active is not None else None,
            "active_step_count": active.step_count if active is not None else None,
            "active_submitted_tickets": [
                int(item["ticket"]) for item in active_submitted_tickets
            ],
            "active_live_position_tickets": active_live_position_tickets,
            "exposure_ledger": (
                exposure_snapshot.to_payload()
                if exposure_snapshot is not None
                else {
                    "status": "none",
                    "fresh": True,
                    "submitted_tickets": [],
                    "live_position_tickets": [],
                    "pending_submitted_tickets": [],
                    "position_matches": [],
                    "last_reconcile_at": None,
                    "absent_confirmed": False,
                }
            ),
            "processed_snapshots": self._processed_snapshots,
            "ignored_snapshots": self._ignored_snapshots,
            "session_cycle_count": self._session_cycle_count,
            "pacing": self._cycle_pacing_snapshot(now),
            "step_policy": self._step_policy_status(),
            "exit_policy": self._exit_policy_status(),
            "entry_confirmation": self._entry_confirmation_status(),
            "risk_budget": self._risk_budget_status(now),
            "decision_counts": _decision_counts_payload(self._decision_counts),
            "decision_analytics": self._decision_analytics.snapshot(),
            "calibration_guard": self._calibration_guard_status(),
            "started_at": _iso(self._started_at),
            "last_snapshot_at": _iso(self._last_snapshot_at),
            "last_decision_at": _iso(self._last_decision_at),
            "last_action": self._last_action,
            "last_reason": self._last_reason,
            "last_execution_status": self._last_execution_status,
            "last_trace_id": self._last_trace_id,
            "last_error": self._last_error,
            "consecutive_errors": self._consecutive_errors,
            "last_position_check_at": _iso(self._last_position_check_at),
            "last_position_check_error": self._last_position_check_error,
            "snapshot_age_seconds": stale_age,
            "snapshot_stale_seconds": float(self._settings.snapshot_stale_seconds),
            "stalled": stalled,
            "stall_reasons": stall_reasons,
        }

    @property
    def _recovery_policy(self) -> RecoveryPolicy:
        return self._settings.to_recovery_policy()

    @property
    def _canary_policy(self) -> RecoveryExecutionCanaryPolicy:
        return self._settings.to_canary_policy()

    @property
    def _calibration_guard_settings(self) -> RecoveryCostCalibrationGuardSettings:
        return self._settings.to_calibration_guard_settings()

    @property
    def _risk_budget_settings(self) -> RecoveryRiskBudgetSettings:
        return self._settings.to_risk_budget_settings()

    def _risk_profile_contract_status(self) -> dict[str, Any]:
        payload = dict(self._risk_profile_contract)
        payload.setdefault("name", self._settings.risk_profile)
        payload.setdefault("policy", None)
        payload.setdefault("trade_frequency_enabled", None)
        payload.setdefault("pre_trade_rules", [])
        payload.setdefault("source", "runner_settings")
        payload["pre_trade_rules"] = [
            str(item) for item in list(payload.get("pre_trade_rules") or [])
        ]
        return payload

    def _open_initial_cycle(
        self,
        snapshot: RecoveryMarketSnapshot,
        feature_snapshot: TickFeatureSnapshot,
    ) -> dict[str, Any]:
        if (
            self._settings.max_cycles_per_session > 0
            and self._session_cycle_count >= self._settings.max_cycles_per_session
        ):
            self._entry_confirmer.reset()
            return self._record_decision(
                action="hold",
                reason="max_cycles_per_session_reached",
                status="hold",
            )
        if self._daily_cycle_count() >= self._settings.max_cycles_per_day > 0:
            self._entry_confirmer.reset()
            return self._record_decision(
                action="hold",
                reason="max_cycles_per_day_reached",
                status="hold",
                execution={
                    "max_cycles_per_day": int(self._settings.max_cycles_per_day)
                },
            )
        budget_decision = self._assess_risk_budget()
        if not budget_decision.allowed:
            self._entry_confirmer.reset()
            return self._record_decision(
                action="hold",
                reason=budget_decision.reason,
                status="hold",
                execution={"risk_budget": budget_decision.snapshot},
            )
        pacing = self._cycle_pacing_snapshot(self._clock())
        pacing_reason = str(pacing.get("active_reason") or "")
        if pacing_reason:
            self._entry_confirmer.reset()
            return self._record_decision(
                action="hold",
                reason=pacing_reason,
                status="hold",
                execution={"pacing": pacing},
            )
        direction_decision = self._direction_policy.choose_direction(
            self._recovery_policy,
            feature_snapshot,
            fallback_direction=self._settings.direction,
        )
        policy_execution = {
            "risk_profile": self._settings.risk_profile,
            "direction_policy": _policy_decision_payload(direction_decision),
        }
        if direction_decision.direction is None:
            self._entry_confirmer.reset()
            return self._record_decision(
                action="hold",
                reason=direction_decision.reason,
                status="hold",
                execution=policy_execution,
            )
        cost_decision = self._cost_gate.assess_entry(
            self._recovery_policy,
            direction=direction_decision.direction,
            snapshot=feature_snapshot,
        )
        policy_execution["cost_gate"] = _policy_decision_payload(cost_decision)
        if not cost_decision.allowed:
            self._entry_confirmer.reset()
            return self._record_decision(
                action="hold",
                reason=cost_decision.reason,
                status="hold",
                execution=policy_execution,
            )
        confirmation_decision = self._entry_confirmer.assess(
            direction=direction_decision.direction,
            observed_at=feature_snapshot.generated_at,
            required_snapshots=int(self._settings.entry_confirmation_snapshots),
            max_gap_seconds=float(self._settings.entry_confirmation_max_gap_seconds),
        )
        policy_execution["entry_confirmation"] = _policy_decision_payload(
            confirmation_decision
        )
        if not confirmation_decision.allowed:
            return self._record_decision(
                action="hold",
                reason=confirmation_decision.reason,
                status="hold",
                execution=policy_execution,
            )
        calibration_decision = self._calibration_guard.assess(
            settings=self._calibration_guard_settings,
            dry_run=bool(self._settings.dry_run),
            analytics_snapshot=self._decision_analytics.snapshot(),
        )
        calibration_payload = _policy_decision_payload(calibration_decision)
        self._last_calibration_guard = calibration_payload
        policy_execution["calibration_guard"] = calibration_payload
        if not calibration_decision.allowed:
            return self._record_decision(
                action="hold",
                reason=calibration_decision.reason,
                status="hold",
                execution=policy_execution,
            )
        blocked_retry = self._blocked_dispatch_retry_decision(
            scope="initial",
            cycle_id="",
            step_index=0,
            wait_reason="blocked_initial_retry_wait",
        )
        if blocked_retry is not None:
            return blocked_retry
        entry_price = snapshot.entry_price(direction_decision.direction)
        if entry_price is None:
            return self._ignore("quote_side_missing", counted_as_processed=True)
        cycle_id = self._cycle_id_factory(self._clock())
        source_signal_id = self._source_signal_id_factory(cycle_id)
        result = open_initial_recovery_cycle(
            symbol=self._settings.symbol,
            direction=direction_decision.direction,
            entry_price=entry_price,
            recovery_policy=self._recovery_policy,
            canary_policy=self._canary_policy,
            account_alias=self._account_alias,
            account_key=self._account_key,
            cycle_id=cycle_id,
            source_signal_id=source_signal_id,
            state_store=self._state_store,
            trading_port=self._trading_port,
            strategy=self._settings.strategy,
            timeframe=self._settings.timeframe,
            extra_metadata=policy_execution,
            status_reason=(
                "resident_recovery_initial_dry_run"
                if self._settings.dry_run
                else "resident_recovery_initial_submitted"
            ),
        )
        status = str(result.get("status") or "unknown")
        self._last_execution_status = status
        self._last_trace_id = source_signal_id
        if status in {"dry_run", "submitted"}:
            cycle = result.get("cycle")
            if isinstance(cycle, RecoveryCycleState):
                if status == "submitted":
                    cycle = _cycle_with_submitted_ticket(
                        cycle,
                        scope="initial",
                        step_index=0,
                        execution_result=result,
                    )
                    if not _submitted_tickets(cycle):
                        blocked_cycle = replace(
                            cycle,
                            status="blocked",
                            updated_at=self._clock(),
                        )
                        self._state_store.record_recovery_cycle_state(
                            blocked_cycle,
                            status_reason="resident_recovery_initial_ticket_missing",
                        )
                        self._remember_blocked_dispatch(
                            scope="initial",
                            cycle_id="",
                            step_index=0,
                        )
                        return self._record_decision(
                            action="block",
                            reason="submitted_ticket_missing",
                            status="blocked",
                            execution=result,
                        )
                    self._state_store.record_recovery_cycle_state(
                        cycle,
                        status_reason="resident_recovery_initial_submitted",
                    )
                    self._clear_blocked_dispatch(
                        scope="initial",
                        cycle_id="",
                        step_index=0,
                    )
                self._entry_confirmer.reset()
                self._active_cycle = cycle
                self._session_cycle_count += 1
            return self._record_decision(
                action="open_initial",
                reason="initial_dispatched",
                status=status,
                execution=result,
            )
        self._remember_blocked_dispatch(
            scope="initial",
            cycle_id="",
            step_index=0,
        )
        self._entry_confirmer.reset()
        return self._record_decision(
            action="block" if status == "blocked" else "hold",
            reason=str(result.get("reason") or status),
            status=status,
            execution=result,
        )

    def _open_recovery_step(
        self,
        decision: Any,
        observed_at: datetime,
    ) -> dict[str, Any]:
        assert self._active_cycle is not None
        blocked_retry = self._blocked_step_retry_decision(decision)
        if blocked_retry is not None:
            return blocked_retry
        plan = RecoveryExecutionPlan.from_decision(
            cycle=self._active_cycle,
            decision=decision,
            created_at=observed_at,
        )
        assert plan.position_scaling_intent is not None
        scaling_intent = replace(
            plan.position_scaling_intent,
            metadata={
                "risk_profile": self._settings.risk_profile,
                **dict(plan.position_scaling_intent.metadata or {}),
            },
        )
        execution = self._execution_adapter.execute_scaling_intent(
            recovery_policy=self._recovery_policy,
            cycle=self._active_cycle,
            intent=scaling_intent,
            canary_policy=self._canary_policy,
        )
        status = str(execution.get("status") or "unknown")
        self._last_execution_status = status
        if status in {"dry_run", "submitted"}:
            updated_cycle = self._controller.apply_open_step(
                self._active_cycle,
                decision,
                now=observed_at,
            )
            if status == "submitted":
                previous_tickets = {
                    int(item["ticket"]) for item in _submitted_tickets(updated_cycle)
                }
                updated_cycle = _cycle_with_submitted_ticket(
                    updated_cycle,
                    scope=f"step_{decision.step_index}",
                    step_index=int(decision.step_index or 0),
                    execution_result=execution,
                )
                current_tickets = {
                    int(item["ticket"]) for item in _submitted_tickets(updated_cycle)
                }
                if current_tickets == previous_tickets:
                    self._remember_blocked_step(decision)
                    return self._record_decision(
                        action="block",
                        reason="submitted_ticket_missing",
                        status="blocked",
                        decision=decision,
                        execution=execution,
                    )
            self._state_store.record_recovery_cycle_state(
                updated_cycle,
                status_reason=(
                    "resident_recovery_step_dry_run"
                    if status == "dry_run"
                    else "resident_recovery_step_submitted"
                ),
            )
            self._active_cycle = updated_cycle
            self._clear_blocked_step(decision)
            return self._record_decision(
                action="open_step",
                reason=decision.reason,
                status=status,
                decision=decision,
                execution=execution,
            )
        self._remember_blocked_step(decision)
        return self._record_decision(
            action="block",
            reason=str(execution.get("reason") or decision.reason),
            status=status,
            decision=decision,
            execution=execution,
        )

    def _blocked_step_retry_decision(self, decision: Any) -> dict[str, Any] | None:
        if self._active_cycle is None:
            return None
        step_index = getattr(decision, "step_index", None)
        try:
            normalized_step_index = int(step_index)
        except (TypeError, ValueError):
            return None
        return self._blocked_dispatch_retry_decision(
            scope="step",
            cycle_id=str(self._active_cycle.cycle_id),
            step_index=normalized_step_index,
            wait_reason="blocked_step_retry_wait",
            decision=decision,
        )

    def _blocked_dispatch_retry_decision(
        self,
        *,
        scope: str,
        cycle_id: str,
        step_index: int,
        wait_reason: str,
        decision: Any | None = None,
    ) -> dict[str, Any] | None:
        retry_seconds = float(self._settings.blocked_dispatch_retry_seconds)
        if retry_seconds <= 0:
            return None
        key = self._blocked_dispatch_key(
            scope=scope,
            cycle_id=cycle_id,
            step_index=step_index,
        )
        last_attempt = self._blocked_dispatch_attempts.get(key)
        if last_attempt is None:
            return None
        elapsed = max(0.0, (self._clock() - last_attempt).total_seconds())
        if elapsed >= retry_seconds:
            return None
        return self._record_decision(
            action="hold",
            reason=wait_reason,
            status="hold",
            decision=decision,
            execution={
                "blocked_dispatch_retry_seconds": retry_seconds,
                "elapsed_seconds": elapsed,
                "scope": scope,
            },
        )

    def _remember_blocked_step(self, decision: Any) -> None:
        if self._active_cycle is None:
            return
        try:
            step_index = int(getattr(decision, "step_index"))
        except (TypeError, ValueError):
            return
        self._remember_blocked_dispatch(
            scope="step",
            cycle_id=str(self._active_cycle.cycle_id),
            step_index=step_index,
        )

    def _clear_blocked_step(self, decision: Any) -> None:
        if self._active_cycle is None:
            return
        try:
            step_index = int(getattr(decision, "step_index"))
        except (TypeError, ValueError):
            return
        self._clear_blocked_dispatch(
            scope="step",
            cycle_id=str(self._active_cycle.cycle_id),
            step_index=step_index,
        )

    def _remember_blocked_dispatch(
        self,
        *,
        scope: str,
        cycle_id: str,
        step_index: int,
    ) -> None:
        key = self._blocked_dispatch_key(
            scope=scope,
            cycle_id=cycle_id,
            step_index=step_index,
        )
        self._blocked_dispatch_attempts[key] = self._clock()

    def _clear_blocked_dispatch(
        self,
        *,
        scope: str,
        cycle_id: str,
        step_index: int,
    ) -> None:
        key = self._blocked_dispatch_key(
            scope=scope,
            cycle_id=cycle_id,
            step_index=step_index,
        )
        self._blocked_dispatch_attempts.pop(key, None)

    def _blocked_dispatch_key(
        self,
        *,
        scope: str,
        cycle_id: str,
        step_index: int,
    ) -> tuple[str, str, int]:
        cycle_key = str(cycle_id or self._settings.symbol)
        return (str(scope), cycle_key, int(step_index))

    def _close_dry_run_cycle(self, decision: Any) -> dict[str, Any]:
        assert self._active_cycle is not None
        if not self._settings.dry_run:
            return self._close_submitted_cycle(decision)
        closed_at = self._clock()
        closed_cycle = replace(
            self._active_cycle,
            status="closed",
            updated_at=closed_at,
            metadata={
                **dict(self._active_cycle.metadata or {}),
                "close_decision": _decision_payload(decision),
            },
        )
        realized_pnl = None
        if decision.exit_price is not None:
            realized_pnl = self._controller.estimate_pnl(
                self._recovery_policy,
                self._active_cycle,
                exit_price=float(decision.exit_price),
            )
        self._state_store.record_recovery_cycle_state(
            closed_cycle,
            status_reason=_cycle_close_status_reason(decision.reason, dry_run=True),
            closed_at=closed_at,
            close_price=decision.exit_price,
            realized_pnl=realized_pnl,
        )
        self._active_cycle = None
        return self._record_decision(
            action="close_cycle",
            reason=decision.reason,
            status="closed",
            decision=decision,
        )

    def _close_submitted_cycle(self, decision: Any) -> dict[str, Any]:
        assert self._active_cycle is not None
        close_result = self._close_submitted_live_positions(
            cycle=self._active_cycle,
            source_signal_id=self._active_cycle.source_signal_id,
        )
        if close_result.get("status") != "closed":
            return self._record_decision(
                action="block",
                reason=str(close_result.get("reason") or "recovery_close_failed"),
                status=str(close_result.get("status") or "failed"),
                decision=decision,
                execution={"close_result": close_result},
            )

        closed_at = self._clock()
        realized_pnl = None
        if decision.exit_price is not None:
            realized_pnl = self._controller.estimate_pnl(
                self._recovery_policy,
                self._active_cycle,
                exit_price=float(decision.exit_price),
            )
        closed_cycle = replace(
            self._active_cycle,
            status="closed",
            updated_at=closed_at,
            metadata={
                **dict(self._active_cycle.metadata or {}),
                "close_decision": _decision_payload(decision),
                "cleanup_result": _cleanup_result_metadata(close_result),
            },
        )
        self._state_store.record_recovery_cycle_state(
            closed_cycle,
            status_reason=_cycle_close_status_reason(decision.reason, dry_run=False),
            closed_at=closed_at,
            close_price=decision.exit_price,
            realized_pnl=realized_pnl,
        )
        self._active_cycle = None
        payload = self._record_decision(
            action="close_cycle",
            reason=decision.reason,
            status="closed",
            decision=decision,
        )
        payload["close_result"] = close_result
        return payload

    def _close_submitted_live_positions(
        self,
        *,
        cycle: RecoveryCycleState,
        source_signal_id: str | None,
    ) -> dict[str, Any]:
        submitted = _submitted_tickets(cycle)
        submitted_tickets = [int(item["ticket"]) for item in submitted]
        if not submitted:
            return {"status": "failed", "reason": "submitted_ticket_missing"}

        position_snapshot = self._submitted_position_snapshot(cycle)
        if position_snapshot is None:
            return {
                "status": "failed",
                "reason": "position_snapshot_unavailable",
                "submitted_tickets": submitted_tickets,
            }

        live_tickets = _unique_positive_ints(
            position_snapshot.get("matching_position_tickets")
        )
        if not live_tickets and str(position_snapshot.get("status") or "") == "pending":
            live_tickets = _unique_positive_ints(
                position_snapshot.get("pending_submitted_tickets")
            )
        if not live_tickets:
            return {
                "status": "closed",
                "reason": "submitted_positions_absent",
                "tickets": [],
                "submitted_tickets": submitted_tickets,
                "position_snapshot": position_snapshot,
                "results": [],
            }

        results: list[dict[str, Any]] = []
        for ticket in live_tickets:
            scope = f"position_{ticket}"
            payload = {
                "ticket": ticket,
                "deviation": int(self._settings.deviation),
                "comment": f"{self._settings.comment_prefix}-close",
                "actor": "recovery_runner",
                "reason": "resident_recovery_target_reached",
                "action_id": f"recovery:{cycle.cycle_id}:close_{scope}",
                "request_context": {
                    "trace_id": source_signal_id,
                    "cycle_id": cycle.cycle_id,
                    "execution_scope": "recovery_cycle_cleanup",
                    "recovery_close_scope": scope,
                    "submitted_tickets": submitted_tickets,
                    "matching_position_tickets": live_tickets,
                },
            }
            try:
                result = self._trading_port.dispatch_operation("close", payload)
                if _close_result_success(result):
                    status = "closed"
                elif _close_result_position_absent(result):
                    status = "already_closed"
                else:
                    status = "failed"
                results.append(
                    {
                        "scope": scope,
                        "ticket": ticket,
                        "status": status,
                        "payload": payload,
                        "result": result,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - close failures are health facts.
                status = (
                    "already_closed" if _position_absent_message(str(exc)) else "failed"
                )
                results.append(
                    {
                        "scope": scope,
                        "ticket": ticket,
                        "status": status,
                        "payload": payload,
                        "error_message": str(exc),
                    }
                )
        all_closed = all(
            item.get("status") in {"closed", "already_closed"} for item in results
        )
        all_already_closed = bool(results) and all(
            item.get("status") == "already_closed" for item in results
        )
        return {
            "status": "closed" if all_closed else "failed",
            "reason": (
                "cleanup_positions_already_absent"
                if all_already_closed
                else "live_position_cleanup_dispatched"
            ),
            "tickets": live_tickets,
            "submitted_tickets": submitted_tickets,
            "position_snapshot": position_snapshot,
            "results": results,
        }

    def _record_decision(
        self,
        *,
        action: str,
        reason: str,
        status: str,
        decision: Any | None = None,
        execution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        decision_at = self._clock()
        self._decision_counts[action] += 1
        self._last_action = action
        self._last_reason = reason
        self._last_decision_at = decision_at
        self._last_error = None
        self._consecutive_errors = 0
        self._decision_analytics.record(
            observed_at=decision_at,
            action=action,
            reason=reason,
            status=status,
            decision=decision,
            execution=execution,
        )
        payload: dict[str, Any] = {
            "status": status,
            "action": action,
            "reason": reason,
        }
        if decision is not None:
            payload["decision"] = _decision_payload(decision)
        if execution is not None:
            payload["execution"] = execution
        return payload

    def _ignore(
        self,
        reason: str,
        *,
        counted_as_processed: bool = False,
        **details: Any,
    ) -> dict[str, Any]:
        if not counted_as_processed:
            self._ignored_snapshots += 1
        else:
            self._ignored_snapshots += 1
        self._last_reason = reason
        self._last_action = "ignored"
        self._decision_analytics.record(
            observed_at=self._clock(),
            action="ignored",
            reason=reason,
            status="ignored",
            execution=details,
        )
        payload = {
            "status": "ignored",
            "reason": reason,
            **details,
        }
        return payload

    def _reconcile_submitted_cycle_exposure(
        self,
        snapshot: RecoveryMarketSnapshot,
    ) -> dict[str, Any] | None:
        if self._settings.dry_run or self._active_cycle is None:
            return None
        cycle = self._active_cycle
        submitted = _submitted_tickets(cycle)
        if not submitted or self._position_snapshot_provider is None:
            return None

        position_snapshot = self._submitted_position_snapshot(cycle)
        if position_snapshot is None:
            return None
        if str(position_snapshot.get("status") or "") != "absent":
            return None

        closed_at = self._clock()
        exit_price = snapshot.exit_price(cycle.direction)
        realized_pnl = None
        if exit_price is not None:
            realized_pnl = self._controller.estimate_pnl(
                self._recovery_policy,
                cycle,
                exit_price=float(exit_price),
            )
        metadata = dict(cycle.metadata or {})
        metadata["exposure_absent_confirmation"] = {
            "reason": "submitted_positions_absent",
            "submitted_tickets": [int(item["ticket"]) for item in submitted],
            "checked_at": _iso(self._last_position_check_at),
            "last_reconcile_at": position_snapshot.get("last_reconcile_at"),
        }
        closed_cycle = replace(
            cycle,
            status="closed",
            updated_at=closed_at,
            metadata=metadata,
        )
        self._state_store.record_recovery_cycle_state(
            closed_cycle,
            status_reason="resident_recovery_submitted_exposure_absent",
            closed_at=closed_at,
            close_price=exit_price,
            realized_pnl=realized_pnl,
        )
        self._active_cycle = None
        return self._record_decision(
            action="close_cycle",
            reason="submitted_exposure_absent",
            status="closed",
            execution={"position_snapshot": position_snapshot},
        )

    def _submitted_position_snapshot(
        self,
        cycle: RecoveryCycleState,
    ) -> dict[str, Any] | None:
        snapshot = self._exposure_snapshot(cycle)
        if snapshot is None:
            return None
        payload = snapshot.to_payload()
        matches = list(payload.get("position_matches") or [])
        return {
            **payload,
            "matching_position_tickets": list(payload["live_position_tickets"]),
            "matching_positions": matches,
            "matching_position_count": len(matches),
        }

    def _exposure_snapshot(self, cycle: RecoveryCycleState | None):
        if cycle is None:
            return None
        provider = self._position_snapshot_provider
        if provider is None:
            return self._exposure_ledger.classify(
                cycle,
                positions=None,
                last_reconcile_at=None,
            )
        self._last_position_check_at = self._clock()
        self._last_position_check_error = None
        try:
            positions_reader = getattr(provider, "active_positions", None)
            if not callable(positions_reader):
                return self._exposure_ledger.classify(
                    cycle,
                    positions=None,
                    last_reconcile_at=None,
                )
            positions = [
                dict(item)
                for item in list(positions_reader() or [])
                if isinstance(item, Mapping)
            ]
            status_reader = getattr(provider, "status", None)
            provider_status = (
                dict(status_reader() or {}) if callable(status_reader) else {}
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 - provider health is runner health data.
            self._last_position_check_error = str(exc)
            return self._exposure_ledger.classify(
                cycle,
                positions=None,
                last_reconcile_at=None,
            )
        return self._exposure_ledger.classify(
            cycle,
            positions=positions,
            last_reconcile_at=_parse_datetime(provider_status.get("last_reconcile_at")),
        )

    def _active_matching_position_tickets(
        self,
        cycle: RecoveryCycleState,
    ) -> list[int]:
        snapshot = self._submitted_position_snapshot(cycle)
        if snapshot is None:
            return []
        return [
            int(item) for item in list(snapshot.get("matching_position_tickets") or [])
        ]

    def _market_snapshot(self, snapshot: TickFeatureSnapshot) -> RecoveryMarketSnapshot:
        return RecoveryMarketSnapshot(
            symbol=snapshot.symbol,
            bid=snapshot.bid,
            ask=snapshot.ask,
            time=snapshot.generated_at,
            time_msc=snapshot.window_end_msc,
        )

    def _load_open_cycle(self) -> RecoveryCycleState | None:
        loader = getattr(self._state_store, "load_open_recovery_cycle", None)
        if loader is None:
            return None
        cycle = loader(
            symbol=self._settings.symbol,
            strategy=self._settings.strategy,
            cycle_id=None,
        )
        return cycle if isinstance(cycle, RecoveryCycleState) else None

    def _daily_cycle_count(self) -> int:
        limit = int(self._settings.max_cycles_per_day)
        if limit <= 0:
            return 0
        lister = getattr(self._state_store, "list_recovery_cycle_states", None)
        if not callable(lister):
            return 0
        try:
            rows = list(
                lister(
                    symbol=self._settings.symbol,
                    strategy=self._settings.strategy,
                    limit=max(limit * 4, 20),
                )
                or []
            )
        except Exception as exc:  # noqa: BLE001 - fail closed for daily cap checks.
            self._last_error = str(exc)
            return limit
        today = self._clock().astimezone(timezone.utc).date()
        cycle_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if (
                str(row.get("symbol") or "").strip().upper()
                != self._settings.symbol.upper()
            ):
                continue
            if str(row.get("strategy") or "").strip() != self._settings.strategy:
                continue
            if not self._row_matches_account(row):
                continue
            status_reason = str(row.get("status_reason") or "").strip()
            if not status_reason.startswith("resident_recovery_"):
                continue
            if not self._settings.dry_run and recovery_row_is_dry_run_cycle(row):
                continue
            started_at = _parse_datetime(row.get("started_at"))
            if started_at is None:
                continue
            if started_at.astimezone(timezone.utc).date() == today:
                cycle_id = str(row.get("cycle_id") or "").strip()
                if not cycle_id:
                    cycle_id = f"started_at:{started_at.isoformat()}"
                cycle_ids.add(cycle_id)
        return len(cycle_ids)

    def _cycle_pacing_snapshot(self, now: datetime) -> dict[str, Any]:
        min_interval = float(self._settings.min_cycle_interval_seconds)
        close_cooldown = float(self._settings.cooldown_after_cycle_close_seconds)
        hourly_cap = int(self._settings.max_cycles_per_hour)
        enabled = bool(min_interval > 0 or close_cooldown > 0 or hourly_cap > 0)
        payload: dict[str, Any] = {
            "enabled": enabled,
            "account_key": self._account_key,
            "min_cycle_interval_seconds": min_interval,
            "cooldown_after_cycle_close_seconds": close_cooldown,
            "max_cycles_per_hour": hourly_cap,
            "latest_started_at": None,
            "latest_closed_at": None,
            "hourly_cycle_count": 0,
            "next_cycle_not_before": None,
            "remaining_seconds": 0.0,
            "active_reason": None,
            "error": None,
        }
        if not enabled:
            return payload

        rows = self._list_resident_recovery_cycle_rows(limit=max(hourly_cap * 4, 100))
        if rows is None:
            payload["active_reason"] = "cycle_pacing_state_unavailable"
            payload["error"] = self._last_error
            return payload

        latest_started: datetime | None = None
        latest_closed: datetime | None = None
        recent_by_cycle: dict[str, datetime] = {}
        hour_start = now - timedelta(hours=1)
        for row in rows:
            started_at = _parse_datetime(row.get("started_at"))
            closed_at = _parse_datetime(row.get("closed_at"))
            if started_at is not None:
                if latest_started is None or started_at > latest_started:
                    latest_started = started_at
                if started_at >= hour_start:
                    cycle_id = str(row.get("cycle_id") or "").strip()
                    if not cycle_id:
                        cycle_id = f"started_at:{started_at.isoformat()}"
                    previous = recent_by_cycle.get(cycle_id)
                    if previous is None or started_at < previous:
                        recent_by_cycle[cycle_id] = started_at
            if closed_at is not None and (
                latest_closed is None or closed_at > latest_closed
            ):
                latest_closed = closed_at

        payload["latest_started_at"] = _iso(latest_started)
        payload["latest_closed_at"] = _iso(latest_closed)
        payload["hourly_cycle_count"] = len(recent_by_cycle)

        gates: list[tuple[datetime, str]] = []
        if latest_started is not None and min_interval > 0:
            gates.append(
                (
                    latest_started + timedelta(seconds=min_interval),
                    "cycle_min_interval_active",
                )
            )
        if latest_closed is not None and close_cooldown > 0:
            gates.append(
                (
                    latest_closed + timedelta(seconds=close_cooldown),
                    "cycle_close_cooldown_active",
                )
            )
        if hourly_cap > 0 and len(recent_by_cycle) >= hourly_cap:
            earliest_recent = min(recent_by_cycle.values())
            gates.append(
                (
                    earliest_recent + timedelta(hours=1),
                    "max_cycles_per_hour_reached",
                )
            )

        active_gates = [(until, reason) for until, reason in gates if until > now]
        if not active_gates:
            return payload
        next_time, reason = max(active_gates, key=lambda item: item[0])
        payload["next_cycle_not_before"] = _iso(next_time)
        payload["remaining_seconds"] = round(
            max(0.0, (next_time - now).total_seconds()),
            3,
        )
        payload["active_reason"] = reason
        return payload

    def _list_resident_recovery_cycle_rows(
        self, *, limit: int
    ) -> list[Mapping[str, Any]] | None:
        lister = getattr(self._state_store, "list_recovery_cycle_states", None)
        if not callable(lister):
            return []
        try:
            rows = list(
                lister(
                    symbol=self._settings.symbol,
                    strategy=self._settings.strategy,
                    limit=max(int(limit), 1),
                )
                or []
            )
        except Exception as exc:  # noqa: BLE001 - pacing must fail closed.
            self._last_error = str(exc)
            return None

        filtered: list[Mapping[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if (
                str(row.get("symbol") or "").strip().upper()
                != self._settings.symbol.upper()
            ):
                continue
            if str(row.get("strategy") or "").strip() != self._settings.strategy:
                continue
            if not self._row_matches_account(row):
                continue
            status_reason = str(row.get("status_reason") or "").strip()
            if not status_reason.startswith("resident_recovery_"):
                continue
            if not self._settings.dry_run and recovery_row_is_dry_run_cycle(row):
                continue
            filtered.append(row)
        return filtered

    def _row_matches_account(self, row: Mapping[str, Any]) -> bool:
        expected = str(self._account_key or "").strip()
        if not expected:
            return True
        return str(row.get("account_key") or "").strip() == expected

    def _snapshot_age_seconds(self, now: datetime) -> float | None:
        if self._last_snapshot_at is not None:
            return max(0.0, (now - self._last_snapshot_at).total_seconds())
        if self._started_at is not None:
            return max(0.0, (now - self._started_at).total_seconds())
        return None

    def _assert_demo_dry_run_contract(self) -> None:
        if not self._settings.demo_only:
            return
        account_text = f"{self._account_alias} {self._account_key}".lower()
        if "demo" not in account_text:
            raise RuntimeError("recovery runtime runner requires a demo account")

    def _calibration_guard_status(self) -> dict[str, Any]:
        settings = self._calibration_guard_settings
        last = dict(self._last_calibration_guard or {})
        return {
            "enabled": bool(settings.enabled),
            "min_samples": int(settings.min_samples),
            "max_target_shortfall_p90_points": float(
                settings.max_target_shortfall_p90_points
            ),
            "min_net_margin_p50_points": float(settings.min_net_margin_p50_points),
            "allowed": last.get("allowed"),
            "reason": last.get("reason"),
            "metadata": dict(last.get("metadata") or {}),
        }

    def _assess_risk_budget(self) -> Any:
        return self._risk_budget_guard.assess(
            settings=self._risk_budget_settings,
            state_store=self._state_store,
            now=self._clock(),
            account_key=self._account_key,
            symbol=self._settings.symbol,
            strategy=self._settings.strategy,
            dry_run=bool(self._settings.dry_run),
        )

    def _risk_budget_status(self, now: datetime) -> dict[str, Any]:
        return self._risk_budget_guard.assess(
            settings=self._risk_budget_settings,
            state_store=self._state_store,
            now=now,
            account_key=self._account_key,
            symbol=self._settings.symbol,
            strategy=self._settings.strategy,
            dry_run=bool(self._settings.dry_run),
        ).snapshot

    def _exit_policy_status(self) -> dict[str, Any]:
        policy = self._recovery_policy
        return {
            "recovery_target_points": float(policy.recovery_target_points),
            "max_cycle_loss_points": float(policy.max_cycle_loss_points),
            "max_cycle_duration_seconds": float(policy.max_cycle_duration_seconds),
            "max_steps": int(policy.max_steps),
            "max_steps_exit_mode": str(policy.max_steps_exit_mode),
            "max_steps_hold_seconds": float(policy.max_steps_hold_seconds),
        }

    def _step_policy_status(self) -> dict[str, Any]:
        policy = self._recovery_policy
        return {
            "step_distance_points": float(policy.step_distance_points),
            "max_step_adverse_move_points": float(policy.max_step_adverse_move_points),
            "min_step_interval_ms": int(policy.min_step_interval_ms),
            "max_steps": int(policy.max_steps),
            "max_next_volume": policy.max_next_volume,
            "max_total_volume": float(policy.max_total_volume),
        }

    def _entry_confirmation_status(self) -> dict[str, Any]:
        return {
            "required_snapshots": int(self._settings.entry_confirmation_snapshots),
            "max_gap_seconds": float(self._settings.entry_confirmation_max_gap_seconds),
            **self._entry_confirmer.status(),
        }
