"""Unified timezone utilities for the entire application.

All modules should use these functions instead of inline
``datetime.now(timezone.utc)`` or ad-hoc timezone conversions.

Design:
- Internal storage and computation always use **UTC**.
- Display (logs, API responses) uses the **display timezone** configured
  in ``app.ini [system] timezone`` (e.g. ``Asia/Shanghai``).
- This module is the *single source of truth* for "what time is it".
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, tzinfo
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

# ── Module-level display timezone ─────────────────────────────
# Set once at startup via ``configure()``, then immutable.
_display_tz: tzinfo = timezone.utc
_display_tz_name: str = "UTC"


def configure(tz_name: str = "UTC") -> None:
    """Set the application-wide display timezone.

    Call this **once** during startup (from ``src.entrypoint.web`` or ``AppBuilder``).
    Subsequent calls are allowed but will log a warning.
    """
    global _display_tz, _display_tz_name
    _display_tz = _load_tz(tz_name)
    _display_tz_name = tz_name
    logger.info("Timezone configured: display=%s", tz_name)


# ── Core helpers ──────────────────────────────────────────────

def utc_now() -> datetime:
    """Return the current time in UTC (timezone-aware)."""
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """Ensure a datetime is UTC. Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso_to_utc(value: str) -> datetime:
    """Parse an ISO-8601 string and normalize to UTC, preserving absolute moment.

    §0w R4：旧反模式 ``datetime.fromisoformat(s).replace(tzinfo=UTC)`` 对
    带偏移的 ISO（如 ``2026-04-26T08:00:00+08:00``）会**直接改时区标签**而不
    做时区换算 → 写库变成 ``2026-04-26T08:00:00+00:00``，绝对时刻偏 8 小时。

    本 helper 强制走 ``to_utc()``：
    - naive 输入按 UTC 解释（无别的信息可参考）
    - aware 输入用 ``astimezone(UTC)`` 保持绝对时刻
    """
    return to_utc(datetime.fromisoformat(str(value)))


def to_display(dt: datetime) -> datetime:
    """Convert a datetime to the configured display timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_display_tz)


def display_now() -> datetime:
    """Return the current time in the display timezone."""
    return datetime.now(_display_tz)


def format_display(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format a datetime in the display timezone."""
    return to_display(dt).strftime(fmt)


def get_display_tz_name() -> str:
    """Return the configured display timezone name."""
    return _display_tz_name


def get_display_tz() -> tzinfo:
    """Return the configured display timezone object."""
    return _display_tz


# ── Logging formatter ─────────────────────────────────────────

class LocalTimeFormatter(logging.Formatter):
    """A logging formatter that converts timestamps to the display timezone.

    Usage in ``src.entrypoint.web``::

        from src.utils.timezone import LocalTimeFormatter
        handler = logging.StreamHandler()
        handler.setFormatter(LocalTimeFormatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        ))
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str = None) -> str:  # type: ignore[override]
        ct = datetime.fromtimestamp(record.created, tz=timezone.utc)
        local_ct = ct.astimezone(_display_tz)
        if datefmt:
            return local_ct.strftime(datefmt)
        return local_ct.strftime("%Y-%m-%d %H:%M:%S")


# ── Internal ──────────────────────────────────────────────────

@lru_cache(maxsize=16)
def _load_tz(name: str) -> tzinfo:
    """Load a timezone by name, with fallback to UTC."""
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except (ImportError, KeyError):
        pass
    try:
        import pytz
        return pytz.timezone(name)
    except (ImportError, Exception):
        pass
    logger.warning("Unknown timezone '%s', falling back to UTC", name)
    return timezone.utc
