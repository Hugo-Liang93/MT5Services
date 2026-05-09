from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from src.backtesting.tick_replay.recovery import (
    RecoveryReplayFill,
    TickRecoveryReplayRunner,
)
from src.backtesting.tick_replay.recovery_canary import (
    RecoveryCanaryGate,
    RecoveryCanaryGatePolicy,
)
from src.clients.mt5_market import Tick
from src.risk.service import PreTradeRiskBlockedError
from src.trading.recovery.controller import BoundedRecoveryController
from src.trading.recovery.execution import (
    RecoveryExecutionAdapter,
    RecoveryExecutionCanaryPolicy,
    extract_blocked_pretrade_snapshot,
    pretrade_block_reason,
)
from src.trading.recovery.models import (
    PositionScalingIntent,
    RecoveryCycleState,
    RecoveryExecutionPlan,
    RecoveryMarketSnapshot,
    RecoveryPolicy,
)


def execute_recovery_canary(
    *,
    symbol: str,
    direction: str,
    ticks: Iterable[Tick],
    recovery_policy: RecoveryPolicy,
    canary_policy: RecoveryExecutionCanaryPolicy,
    gate_policy: RecoveryCanaryGatePolicy,
    account_alias: str,
    account_key: str,
    cycle_id: str,
    source_signal_id: str,
    state_store: Any,
    trading_port: Any,
    strategy: str = "tick_recovery_probe",
    timeframe: str = "TICK",
) -> dict[str, Any]:
    """Run a bounded recovery demo canary through replay, gate, state, and audit.

    This is a canary orchestration service, not a strategy. It deliberately
    emits only the first recovery scaling intent from a replay candidate and
    relies on RecoveryCanaryGate to keep real orders out of this synthetic path.
    """

    report = TickRecoveryReplayRunner().run(
        symbol=symbol,
        direction=direction,
        ticks=ticks,
        policy=recovery_policy,
        account_key=account_key,
        cycle_id=cycle_id,
    )
    gate_decision = RecoveryCanaryGate(gate_policy).assess(
        report=report,
        recovery_policy=recovery_policy,
        canary_policy=canary_policy,
    )
    gate_payload = _gate_payload(gate_decision)
    replay_payload = _replay_payload(report)
    if not gate_decision.allowed:
        return {
            "status": "blocked",
            "reason": "recovery_canary_gate_blocked",
            "gate": gate_payload,
            "replay": replay_payload,
        }

    initial_fill = _first_fill(report.fills, role="initial")
    recovery_fill = _first_fill(report.fills, role="recovery")
    if initial_fill is None or recovery_fill is None:
        return {
            "status": "blocked",
            "reason": "no_recovery_step_candidate",
            "gate": gate_payload,
            "replay": replay_payload,
        }

    cycle = _cycle_from_initial_fill(
        initial_fill,
        account_key=account_key,
        cycle_id=cycle_id,
        source_signal_id=source_signal_id,
        strategy=strategy,
        timeframe=timeframe,
        replay=replay_payload,
        gate=gate_payload,
    )
    state_store.record_recovery_cycle_state(
        cycle,
        status_reason="canary_initial_state",
    )
    intent = _intent_from_recovery_fill(
        recovery_fill,
        account_key=account_key,
        cycle_id=cycle_id,
        source_signal_id=source_signal_id,
        strategy=strategy,
        timeframe=timeframe,
        replay=replay_payload,
        gate=gate_payload,
    )
    execution = RecoveryExecutionAdapter(
        trading_port=trading_port
    ).execute_scaling_intent(
        recovery_policy=recovery_policy,
        cycle=cycle,
        intent=intent,
        canary_policy=canary_policy,
    )
    return {
        "status": execution.get("status", "unknown"),
        "reason": execution.get("reason"),
        "gate": gate_payload,
        "replay": replay_payload,
        "cycle_id": cycle_id,
        "source_signal_id": source_signal_id,
        "execution": execution,
        "account_alias": account_alias,
        "account_key": account_key,
    }


