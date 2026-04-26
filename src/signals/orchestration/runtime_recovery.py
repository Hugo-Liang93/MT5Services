from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..metadata_keys import MetadataKey as MK
from .policy import RuntimeSignalState

if TYPE_CHECKING:
    from .runtime import SignalRuntime

logger = logging.getLogger(__name__)


def restore_state(runtime: "SignalRuntime") -> None:
    limit = max(len(runtime._targets) * 6, 200)
    try:
        rows = runtime.service.recent_signals(scope="all", limit=limit)
    except Exception:
        logger.exception("Failed to restore signal runtime state from repository")
        return

    restored_confirmed: set[tuple[str, str, str]] = set()
    skipped_bad_rows = 0
    for row in rows:
        # P2 回归：单条坏数据（如 generated_at='bad-date'）不应中断整个 restore；
        # 旧实现循环无 try/except 隔离 → ValueError 让 prepare_startup → restore_state
        # 直接挂掉，启动恢复阶段被一条脏数据打断。
        try:
            key = (row.get("symbol"), row.get("timeframe"), row.get("strategy"))
            if key[0] is None or key[1] is None or key[2] is None:
                continue
            metadata = row.get("metadata") or {}
            signal_state = str(metadata.get(MK.SIGNAL_STATE, "")).strip().lower()
            scope = (
                str(row.get("scope") or metadata.get(MK.SCOPE) or "confirmed")
                .strip()
                .lower()
            )
            generated_at_raw = row.get("generated_at") or metadata.get(MK.SNAPSHOT_TIME)
            bar_time_raw = metadata.get(MK.BAR_TIME) or row.get("generated_at")
            if generated_at_raw is None or bar_time_raw is None:
                continue
            generated_at = runtime._parse_event_time(generated_at_raw)
            bar_time = runtime._parse_event_time(bar_time_raw)
            state = runtime._state_by_target.setdefault(key, RuntimeSignalState())

            if scope == "confirmed" and key not in restored_confirmed:
                restored_confirmed.add(key)
                restore_confirmed_state(state, signal_state, generated_at, bar_time)
                continue
        except (ValueError, TypeError, KeyError) as exc:
            # 数据降级（脏时间串、缺字段、类型错）→ 跳过本行，继续后续 rows
            skipped_bad_rows += 1
            logger.warning(
                "restore_state: skipping bad row (%s): %s; row=%r",
                type(exc).__name__,
                exc,
                row,
            )
            continue
    if skipped_bad_rows:
        logger.warning(
            "restore_state: skipped %d bad rows during signal runtime restore",
            skipped_bad_rows,
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
