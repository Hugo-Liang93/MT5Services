"""Unit tests for Deduper."""

from __future__ import annotations

import pytest

from src.notifications.dedup import Deduper


class TestDeduper:
    def test_first_observation_allowed(self):
        d = Deduper()
        verdict = d.check("key1", ttl_seconds=60, now=100.0)
        assert verdict.allowed is True
        assert verdict.suppressed_count == 0

    def test_within_ttl_suppressed(self):
        d = Deduper()
        d.check("key1", ttl_seconds=60, now=100.0)
        verdict = d.check("key1", ttl_seconds=60, now=130.0)
        assert verdict.allowed is False
        assert verdict.suppressed_count == 1

    def test_multiple_suppressions_increment(self):
        d = Deduper()
        d.check("key1", ttl_seconds=60, now=100.0)
        for i in range(1, 6):
            verdict = d.check("key1", ttl_seconds=60, now=100.0 + i)
            assert verdict.allowed is False
            assert verdict.suppressed_count == i

    def test_after_ttl_allowed_again(self):
        d = Deduper()
        d.check("key1", ttl_seconds=60, now=100.0)
        d.check("key1", ttl_seconds=60, now=130.0)  # suppressed
        verdict = d.check("key1", ttl_seconds=60, now=165.0)  # 65s later
        assert verdict.allowed is True
        assert verdict.suppressed_count == 0

    def test_suppression_counter_resets_on_allow(self):
        d = Deduper()
        d.check("key1", ttl_seconds=60, now=100.0)
        d.check("key1", ttl_seconds=60, now=130.0)  # +1
        d.check("key1", ttl_seconds=60, now=165.0)  # reset via allow
        verdict = d.check("key1", ttl_seconds=60, now=170.0)
        assert verdict.suppressed_count == 1  # counter restarted

    def test_different_keys_independent(self):
        d = Deduper()
        d.check("a", ttl_seconds=60, now=100.0)
        verdict = d.check("b", ttl_seconds=60, now=100.0)
        assert verdict.allowed is True

    def test_ttl_zero_disables_dedup(self):
        d = Deduper()
        assert d.check("k", ttl_seconds=0, now=100.0).allowed is True
        assert d.check("k", ttl_seconds=0, now=100.0).allowed is True

    def test_negative_ttl_disables_dedup(self):
        d = Deduper()
        assert d.check("k", ttl_seconds=-1, now=100.0).allowed is True

    def test_empty_key_raises(self):
        d = Deduper()
        with pytest.raises(ValueError):
            d.check("", ttl_seconds=60)

    def test_lru_eviction_when_size_exceeded(self):
        d = Deduper(max_entries=3)
        d.check("a", ttl_seconds=1000, now=1.0)
        d.check("b", ttl_seconds=1000, now=2.0)
        d.check("c", ttl_seconds=1000, now=3.0)
        d.check("d", ttl_seconds=1000, now=4.0)  # triggers eviction of "a"
        assert d.size() == 3
        # "a" evicted → re-checking treats as first observation
        verdict = d.check("a", ttl_seconds=1000, now=5.0)
        assert verdict.allowed is True

    def test_clear_expired(self):
        d = Deduper()
        d.check("old", ttl_seconds=60, now=100.0)
        d.check("fresh", ttl_seconds=60, now=200.0)
        evicted = d.clear_expired(now=300.0, ttl_seconds=60)
        assert evicted == 2  # both older than 60s relative to now=300
        assert d.size() == 0

    def test_clear_expired_selective(self):
        d = Deduper()
        d.check("old", ttl_seconds=60, now=100.0)
        d.check("fresh", ttl_seconds=60, now=200.0)
        evicted = d.clear_expired(now=170.0, ttl_seconds=60)
        # "old" at t=100, now=170 → age=70 >= 60 → evicted.
        # "fresh" at t=200 is in the future relative to now=170 → age=-30 < 60 → kept.
        assert evicted == 1

    def test_pending_suppressions_snapshot(self):
        d = Deduper()
        d.check("a", ttl_seconds=60, now=100.0)
        d.check("a", ttl_seconds=60, now=120.0)  # suppressed once
        d.check("a", ttl_seconds=60, now=130.0)  # suppressed twice
        d.check("b", ttl_seconds=60, now=100.0)
        snap = d.pending_suppressions()
        assert snap == {"a": 2}
