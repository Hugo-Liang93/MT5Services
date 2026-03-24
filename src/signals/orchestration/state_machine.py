"""Signal state machine transitions — preview / armed / confirmed.

Extracted from SignalRuntime. All functions are pure state operations
on RuntimeSignalState. No thread/queue/IO dependencies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from .policy import RuntimeSignalState


def build_transition_metadata(
    metadata: Dict[str, Any],
    *,
    signal_state: str,
    state_changed: bool,
    previous_state: str,
) -> Dict[str, Any]:
    enriched = dict(metadata)
    enriched["signal_state"] = signal_state
    enriched["state_changed"] = state_changed
    enriched["previous_state"] = previous_state
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
    preview_state_at_close = state.preview_state
    state.preview_state = "idle"
    state.preview_action = None
    state.preview_since = None
    state.preview_bar_time = None

    if decision_action not in {"buy", "sell"}:
        if previous_state != "idle":
            signal_state = "confirmed_cancelled"
            state.confirmed_state = "idle"
            state.confirmed_bar_time = bar_time
            mark_emitted(state, signal_state, event_time, bar_time)
            result = build_transition_metadata(
                metadata,
                signal_state=signal_state,
                state_changed=True,
                previous_state=previous_state,
            )
            result["preview_state_at_close"] = preview_state_at_close
            return result
        state.confirmed_state = "idle"
        state.confirmed_bar_time = bar_time
        return None

    signal_state = f"confirmed_{decision_action}"
    state.confirmed_state = signal_state
    state.confirmed_bar_time = bar_time
    if not should_emit(state, signal_state, event_time, bar_time, cooldown_seconds=0.0):
        return None
    mark_emitted(state, signal_state, event_time, bar_time)
    result = build_transition_metadata(
        metadata,
        signal_state=signal_state,
        state_changed=signal_state != previous_state,
        previous_state=previous_state,
    )
    result["preview_state_at_close"] = preview_state_at_close
    return result


def transition_intrabar(
    state: RuntimeSignalState,
    decision_action: str,
    confidence: float,
    event_time: datetime,
    bar_time: datetime,
    metadata: Dict[str, Any],
    *,
    min_preview_confidence: float,
    min_preview_bar_progress: float,
    min_preview_stable_seconds: float,
    preview_cooldown_seconds: float,
) -> Optional[Dict[str, Any]]:
    """Process an intrabar-scope signal decision and return transition metadata.

    Returns ``None`` when no state transition should be emitted.
    """
    previous_state = state.preview_state
    if state.preview_bar_time is not None and state.preview_bar_time != bar_time:
        state.preview_state = "idle"
        state.preview_action = None
        state.preview_since = None

    bar_progress = float(metadata.get("bar_progress", 0.0) or 0.0)
    actionable = (
        decision_action in {"buy", "sell"}
        and confidence >= min_preview_confidence
        and bar_progress >= min_preview_bar_progress
    )
    state.preview_bar_time = bar_time

    if not actionable:
        if previous_state == "idle":
            return None
        state.preview_state = "idle"
        state.preview_action = None
        state.preview_since = None
        signal_state = "cancelled"
        if not should_emit(
            state, signal_state, event_time, bar_time,
            cooldown_seconds=preview_cooldown_seconds,
        ):
            return None
        mark_emitted(state, signal_state, event_time, bar_time)
        return build_transition_metadata(
            metadata,
            signal_state=signal_state,
            state_changed=True,
            previous_state=previous_state,
        )

    if state.preview_action != decision_action:
        state.preview_action = decision_action
        state.preview_since = event_time
        state.preview_state = f"preview_{decision_action}"
        signal_state = state.preview_state
        if not should_emit(
            state, signal_state, event_time, bar_time,
            cooldown_seconds=preview_cooldown_seconds,
        ):
            return None
        mark_emitted(state, signal_state, event_time, bar_time)
        return build_transition_metadata(
            metadata,
            signal_state=signal_state,
            state_changed=signal_state != previous_state,
            previous_state=previous_state,
        )

    if state.preview_since is None:
        state.preview_since = event_time
        return None

    stable_seconds = (event_time - state.preview_since).total_seconds()
    if stable_seconds < min_preview_stable_seconds:
        return None

    signal_state = f"armed_{decision_action}"
    if state.preview_state == signal_state:
        return None

    state.preview_state = signal_state
    if not should_emit(
        state, signal_state, event_time, bar_time,
        cooldown_seconds=preview_cooldown_seconds,
    ):
        return None
    mark_emitted(state, signal_state, event_time, bar_time)
    enriched = build_transition_metadata(
        metadata,
        signal_state=signal_state,
        state_changed=signal_state != previous_state,
        previous_state=previous_state,
    )
    enriched["preview_stable_seconds"] = stable_seconds
    return enriched
