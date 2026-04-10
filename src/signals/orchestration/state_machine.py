"""Signal state machine transitions — confirmed scope only.

Extracted from SignalRuntime. All functions are pure state operations
on RuntimeSignalState. No thread/queue/IO dependencies.

The intrabar preview/armed state machine (transition_intrabar) was removed —
IntrabarTradeCoordinator handles intrabar trade decisions independently.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from ..metadata_keys import MetadataKey as MK
from .policy import RuntimeSignalState


def build_transition_metadata(
    metadata: Dict[str, Any],
    *,
    signal_state: str,
    state_changed: bool,
    previous_state: str,
) -> Dict[str, Any]:
    enriched = dict(metadata)
    enriched[MK.SIGNAL_STATE] = signal_state
    enriched[MK.STATE_CHANGED] = state_changed
    enriched[MK.PREVIOUS_STATE] = previous_state
    return enriched


def should_emit(
    state: RuntimeSignalState,
    signal_state: str,
    event_time: datetime,
    bar_time: datetime,
    *,
    cooldown_seconds: float,
) -> bool:
    if state.last_emitted_state != signal_state:
        return True
    if state.last_emitted_bar_time != bar_time:
        return True
    if state.last_emitted_at is None:
        return True
    return (event_time - state.last_emitted_at).total_seconds() >= cooldown_seconds


def mark_emitted(
    state: RuntimeSignalState,
    signal_state: str,
    event_time: datetime,
    bar_time: datetime,
) -> None:
    state.last_emitted_state = signal_state
    state.last_emitted_at = event_time
    state.last_emitted_bar_time = bar_time


def snapshot_signature(indicators: Dict[str, Dict[str, float]]) -> int:
    """O(n) hash for snapshot deduplication."""
    return hash(
        frozenset(
            (name, frozenset(payload.items()))
            for name, payload in indicators.items()
            if isinstance(payload, dict)
        )
    )


def is_new_snapshot(
    state: RuntimeSignalState,
    *,
    scope: str,
    event_time: datetime,
    bar_time: datetime,
    indicators: Dict[str, Dict[str, float]],
    dedupe_window_seconds: float,
) -> bool:
    sig = snapshot_signature(indicators)
    if (
        state.last_snapshot_scope == scope
        and state.last_snapshot_bar_time == bar_time
        and state.last_snapshot_signature == sig
    ):
        previous_snapshot_time = state.last_snapshot_time
        if scope == "confirmed":
            return False
        if previous_snapshot_time is not None:
            elapsed = abs((event_time - previous_snapshot_time).total_seconds())
            if elapsed < dedupe_window_seconds:
                return False
    state.last_snapshot_scope = scope
    state.last_snapshot_bar_time = bar_time
    state.last_snapshot_signature = sig
    state.last_snapshot_time = event_time
    return True


def transition_confirmed(
    state: RuntimeSignalState,
    decision_action: str,
    event_time: datetime,
    bar_time: datetime,
    metadata: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Process a confirmed-scope signal decision and return transition metadata.

    Returns ``None`` when no state transition should be emitted.
    """
    previous_state = state.confirmed_state

    if decision_action not in {"buy", "sell"}:
        if previous_state != "idle":
            signal_state = "confirmed_cancelled"
            state.confirmed_state = "idle"
            state.confirmed_bar_time = bar_time
            mark_emitted(state, signal_state, event_time, bar_time)
            return build_transition_metadata(
                metadata,
                signal_state=signal_state,
                state_changed=True,
                previous_state=previous_state,
            )
        state.confirmed_state = "idle"
        state.confirmed_bar_time = bar_time
        return None

    signal_state = f"confirmed_{decision_action}"
    state.confirmed_state = signal_state
    state.confirmed_bar_time = bar_time
    if not should_emit(state, signal_state, event_time, bar_time, cooldown_seconds=0.0):
        return None
    mark_emitted(state, signal_state, event_time, bar_time)
    return build_transition_metadata(
        metadata,
        signal_state=signal_state,
        state_changed=signal_state != previous_state,
        previous_state=previous_state,
    )
