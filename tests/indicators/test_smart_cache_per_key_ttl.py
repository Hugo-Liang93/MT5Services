"""SmartCache per-key TTL 测试。"""

from __future__ import annotations

import time

from src.indicators.cache.smart_cache import SmartCache


class TestPerKeyTTL:

    def test_global_ttl_applies_when_no_per_key(self):
        cache = SmartCache(maxsize=10, ttl=1)
        cache.set("k1", "v1")
        assert cache.get("k1") == "v1"
        time.sleep(1.1)
        assert cache.get("k1") is None

    def test_per_key_ttl_overrides_global(self):
        cache = SmartCache(maxsize=10, ttl=10)  # global=10s
        cache.set("short", "val", ttl=1)        # per-key=1s
        cache.set("long", "val", ttl=100)        # per-key=100s
        assert cache.get("short") == "val"
        assert cache.get("long") == "val"
        time.sleep(1.1)
        assert cache.get("short") is None        # per-key expired
        assert cache.get("long") == "val"         # still alive

    def test_cleanup_respects_per_key_ttl(self):
        cache = SmartCache(maxsize=10, ttl=10)
        cache.set("short", "val", ttl=1)
        cache.set("long", "val", ttl=100)
        time.sleep(1.1)
        removed = cache.cleanup_expired()
        assert removed == 1
        assert cache.get("long") == "val"

    def test_stats_respects_per_key_ttl(self):
        cache = SmartCache(maxsize=10, ttl=10)
        cache.set("short", "val", ttl=1)
        cache.set("long", "val", ttl=100)
        time.sleep(1.1)
        stats = cache.get_stats()
        # short expired, only long is valid
        assert stats["size"] == 2  # still in dict until accessed/cleaned
