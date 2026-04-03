from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from src.utils.common import timeframe_seconds

from .policy import RuntimeSignalState

if TYPE_CHECKING:
    from .runtime import SignalRuntime

logger = logging.getLogger(__name__)


def restore_state(runtime: "SignalRuntime") -> None:
    recent_signals = getattr(runtime.service, "recent_signals", None)
    if not callable(recent_signals):
        return

    limit = max(len(runtime._targets) * 6, 200)
    try:
        rows = recent_signals(scope="all", limit=limit)
    except Exception:
        logger.exception("Failed to restore signal runtime state from repository")
        return

    now = datetime.now(timezone.utc)
    restored_confirmed: set[tuple[str, str, str]] = set()
    restored_preview: set[tuple[str, str, str]] = set()
    restored_confirmed_at: dict[tuple[str, str, str], datetime] = {}
    for row in rows:
        key = (row.get("symbol"), row.get("timeframe"), row.get("strategy"))
        if key[0] is None or key[1] is None or key[2] is None:
            continue
        metadata = row.get("metadata") or {}
        signal_state = str(metadata.get("signal_state", "")).strip().lower()
        scope = (
            str(row.get("scope") or metadata.get("scope") or "confirmed")
            .strip()
            .lower()
        )
        generated_at_raw = row.get("generated_at") or metadata.get("snapshot_time")
        bar_time_raw = metadata.get("bar_time") or row.get("generated_at")
        if generated_at_raw is None or bar_time_raw is None:
            continue
        generated_at = runtime._parse_event_time(generated_at_raw)
        bar_time = runtime._parse_event_time(bar_time_raw)
        state = runtime._state_by_target.setdefault(key, RuntimeSignalState())

        if scope == "confirmed" and key not in restored_confirmed:
            restored_confirmed.add(key)
            restored_confirmed_at[key] = generated_at
            restore_confirmed_state(state, signal_state, generated_at, bar_time)
            continue

        if scope in {"preview", "intrabar"} and key not in restored_preview:
            confirmed_at = restored_confirmed_at.get(key)
            if confirmed_at is not None and generated_at <= confirmed_at:
                continue
            restored_preview.add(key)
            restore_preview_state(
                key[1], state, signal_state, generated_at, bar_time, now
            )


def restore_confirmed_state(
    state: RuntimeSignalState,
    signal_state: str,
    generated_at: datetime,
    bar_time: datetime,
) -> None:
    if signal_state == "confirmed_cancelled":
        state.confirmed_state = "idle"
        state.confirmed_bar_time = bar_time
        state.last_emitted_state = signal_state
        state.last_emitted_at = generated_at
        state.last_emitted_bar_time = bar_time
        return
    if signal_state not in {"confirmed_buy", "confirmed_sell"}:
        return
    state.confirmed_state = signal_state
    state.confirmed_bar_time = bar_time
    state.last_emitted_state = signal_state
    state.last_emitted_at = generated_at
    state.last_emitted_bar_time = bar_time


def restore_preview_state(
    timeframe: str,
    state: RuntimeSignalState,
    signal_state: str,
    generated_at: datetime,
    bar_time: datetime,
    now: datetime,
) -> None:
    if signal_state not in {
        "preview_buy",
        "preview_sell",
        "armed_buy",
        "armed_sell",
    }:
        return
    if now >= bar_time + timedelta(seconds=max(timeframe_seconds(timeframe), 1)):
        return
    action = signal_state.rsplit("_", 1)[-1]
    state.preview_state = signal_state
    state.preview_action = action
    state.preview_bar_time = bar_time
    state.preview_since = generated_at
    state.last_emitted_state = signal_state
    state.last_emitted_at = generated_at
    state.last_emitted_bar_time = bar_time