def open_initial_recovery_cycle(
    *,
    symbol: str,
    direction: str,
    entry_price: float,
    recovery_policy: RecoveryPolicy,
    canary_policy: RecoveryExecutionCanaryPolicy,
    account_alias: str,
    account_key: str,
    cycle_id: str,
    source_signal_id: str,
    state_store: Any,
    trading_port: Any,
    strategy: str = "tick_recovery_probe",
    timeframe: str = "TICK",
    extra_metadata: Mapping[str, Any] | None = None,
    status_reason: str | None = None,
) -> dict[str, Any]:
    """Submit or dry-run the initial demo recovery position and record cycle state."""

    created_at = datetime.now(timezone.utc)
    payload = _initial_trade_payload(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        recovery_policy=recovery_policy,
        canary_policy=canary_policy,
        account_key=account_key,
        cycle_id=cycle_id,
        source_signal_id=source_signal_id,
        strategy=strategy,
        timeframe=timeframe,
        extra_metadata=extra_metadata,
    )
    try:
        result = trading_port.dispatch_operation("trade", payload)
    except PreTradeRiskBlockedError as exc:
        assessment = dict(getattr(exc, "assessment", {}) or {})
        reason = pretrade_block_reason(assessment, fallback=str(exc))
        return {
            "status": "blocked",
            "reason": reason,
            "category": "pre_trade_risk",
            "payload": payload,
            "pre_trade_risk": assessment,
            "error_message": str(exc),
            "account_alias": account_alias,
            "account_key": account_key,
        }
    except (
        Exception
    ) as exc:  # noqa: BLE001 - trading port failures are domain blocks here.
        return {
            "status": "blocked",
            "reason": str(exc) or type(exc).__name__,
            "category": "trading_port_failure",
            "payload": payload,
            "error_message": str(exc),
            "account_alias": account_alias,
            "account_key": account_key,
        }
    blocked_snapshot = extract_blocked_pretrade_snapshot(result)
    if blocked_snapshot is not None:
        return {
            "status": "blocked",
            "reason": pretrade_block_reason(
                blocked_snapshot,
                fallback="pre_trade_risk_blocked",
            ),
            "category": "pre_trade_risk",
            "payload": payload,
            "result": result,
            "pre_trade_risk": blocked_snapshot,
            "account_alias": account_alias,
            "account_key": account_key,
        }
    # T4 chaos fix: broker 端非异常失败（margin not enough / requote / 拒单）
    # 必须归类为 trading_port_failure，而不是当成 submitted 静默推进。
    result_status = str((result or {}).get("status") or "").lower()
    accepted = (result or {}).get("accepted")
    if result_status in {"failed", "rejected", "error"} or accepted is False:
        return {
            "status": "blocked",
            "reason": str(
                (result or {}).get("error_message")
                or (result or {}).get("message")
                or result_status
                or "trading_port_dispatch_failed"
            ),
            "category": "trading_port_failure",
            "payload": payload,
            "result": result,
            "error_message": str(
                (result or {}).get("error_message")
                or (result or {}).get("message")
                or ""
            ),
            "account_alias": account_alias,
            "account_key": account_key,
        }
    fill_price = _fill_price_from_result(result, fallback=entry_price)
    cycle = RecoveryCycleState(
        cycle_id=cycle_id,
        account_key=account_key,
        symbol=symbol,
        direction=direction,
        status="open",
        base_volume=recovery_policy.base_volume,
        total_volume=recovery_policy.base_volume,
        step_count=0,
        average_entry_price=fill_price,
        last_entry_price=fill_price,
        started_at=created_at,
        updated_at=created_at,
        last_step_at=created_at,
        strategy=strategy,
        timeframe=timeframe,
        source_signal_id=source_signal_id,
        metadata={
            "canary": True,
            "execution_scope": "recovery_initial",
            "initial_result": (
                result if isinstance(result, dict) else {"result": result}
            ),
            **dict(extra_metadata or {}),
        },
    )
    state_store.record_recovery_cycle_state(
        cycle,
        status_reason=status_reason
        or (
            "canary_initial_dry_run"
            if canary_policy.dry_run
            else "canary_initial_submitted"
        ),
    )
    return {
        "status": "dry_run" if canary_policy.dry_run else "submitted",
        "reason": "initial_dispatched",
        "payload": payload,
        "result": result,
        "cycle": cycle,
        "account_alias": account_alias,
        "account_key": account_key,
    }


