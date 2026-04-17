"""Abstract transport interface.

Keeping this abstract lets us slot in email / Slack / console-printer sinks
later without touching the dispatcher, and also lets tests swap in an
in-memory fake transport for end-to-end dispatch tests without mocking HTTP.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True)
class TransportResult:
    ok: bool
    # When ``ok=False``:
    retryable: bool = False
    error: str | None = None
    # When rate-limited by the remote side, the server may tell us how long
    # to wait (Telegram: ``parameters.retry_after``). Honor it instead of the
    # local backoff ladder.
    retry_after_seconds: float | None = None


class NotificationTransport(abc.ABC):
    """Deliver a rendered text message to a chat, reporting an outcome."""

    @abc.abstractmethod
    def send(self, *, chat_id: str, text: str) -> TransportResult:
        raise NotImplementedError
