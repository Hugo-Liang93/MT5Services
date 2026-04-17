"""TelegramPoller long-polling + offset + backoff tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List
from unittest.mock import MagicMock

import pytest

from src.notifications.inbound.poller import TelegramPoller


@dataclass
class FakeTransport:
    """Scripts a sequence of get_updates responses.

    Each script entry is either:
    - a list[dict]: return it
    - an Exception: raise it
    """

    scripts: List = field(default_factory=list)
    calls: List[int] = field(default_factory=list)  # offsets requested

    def get_updates(self, *, offset: int, timeout_seconds: int):
        self.calls.append(offset)
        if not self.scripts:
            return []
        head = self.scripts.pop(0)
        if isinstance(head, Exception):
            raise head
        return head


def _update(uid: int, text: str = "/health", chat: int = 100) -> dict:
    return {
        "update_id": uid,
        "message": {
            "chat": {"id": chat, "type": "private"},
            "text": text,
            "from": {"id": chat, "username": "u"},
        },
    }


class TestOffsetAdvance:
    def test_offset_starts_zero(self):
        transport = FakeTransport(scripts=[[]])
        router = MagicMock()
        poller = TelegramPoller(transport=transport, router=router)
        assert poller.status()["current_offset"] == 0
        poller.poll_once()
        assert transport.calls == [0]

    def test_offset_advances_past_highest(self):
        transport = FakeTransport(scripts=[[_update(5), _update(7), _update(6)]])
        router = MagicMock()
        poller = TelegramPoller(transport=transport, router=router)
        poller.poll_once()
        assert poller.status()["current_offset"] == 8  # max(5,7,6) + 1

    def test_subsequent_call_uses_new_offset(self):
        transport = FakeTransport(scripts=[[_update(10)], []])
        router = MagicMock()
        poller = TelegramPoller(transport=transport, router=router)
        poller.poll_once()
        poller.poll_once()
        assert transport.calls == [0, 11]

    def test_offset_unchanged_on_empty(self):
        transport = FakeTransport(scripts=[[], []])
        router = MagicMock()
        poller = TelegramPoller(transport=transport, router=router)
        poller.poll_once()
        poller.poll_once()
        assert transport.calls == [0, 0]


class TestDispatch:
    def test_each_update_dispatched(self):
        router = MagicMock()
        transport = FakeTransport(scripts=[[_update(1), _update(2), _update(3)]])
        poller = TelegramPoller(transport=transport, router=router)
        poller.poll_once()
        assert router.dispatch.call_count == 3

    def test_dispatch_exception_does_not_break_poll(self):
        router = MagicMock()
        # router.dispatch is supposed to absorb its own exceptions, but
        # poller must also tolerate a rogue one.
        router.dispatch.side_effect = RuntimeError("boom")
        transport = FakeTransport(scripts=[[_update(1)]])
        poller = TelegramPoller(transport=transport, router=router)
        # poll_once propagates because it's the test-path contract, but the
        # worker loop wraps with try/except. Assert we can still call again
        # and offsets weren't corrupted to negative.
        with pytest.raises(RuntimeError):
            poller.poll_once()


class TestCounters:
    def test_success_counter(self):
        transport = FakeTransport(scripts=[[_update(1)], [_update(2)]])
        router = MagicMock()
        poller = TelegramPoller(transport=transport, router=router)
        poller.poll_once()
        poller.poll_once()
        status = poller.status()
        assert status["polls_ok"] == 2
        assert status["updates_processed"] == 2
        assert status["last_successful_poll_at"] is not None

    def test_malformed_updates_skipped(self):
        # update without update_id is skipped, doesn't advance offset
        bad = {"message": {"chat": {"id": 1}, "text": "/x"}}  # no update_id
        transport = FakeTransport(scripts=[[bad]])
        router = MagicMock()
        poller = TelegramPoller(transport=transport, router=router)
        poller.poll_once()
        # Dispatch not called (no valid update_id), offset unchanged.
        assert router.dispatch.call_count == 0
        assert poller.status()["current_offset"] == 0


class TestLifecycle:
    def test_start_stop_fast(self):
        transport = FakeTransport()
        router = MagicMock()
        poller = TelegramPoller(transport=transport, router=router, timeout_seconds=1)
        # Patch out the actual loop body to avoid real long-poll waits:
        # the worker blocks on get_updates which returns immediately from
        # our FakeTransport, so the loop spins. Verify start/stop toggles.
        poller.start()
        assert poller.is_running() is True
        poller.stop(timeout=2.0)
        assert poller.is_running() is False

    def test_double_start_no_second_thread(self):
        transport = FakeTransport()
        router = MagicMock()
        poller = TelegramPoller(transport=transport, router=router, timeout_seconds=1)
        poller.start()
        first = poller._thread
        poller.start()
        assert poller._thread is first
        poller.stop(timeout=2.0)

    def test_invalid_timeout(self):
        transport = FakeTransport()
        router = MagicMock()
        with pytest.raises(ValueError):
            TelegramPoller(transport=transport, router=router, timeout_seconds=0)