def evaluate_current_recovery_step(
    *,
    cycle: RecoveryCycleState,
    tick: Tick,
    recovery_policy: RecoveryPolicy,
    canary_policy: RecoveryExecutionCanaryPolicy,
    trading_port: Any,
    state_store: Any | None = None,
) -> dict[str, Any]:
    """Evaluate one current tick against an open recovery cycle.

    This is the live-cycle canary boundary. It uses the current bid/ask snapshot,
    not replay, and can dispatch a scaling intent only through the recovery
    adapter. With the demo CLI contract the adapter remains dry-run.
    """

    snapshot = RecoveryMarketSnapshot(
        symbol=tick.symbol,
        bid=tick.bid,
        ask=tick.ask,
        time=tick.time,
        time_msc=tick.time_msc,
    )
    controller = BoundedRecoveryController()
    decision = controller.evaluate_next_step(
        recovery_policy,
        cycle,
        snapshot,
        now=tick.time,
    )
    decision_payload = _decision_payload(decision)
    if decision.action != "open_step":
        return {
            "status": decision.action,
            "reason": decision.reason,
            "decision": decision_payload,
        }
    plan = RecoveryExecutionPlan.from_decision(
        cycle=cycle,
        decision=decision,
        created_at=tick.time,
    )
    assert plan.position_scaling_intent is not None
    execution = RecoveryExecutionAdapter(
        trading_port=trading_port
    ).execute_scaling_intent(
        recovery_policy=recovery_policy,
        cycle=cycle,
        intent=plan.position_scaling_intent,
        canary_policy=canary_policy,
    )
    result = {
        "status": execution.get("status", "unknown"),
        "reason": execution.get("reason"),
        "decision": decision_payload,
        "execution": execution,
    }
    if execution.get("status") == "submitted":
        updated_cycle = controller.apply_open_step(cycle, decision, now=tick.time)
        if state_store is not None:
            state_store.record_recovery_cycle_state(
                updated_cycle,
                status_reason="canary_recovery_step_submitted",
            )
        result["updated_cycle"] = _cycle_state_payload(updated_cycle)
    return result


def close_recovery_canary_positions(
    *,
    initial_result: dict[str, Any],
    current_step_result: dict[str, Any],
    cycle_id: str,
    source_signal_id: str,
    trading_port: Any,
    deviation: int,
    cycle: RecoveryCycleState | None = None,
    state_store: Any | None = None,
    actor: str = "recovery_canary",
    reason: str = "demo_recovery_canary_cleanup",
) -> dict[str, Any]:
    """Close every submitted demo canary position owned by a recovery cycle."""

    ticket_scopes = _submitted_canary_ticket_scopes(
        initial_result=initial_result,
        current_step_result=current_step_result,
    )
    if not ticket_scopes:
        return {"status": "skipped", "reason": "no_submitted_canary_tickets"}

    close_results: list[dict[str, Any]] = []
    for item in ticket_scopes:
        scope = item["scope"]
        ticket = int(item["ticket"])
        action_id = f"recovery:{cycle_id}:close_{scope}"
        payload = {
            "ticket": ticket,
            "deviation": int(deviation),
            "comment": "recovery-demo-close",
            "actor": actor,
            "reason": reason,
            "action_id": action_id,
            "request_context": {
                "trace_id": source_signal_id,
                "cycle_id": cycle_id,
                "execution_scope": "recovery_cycle_cleanup",
                "recovery_close_scope": scope,
            },
        }
        try:
            result = trading_port.dispatch_operation("close", payload)
            status = "closed" if _close_result_success(result) else "failed"
            close_results.append(
                {
                    "scope": scope,
                    "ticket": ticket,
                    "status": status,
                    "payload": payload,
                    "result": result,
                }
            )
        except Exception as exc:  # noqa: BLE001 - canary cleanup must surface.
            close_results.append(
                {
                    "scope": scope,
                    "ticket": ticket,
                    "status": "failed",
                    "payload": payload,
                    "error_message": str(exc),
                }
            )

    all_closed = all(item.get("status") == "closed" for item in close_results)
    closed_at = datetime.now(timezone.utc)
    cleanup_cycle = _cleanup_cycle_for_close(
        current_step_result=current_step_result,
        fallback=cycle,
    )
    if all_closed and cleanup_cycle is not None and state_store is not None:
        closed_cycle = replace(
            cleanup_cycle,
            status="closed",
            updated_at=closed_at,
        )
        state_store.record_recovery_cycle_state(
            closed_cycle,
            status_reason="canary_recovery_cycle_cleanup_closed",
            closed_at=closed_at,
            close_price=_last_close_price(close_results),
        )
    return {
        "status": "closed" if all_closed else "failed",
        "reason": "cleanup_dispatched",
        "tickets": [int(item["ticket"]) for item in ticket_scopes],
        "results": close_results,
    }


