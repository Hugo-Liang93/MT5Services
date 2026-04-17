"""Telegram notification module (Phase 1 skeleton).

Public entry points surface as they get built out. Phase 1 covers:
- Core event envelope (`NotificationEvent`, `Severity`)
- Template loading + rendering

Later phases will add dispatcher, transport, inbound poller, and DI wiring.
"""

from src.notifications.events import (
    NotificationEvent,
    Severity,
    build_dedup_key,
    severity_from_str,
)

__all__ = [
    "NotificationEvent",
    "Severity",
    "build_dedup_key",
    "severity_from_str",
]
