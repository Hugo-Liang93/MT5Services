from __future__ import annotations

from enum import Enum


class RuntimeTaskState(str, Enum):
    RUNNING = "running"
    IDLE = "idle"
    READY = "ready"
    FAILED = "failed"
    COMPLETED = "completed"
    DISABLED = "disabled"
    OK = "ok"
    PARTIAL = "partial"
    ERROR = "error"
    STOPPED = "stopped"


RUNTIME_TASK_STATUS_CHECK_CONSTRAINT = "runtime_task_status_state_check"


ALLOWED_RUNTIME_TASK_STATES: tuple[str, ...] = tuple(
    state.value for state in RuntimeTaskState
)


def runtime_task_states_sql_literals() -> str:
    return ", ".join(f"'{state}'" for state in ALLOWED_RUNTIME_TASK_STATES)


__all__ = [
    "ALLOWED_RUNTIME_TASK_STATES",
    "RUNTIME_TASK_STATUS_CHECK_CONSTRAINT",
    "RuntimeTaskState",
    "runtime_task_states_sql_literals",
]