def close_initial_recovery_cycle(
    *,
    initial_result: dict[str, Any],
    cycle_id: str,
    source_signal_id: str,
    trading_port: Any,
    deviation: int,
    cycle: RecoveryCycleState | None = None,
    state_store: Any | None = None,
    actor: str = "recovery_canary",
    reason: str = "demo_recovery_canary_cleanup",
) -> dict[str, Any]:
    """Close the real demo initial position opened by a recovery canary.

    This is intentionally scoped to the initial canary order. Recovery scaling
    remains dry-run in the demo CLI contract.
    """

    if str(initial_result.get("status") or "").strip().lower() != "submitted":
        return {"status": "skipped", "reason": "initial_not_submitted"}
    ticket = _ticket_from_initial_result(initial_result)
    if ticket is None:
        return {"status": "skipped", "reason": "initial_ticket_missing"}

    action_id = f"recovery:{cycle_id}:close_initial"
    payload = {
        "ticket": ticket,
        "deviation": int(deviation),
        "comment": "recovery-demo-close",
        "actor": actor,
        "reason": reason,
        "action_id": action_id,
        "request_context": {
            "trace_id": source_signal_id,
            "cycle_id": cycle_id,
            "execution_scope": "recovery_initial_cleanup",
        },
    }
    try:
        result = trading_port.dispatch_operation("close", payload)
    except Exception as exc:  # noqa: BLE001 - canary cleanup must report, not hide.
        return {
            "status": "failed",
            "reason": "cleanup_exception",
            "ticket": ticket,
            "payload": payload,
            "error_message": str(exc),
        }
    closed_at = datetime.now(timezone.utc)
    if _close_result_success(result) and cycle is not None and state_store is not None:
        closed_cycle = replace(
            cycle,
            status="closed",
            updated_at=closed_at,
        )
        state_store.record_recovery_cycle_state(
            closed_cycle,
            status_reason="canary_initial_cleanup_closed",
            closed_at=closed_at,
            close_price=_close_price_from_result(result),
        )
    return {
        "status": "closed" if _close_result_success(result) else "failed",
        "reason": "cleanup_dispatched",
        "ticket": ticket,
        "payload": payload,
        "result": result,
    }


def close_dry_run_recovery_cycle(
    *,
    cycle: RecoveryCycleState | None,
    state_store: Any | None,
    status_reason: str = "canary_dry_run_cycle_closed",
) -> dict[str, Any]:
    """Mark a dry-run canary cycle closed after evaluation.

    Dry-run live-cycle canaries need an open cycle object while evaluating the
    recovery step, but they must not remain open in the durable recovery state.
    """

    if cycle is None:
        return {"status": "skipped", "reason": "cycle_missing"}
    if state_store is None:
        return {"status": "skipped", "reason": "state_store_missing"}
    closed_at = datetime.now(timezone.utc)
    closed_cycle = replace(
        cycle,
        status="closed",
        updated_at=closed_at,
    )
    state_store.record_recovery_cycle_state(
        closed_cycle,
        status_reason=status_reason,
        closed_at=closed_at,
        close_price=None,
        realized_pnl=None,
    )
    return {
        "status": "closed",
        "reason": status_reason,
        "cycle_id": cycle.cycle_id,
        "closed_at": closed_at,
    }


