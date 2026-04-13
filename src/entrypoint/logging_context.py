from __future__ import annotations

import logging
from typing import Any

from src.config.instance_context import (
    get_current_environment,
    get_current_instance_name,
    normalize_environment,
    normalize_instance_name,
)
from src.config.topology import resolve_topology_assignment

_DEFAULT_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_CONTEXT_PREFIX = "[%(environment)s|%(instance)s|%(role)s] "


def resolve_logging_context(
    *,
    instance_name: str | None = None,
    environment: str | None = None,
    role: str | None = None,
) -> dict[str, str]:
    resolved_instance = normalize_instance_name(
        instance_name or get_current_instance_name()
    )
    assignment = (
        resolve_topology_assignment(resolved_instance) if resolved_instance else None
    )
    resolved_environment = normalize_environment(
        environment
        or (assignment.environment if assignment is not None else None)
        or get_current_environment()
    )
    resolved_role = str(
        role
        or (assignment.role if assignment is not None else None)
        or ("supervisor" if resolved_instance == "supervisor" else "main")
    ).strip() or "main"
    return {
        "instance": resolved_instance or "default",
        "environment": resolved_environment or "unknown",
        "role": resolved_role,
    }


def inject_logging_context(base_format: str | None) -> str:
    normalized = str(base_format or "").strip() or _DEFAULT_LOG_FORMAT
    if any(
        token in normalized
        for token in ("%(instance)", "%(environment)", "%(role)")
    ):
        return normalized
    return f"{_CONTEXT_PREFIX}{normalized}"


def install_log_record_context(
    *,
    instance_name: str | None = None,
    environment: str | None = None,
    role: str | None = None,
) -> dict[str, str]:
    context = resolve_logging_context(
        instance_name=instance_name,
        environment=environment,
        role=role,
    )
    previous_factory = logging.getLogRecordFactory()
    base_factory = getattr(previous_factory, "_mt5_base_factory", previous_factory)

    def contextual_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base_factory(*args, **kwargs)
        for key, value in context.items():
            setattr(record, key, value)
        return record

    setattr(contextual_factory, "_mt5_base_factory", base_factory)
    setattr(contextual_factory, "_mt5_context", dict(context))
    logging.setLogRecordFactory(contextual_factory)
    return context
