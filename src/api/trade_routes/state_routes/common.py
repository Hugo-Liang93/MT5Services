from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi.params import Param


def resolve_param(value: Any) -> Any:
    if isinstance(value, Param):
        default = value.default
        return None if default is ... else default
    return value


def normalize_optional_datetime(value: Optional[datetime]) -> Optional[datetime]:
    value = resolve_param(value)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def iso_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def normalize_optional_string(value: Any) -> Optional[str]:
    value = resolve_param(value)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_int(value: Any, *, default: int) -> int:
    value = resolve_param(value)
    if value is None:
        return default
    return int(value)


def normalize_bool(value: Any, *, default: bool) -> bool:
    value = resolve_param(value)
    if value is None:
        return default
    return bool(value)
