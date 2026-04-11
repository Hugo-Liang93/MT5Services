"""State access adapter for indicator query services."""

from __future__ import annotations

from typing import Any


def _to_runtime_state(manager: Any) -> Any:
    state = getattr(manager, "state", None)
    if state is None:
        raise AttributeError(
            "UnifiedIndicatorManager must expose a runtime `state` container for indicator query services."
        )
    return state


def state_get(manager: Any, name: str, default=None):
    return getattr(_to_runtime_state(manager), name, default)


def state_set(manager: Any, name: str, value) -> None:
    setattr(_to_runtime_state(manager), name, value)