def verify_initial_recovery_cleanup(
    *,
    cleanup_result: dict[str, Any],
    cycle: RecoveryCycleState | None,
    cycle_id: str,
    source_signal_id: str,
    state_store: Any | None,
    position_reader: Any,
) -> dict[str, Any]:
    """Verify a failed initial cleanup through a read-only position snapshot."""

    if str(cleanup_result.get("status") or "").strip().lower() == "closed":
        return {"status": "skipped", "reason": "cleanup_already_closed"}

    ticket = _ticket_from_cleanup_result(cleanup_result)
    if ticket is None:
        alert = _cleanup_alert(
            code="recovery_initial_cleanup_ticket_missing",
            message="Recovery initial cleanup failed and no ticket was available for verification.",
            cycle_id=cycle_id,
            source_signal_id=source_signal_id,
            ticket=None,
            cycle=cycle,
            cleanup_result=cleanup_result,
        )
        state_record = _record_cleanup_blocked_state(
            cycle=cycle,
            state_store=state_store,
            alert=alert,
            status_reason="canary_initial_cleanup_ticket_missing",
        )
        return {
            "status": "critical",
            "reason": "cleanup_ticket_missing",
            "position_found": None,
            "alert": alert,
            "state_record": state_record,
        }

    symbol = cycle.symbol if cycle is not None else None
    try:
        positions = _read_positions(position_reader, symbol=symbol)
    except Exception as exc:  # noqa: BLE001 - failed verification is the alert.
        alert = _cleanup_alert(
            code="recovery_initial_cleanup_verification_failed",
            message=f"Recovery initial cleanup failed and MT5 position verification failed: {exc}",
            cycle_id=cycle_id,
            source_signal_id=source_signal_id,
            ticket=ticket,
            cycle=cycle,
            cleanup_result=cleanup_result,
        )
        state_record = _record_cleanup_blocked_state(
            cycle=cycle,
            state_store=state_store,
            alert=alert,
            status_reason="canary_initial_cleanup_verification_failed",
        )
        return {
            "status": "critical",
            "reason": "position_verification_failed",
            "ticket": ticket,
            "position_found": None,
            "alert": alert,
            "state_record": state_record,
        }

    position = _matching_position_by_ticket(positions, ticket)
    if position is None:
        return {
            "status": "resolved_after_verify",
            "reason": "cleanup_failed_but_ticket_absent",
            "ticket": ticket,
            "position_found": False,
            "position_count": len(list(positions)),
        }

    position_payload = _position_payload(position)
    alert = _cleanup_alert(
        code="recovery_initial_cleanup_unresolved",
        message=(
            "Recovery initial cleanup failed and the submitted initial ticket "
            "is still open in MT5."
        ),
        cycle_id=cycle_id,
        source_signal_id=source_signal_id,
        ticket=ticket,
        cycle=cycle,
        cleanup_result=cleanup_result,
        position=position_payload,
    )
    state_record = _record_cleanup_blocked_state(
        cycle=cycle,
        state_store=state_store,
        alert=alert,
        status_reason="canary_initial_cleanup_unresolved",
    )
    return {
        "status": "critical",
        "reason": "cleanup_unresolved",
        "ticket": ticket,
        "position_found": True,
        "position": position_payload,
        "alert": alert,
        "state_record": state_record,
    }


def _first_fill(
    fills: Iterable[RecoveryReplayFill],
    *,
    role: str,
) -> RecoveryReplayFill | None:
    for fill in fills:
        if fill.role == role:
            return fill
    return None


def _ticket_from_initial_result(initial_result: dict[str, Any]) -> int | None:
    result = initial_result.get("result")
    if not isinstance(result, dict):
        return None
    for key in ("ticket", "order", "order_id", "deal", "deal_id"):
        value = result.get(key)
        try:
            ticket = int(value)
        except (TypeError, ValueError):
            continue
        if ticket > 0:
            return ticket
    return None


def _submitted_canary_ticket_scopes(
    *,
    initial_result: dict[str, Any],
    current_step_result: dict[str, Any],
) -> list[dict[str, Any]]:
    tickets: list[dict[str, Any]] = []
    if str(initial_result.get("status") or "").strip().lower() == "submitted":
        ticket = _ticket_from_initial_result(initial_result)
        if ticket is not None:
            tickets.append({"scope": "initial", "ticket": ticket})

    if str(current_step_result.get("status") or "").strip().lower() == "submitted":
        ticket = _ticket_from_current_step_result(current_step_result)
        if ticket is not None:
            step_index = _step_index_from_current_step_result(current_step_result) or 1
            scope = f"step_{step_index}"
            tickets.append({"scope": scope, "ticket": ticket})
    return _dedupe_ticket_scopes(tickets)


def _ticket_from_current_step_result(current_step_result: dict[str, Any]) -> int | None:
    execution = current_step_result.get("execution")
    if isinstance(execution, dict):
        ticket = _ticket_from_nested_payload(execution.get("result"))
        if ticket is not None:
            return ticket
        ticket = _ticket_from_nested_payload(execution)
        if ticket is not None:
            return ticket
    return _ticket_from_nested_payload(current_step_result)


def _ticket_from_nested_payload(payload: Any) -> int | None:
    if isinstance(payload, dict):
        for key in ("ticket", "position", "order", "order_id", "deal", "deal_id"):
            try:
                ticket = int(payload.get(key))
            except (TypeError, ValueError):
                continue
            if ticket > 0:
                return ticket
        for key in ("result", "response", "response_payload"):
            ticket = _ticket_from_nested_payload(payload.get(key))
            if ticket is not None:
                return ticket
    return None


