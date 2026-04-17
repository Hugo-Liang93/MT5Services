"""Unit tests for token-bucket RateLimiter."""

from __future__ import annotations

import pytest

from src.notifications.rate_limit import RateLimiter


class TestRateLimiter:
    def test_first_call_allowed(self):
        lim = RateLimiter(global_per_minute=60, per_chat_per_minute=60)
        verdict = lim.acquire("chat1", now=0.0)
        assert verdict.allowed is True
        assert verdict.blocked_by is None

    def test_per_chat_exhausted(self):
        # 60 per minute → 1/sec → bucket starts at 60 tokens → 60 immediate calls OK
        lim = RateLimiter(global_per_minute=1000, per_chat_per_minute=2)
        lim.acquire("c", now=0.0)
        lim.acquire("c", now=0.0)
        verdict = lim.acquire("c", now=0.0)
        assert verdict.allowed is False
        assert verdict.blocked_by == "per_chat"

    def test_global_exhausted(self):
        lim = RateLimiter(global_per_minute=2, per_chat_per_minute=100)
        lim.acquire("a", now=0.0)
        lim.acquire("b", now=0.0)
        verdict = lim.acquire("c", now=0.0)
        assert verdict.allowed is False
        assert verdict.blocked_by == "global"

    def test_refill_after_time(self):
        # Keep global generous so it's the per-chat refill we're testing.
        lim = RateLimiter(global_per_minute=6000, per_chat_per_minute=60)
        for _ in range(60):
            lim.acquire("c", now=0.0)
        assert lim.acquire("c", now=0.0).allowed is False
        assert lim.acquire("c", now=30.0).allowed is True  # 30s * 1 tok/sec

    def test_separate_chats_independent(self):
        lim = RateLimiter(global_per_minute=1000, per_chat_per_minute=2)
        lim.acquire("chat_a", now=0.0)
        lim.acquire("chat_a", now=0.0)
        # chat_b still has a fresh bucket
        assert lim.acquire("chat_b", now=0.0).allowed is True

    def test_empty_chat_id_raises(self):
        lim = RateLimiter(global_per_minute=60, per_chat_per_minute=60)
        with pytest.raises(ValueError):
            lim.acquire("", now=0.0)

    def test_invalid_limits(self):
        with pytest.raises(ValueError):
            RateLimiter(global_per_minute=0, per_chat_per_minute=60)
        with pytest.raises(ValueError):
            RateLimiter(global_per_minute=60, per_chat_per_minute=-1)

    def test_rejection_does_not_consume_tokens(self):
        # If global rejects, per-chat should not be decremented — otherwise we'd
        # leak tokens on rejection paths and starve valid chats.
        lim = RateLimiter(global_per_minute=1, per_chat_per_minute=100)
        lim.acquire("chat_a", now=0.0)  # consumes global + chat_a
        # chat_b: global empty → rejected
        verdict = lim.acquire("chat_b", now=0.0)
        assert verdict.allowed is False
        assert verdict.blocked_by == "global"
        # chat_b bucket should still have its full initial balance (100 tokens)
        snap = lim.snapshot(now=0.0)
        assert snap.get("chat:chat_b", 0) == 100.0

    def test_snapshot_includes_all_known_chats(self):
        lim = RateLimiter(global_per_minute=60, per_chat_per_minute=60)
        lim.acquire("a", now=0.0)
        lim.acquire("b", now=0.0)
        snap = lim.snapshot(now=0.0)
        assert "global" in snap
        assert "chat:a" in snap
        assert "chat:b" in snap