def _step_index_from_current_step_result(
    current_step_result: dict[str, Any]
) -> int | None:
    decision = current_step_result.get("decision")
    if isinstance(decision, dict):
        try:
            value = int(decision.get("step_index"))
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    execution = current_step_result.get("execution")
    if isinstance(execution, dict):
        payload = execution.get("payload")
        if isinstance(payload, dict):
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                try:
                    value = int(metadata.get("recovery_step_index"))
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    return value
    return None


def _dedupe_ticket_scopes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        ticket = int(item["ticket"])
        if ticket in seen:
            continue
        seen.add(ticket)
        deduped.append(item)
    return deduped


def _cleanup_cycle_for_close(
    *,
    current_step_result: dict[str, Any],
    fallback: RecoveryCycleState | None,
) -> RecoveryCycleState | None:
    payload = current_step_result.get("updated_cycle")
    if not isinstance(payload, dict):
        return fallback
    try:
        return RecoveryCycleState(
            cycle_id=str(
                payload.get("cycle_id") or (fallback.cycle_id if fallback else "")
            ),
            account_key=str(
                payload.get("account_key") or (fallback.account_key if fallback else "")
            ),
            symbol=str(payload.get("symbol") or (fallback.symbol if fallback else "")),
            direction=str(
                payload.get("direction") or (fallback.direction if fallback else "")
            ),
            status=str(payload.get("status") or "open"),
            base_volume=float(payload.get("base_volume")),
            total_volume=float(payload.get("total_volume")),
            step_count=int(payload.get("step_count")),
            average_entry_price=float(payload.get("average_entry_price")),
            last_entry_price=float(payload.get("last_entry_price")),
            started_at=_datetime_from_payload(
                payload.get("started_at"), fallback.started_at if fallback else None
            ),
            updated_at=_datetime_from_payload(
                payload.get("updated_at"), fallback.updated_at if fallback else None
            ),
            last_step_at=_datetime_from_payload(
                payload.get("last_step_at"), fallback.last_step_at if fallback else None
            ),
            strategy=str(
                payload.get("strategy") or (fallback.strategy if fallback else "")
            ),
            timeframe=str(
                payload.get("timeframe") or (fallback.timeframe if fallback else "")
            ),
            source_signal_id=payload.get("source_signal_id")
            or (fallback.source_signal_id if fallback else None),
            metadata=dict(payload.get("metadata") or {}),
        )
    except Exception:  # noqa: BLE001 - cleanup should still attempt closure.
        return fallback


def _datetime_from_payload(value: Any, fallback: datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if fallback is not None:
        return fallback
    return datetime.now(timezone.utc)


def _ticket_from_cleanup_result(cleanup_result: dict[str, Any]) -> int | None:
    for payload in (cleanup_result, cleanup_result.get("result")):
        if not isinstance(payload, dict):
            continue
        for key in ("ticket", "position", "order", "order_id", "deal", "deal_id"):
            try:
                ticket = int(payload.get(key))
            except (TypeError, ValueError):
                continue
            if ticket > 0:
                return ticket
    return None


def _last_close_price(close_results: list[dict[str, Any]]) -> float | None:
    for item in reversed(close_results):
        price = _close_price_from_result(item.get("result"))
        if price is not None:
            return price
    return None


def _cycle_state_payload(cycle: RecoveryCycleState) -> dict[str, Any]:
    return {
        "cycle_id": cycle.cycle_id,
        "account_key": cycle.account_key,
        "symbol": cycle.symbol,
        "direction": cycle.direction,
        "status": cycle.status,
        "base_volume": cycle.base_volume,
        "total_volume": cycle.total_volume,
        "step_count": cycle.step_count,
        "average_entry_price": cycle.average_entry_price,
        "last_entry_price": cycle.last_entry_price,
        "started_at": cycle.started_at,
        "updated_at": cycle.updated_at,
        "last_step_at": cycle.last_step_at,
        "strategy": cycle.strategy,
        "timeframe": cycle.timeframe,
        "source_signal_id": cycle.source_signal_id,
        "metadata": dict(cycle.metadata),
    }


def _close_result_success(result: Any) -> bool:
    if not isinstance(result, dict):
        return bool(result)
    status = str(result.get("status") or "").strip().lower()
    if status in {"completed", "success", "closed"}:
        return True
    if result.get("accepted") is False:
        return False
    nested = result.get("result")
    if isinstance(nested, dict) and nested.get("success") is not None:
        return bool(nested.get("success"))
    if result.get("success") is not None:
        return bool(result.get("success"))
    return bool(result.get("accepted"))


def _close_price_from_result(result: Any) -> float | None:
    if not isinstance(result, dict):
        return None
    candidates = [result]
    nested = result.get("result")
    if isinstance(nested, dict):
        candidates.append(nested)
    for payload in candidates:
        for key in ("fill_price", "close_price", "price", "price_current"):
            try:
                value = float(payload.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return None


def _read_positions(position_reader: Any, *, symbol: str | None) -> list[Any]:
    if hasattr(position_reader, "get_positions"):
        return list(position_reader.get_positions(symbol=symbol))
    if callable(position_reader):
        return list(position_reader(symbol=symbol))
    raise TypeError(
        "position_reader must expose get_positions(symbol=...) or be callable"
    )


def _matching_position_by_ticket(positions: Iterable[Any], ticket: int) -> Any | None:
    for position in positions:
        for key in ("ticket", "identifier", "position", "order"):
            try:
                candidate = int(_position_value(position, key))
            except (TypeError, ValueError):
                continue
            if candidate == ticket:
                return position
    return None


def _position_value(position: Any, key: str) -> Any:
    if isinstance(position, dict):
        return position.get(key)
    return getattr(position, key, None)


def _position_payload(position: Any) -> dict[str, Any]:
    if isinstance(position, dict):
        return dict(position)
    if hasattr(position, "_asdict"):
        return dict(position._asdict())
    if hasattr(position, "__dict__"):
        return dict(vars(position))
    payload: dict[str, Any] = {}
    for key in ("ticket", "identifier", "symbol", "volume", "price_open", "type"):
        value = getattr(position, key, None)
        if value is not None:
            payload[key] = value
    return payload


def _cleanup_alert(
    *,
    code: str,
    message: str,
    cycle_id: str,
    source_signal_id: str,
    ticket: int | None,
    cycle: RecoveryCycleState | None,
    cleanup_result: dict[str, Any],
    position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    alert = {
        "severity": "critical",
        "code": code,
        "message": message,
        "cycle_id": cycle_id,
        "source_signal_id": source_signal_id,
        "ticket": ticket,
        "symbol": cycle.symbol if cycle is not None else None,
        "account_key": cycle.account_key if cycle is not None else None,
        "cleanup_status": cleanup_result.get("status"),
        "cleanup_reason": cleanup_result.get("reason"),
    }
    if position is not None:
        alert["position"] = position
    return alert


def _record_cleanup_blocked_state(
    *,
    cycle: RecoveryCycleState | None,
    state_store: Any | None,
    alert: dict[str, Any],
    status_reason: str,
) -> dict[str, Any]:
    if cycle is None or state_store is None:
        return {"recorded": False, "reason": "state_store_unavailable"}
    blocked_at = datetime.now(timezone.utc)
    metadata = dict(cycle.metadata)
    metadata["cleanup_alert"] = dict(alert)
    blocked_cycle = replace(
        cycle,
        status="blocked",
        updated_at=blocked_at,
        metadata=metadata,
    )
    try:
        state_store.record_recovery_cycle_state(
            blocked_cycle,
            status_reason=status_reason,
        )
    except Exception as exc:  # noqa: BLE001 - keep the critical alert visible.
        return {
            "recorded": False,
            "reason": "state_record_failed",
            "error_message": str(exc),
        }
    return {
        "recorded": True,
        "status_reason": status_reason,
    }


def _cycle_from_initial_fill(
    fill: RecoveryReplayFill,
    *,
    account_key: str,
    cycle_id: str,
    source_signal_id: str,
    strategy: str,
    timeframe: str,
    replay: dict[str, Any],
    gate: dict[str, Any],
) -> RecoveryCycleState:
    return RecoveryCycleState(
        cycle_id=cycle_id,
        account_key=account_key,
        symbol=fill.tick.symbol,
        direction=fill.side,
        status="open",
        base_volume=fill.volume,
        total_volume=fill.volume,
        step_count=0,
        average_entry_price=fill.fill_price,
        last_entry_price=fill.fill_price,
        started_at=fill.fill_time,
        updated_at=fill.fill_time,
        last_step_at=fill.fill_time,
        strategy=strategy,
        timeframe=timeframe,
        source_signal_id=source_signal_id,
        metadata={"canary": True, "replay": replay, "gate": gate},
    )


def _intent_from_recovery_fill(
    fill: RecoveryReplayFill,
    *,
    account_key: str,
    cycle_id: str,
    source_signal_id: str,
    strategy: str,
    timeframe: str,
    replay: dict[str, Any],
    gate: dict[str, Any],
) -> PositionScalingIntent:
    return PositionScalingIntent(
        cycle_id=cycle_id,
        account_key=account_key,
        symbol=fill.tick.symbol,
        direction=fill.side,
        strategy=strategy,
        timeframe=timeframe,
        step_index=fill.step_index,
        volume=fill.volume,
        entry_price=fill.fill_price,
        reason="recovery_canary_replay_candidate",
        created_at=fill.fill_time,
        source_signal_id=source_signal_id,
        metadata={
            "canary": True,
            "fill_time_msc": fill.fill_time_msc,
            "replay": replay,
            "gate": gate,
        },
    )


def _initial_trade_payload(
    *,
    symbol: str,
    direction: str,
    entry_price: float,
    recovery_policy: RecoveryPolicy,
    canary_policy: RecoveryExecutionCanaryPolicy,
    account_key: str,
    cycle_id: str,
    source_signal_id: str,
    strategy: str,
    timeframe: str,
    extra_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    request_id = f"recovery:{cycle_id}:initial"
    metadata = {
        "execution_scope": "recovery_initial",
        "recovery_cycle_id": cycle_id,
        "recovery_step_index": 0,
        "source_signal_id": source_signal_id,
        "account_key": account_key,
        "strategy": strategy,
        "timeframe": timeframe,
        "canary": True,
        **dict(extra_metadata or {}),
    }
    payload = {
        "symbol": symbol,
        "volume": recovery_policy.base_volume,
        "side": direction,
        "order_kind": canary_policy.order_kind,
        "price": entry_price,
        "deviation": int(canary_policy.deviation),
        "comment": f"{canary_policy.comment_prefix}:{cycle_id}:initial",
        "magic": int(canary_policy.magic),
        "dry_run": bool(canary_policy.dry_run),
        "request_id": request_id,
        "trace_id": source_signal_id,
        "action_id": request_id,
        "metadata": metadata,
    }
    if str(canary_policy.order_kind or "market").strip().lower() == "market":
        payload["sl"] = _protective_stop_price(
            direction=direction,
            entry_price=entry_price,
            stop_points=float(canary_policy.protective_stop_points or 0.0),
            point=float(recovery_policy.point),
        )
    return payload


def _fill_price_from_result(result: Any, *, fallback: float) -> float:
    if isinstance(result, dict):
        for key in ("fill_price", "price", "requested_price"):
            value = result.get(key)
            if value is None:
                continue
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
    return float(fallback)


def _decision_payload(decision: Any) -> dict[str, Any]:
    return {
        "action": decision.action,
        "reason": decision.reason,
        "step_index": decision.step_index,
        "volume": decision.volume,
        "entry_price": decision.entry_price,
        "exit_price": decision.exit_price,
        "metadata": dict(decision.metadata),
    }


def _protective_stop_price(
    *,
    direction: str,
    entry_price: float,
    stop_points: float,
    point: float,
) -> float:
    distance = stop_points * point
    normalized = str(direction or "").strip().lower()
    if normalized == "buy":
        return round(entry_price - distance, 8)
    if normalized == "sell":
        return round(entry_price + distance, 8)
    raise ValueError("direction must be buy or sell")


def _gate_payload(decision: Any) -> dict[str, Any]:
    return {
        "allowed": bool(decision.allowed),
        "stage": decision.stage,
        "reasons": list(decision.reasons),
        "details": dict(decision.details),
    }


def _replay_payload(report: Any) -> dict[str, Any]:
    return {
        "symbol": report.symbol,
        "direction": report.direction,
        "tick_count": report.tick_count,
        "tradable_tick_count": report.tradable_tick_count,
        "tick_coverage": report.tick_coverage,
        "fill_count": len(report.fills),
        "recovery_fill_count": sum(
            1 for fill in report.fills if fill.role == "recovery"
        ),
        "event_count": len(report.events),
        "blocked_count": report.blocked_count,
        "hold_count": report.hold_count,
        "closed": report.closed,
        "close_reason": report.close_reason,
        "close_price": report.close_price,
        "estimated_pnl": report.estimated_pnl,
        "max_step_count": report.max_step_count,
        "max_total_volume": report.max_total_volume,
    }
