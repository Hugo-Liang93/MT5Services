"""DataMatrixCache 单元测试（Phase R.1）。

验证契约：
- make_key 确定性（参数顺序无关）
- key 随参数变化（任一字段不同 → 不同 key）
- get/set 正常路径
- TTL 过期 → invalidate
- pickle 损坏 → invalidate
- LRU eviction（超 max_size 删最旧）
- clear / stats
- hash_indicator_set 不依赖运行时状态
"""

from __future__ import annotations

import os
import time

# 模块级 dataclass（pickle 要求 importable）
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

import pytest

from src.research.core.cache import (
    DataMatrixCache,
    _to_hashable,
    default_cache,
    hash_indicator_set,
)


@dataclass(frozen=True)
class _FakeMatrix:
    symbol: str
    n_bars: int
    closes: List[float]


@pytest.fixture
def cache(tmp_path: Path) -> DataMatrixCache:
    return DataMatrixCache(
        cache_dir=tmp_path / "cache",
        max_size_gb=1.0,
        ttl_days=30,
    )


# ── make_key ───────────────────────────────────────────────────────────────


def test_make_key_is_deterministic(cache: DataMatrixCache) -> None:
    """同样参数（不同传入顺序）应得到同 key。"""
    k1 = cache.make_key(symbol="X", timeframe="H1", warmup_bars=200)
    k2 = cache.make_key(warmup_bars=200, timeframe="H1", symbol="X")
    assert k1 == k2


def test_make_key_changes_with_each_param(cache: DataMatrixCache) -> None:
    base = dict(symbol="X", timeframe="H1", warmup_bars=200)
    base_key = cache.make_key(**base)
    for field, new_value in [
        ("symbol", "Y"),
        ("timeframe", "M30"),
        ("warmup_bars", 100),
    ]:
        modified = {**base, field: new_value}
        assert (
            cache.make_key(**modified) != base_key
        ), f"Changing {field} should change key"


def test_make_key_handles_datetime(cache: DataMatrixCache) -> None:
    k1 = cache.make_key(start_time=datetime(2025, 1, 1))
    k2 = cache.make_key(start_time=datetime(2025, 1, 1))
    assert k1 == k2
    k3 = cache.make_key(start_time=datetime(2025, 1, 2))
    assert k3 != k1


def test_make_key_handles_nested_collections(cache: DataMatrixCache) -> None:
    k1 = cache.make_key(forward_horizons=(1, 3, 5), barrier_configs=None)
    k2 = cache.make_key(forward_horizons=(1, 3, 5), barrier_configs=None)
    assert k1 == k2


def test_to_hashable_handles_diverse_types() -> None:
    assert _to_hashable(None) is None
    assert _to_hashable(1) == 1
    assert _to_hashable("x") == "x"
    assert _to_hashable(datetime(2025, 1, 1)) == "2025-01-01T00:00:00"
    assert _to_hashable([1, 2, 3]) == [1, 2, 3]
    assert _to_hashable((1, 2)) == [1, 2]
    assert _to_hashable({"b": 1, "a": 2}) == {"a": 2, "b": 1}


# ── get / set ──────────────────────────────────────────────────────────────


def test_get_returns_none_on_miss(cache: DataMatrixCache) -> None:
    assert cache.get("nonexistent_key") is None


def test_set_then_get_roundtrip(cache: DataMatrixCache) -> None:
    payload = {"symbol": "XAUUSD", "n_bars": 1000, "data": list(range(100))}
    cache.set("roundtrip_key", payload)
    loaded = cache.get("roundtrip_key")
    assert loaded == payload


def test_set_then_get_complex_dataclass_like(cache: DataMatrixCache) -> None:
    """模拟 DataMatrix 这类 frozen dataclass 也能 pickle 往返。"""
    obj = _FakeMatrix("XAUUSD", 3, [1.0, 2.0, 3.0])
    cache.set("dataclass_key", obj)
    loaded = cache.get("dataclass_key")
    assert loaded is not None
    assert loaded.symbol == "XAUUSD"
    assert loaded.closes == [1.0, 2.0, 3.0]


# ── TTL ────────────────────────────────────────────────────────────────────


def test_get_invalidates_after_ttl_expiry(tmp_path: Path) -> None:
    """文件 mtime 比 TTL 早 → 视为过期，删除文件并返回 None。"""
    short_cache = DataMatrixCache(
        cache_dir=tmp_path / "cache_ttl",
        max_size_gb=1.0,
        ttl_days=1,  # 1 天 TTL
    )
    short_cache.set("ttl_key", {"data": 1})
    # 设置 mtime 到 2 天前（已过 1 天 TTL）
    path = short_cache._key_path("ttl_key")
    old_time = time.time() - 2 * 86400
    os.utime(path, (old_time, old_time))
    assert short_cache.get("ttl_key") is None
    assert not path.exists()


def test_ttl_zero_disables_check(tmp_path: Path) -> None:
    """ttl_days=0 表示禁用 TTL，永不过期。"""
    cache = DataMatrixCache(
        cache_dir=tmp_path / "no_ttl",
        max_size_gb=1.0,
        ttl_days=0,
    )
    cache.set("forever", {"data": 1})
    path = cache._key_path("forever")
    # 即使 mtime 设到很久以前
    os.utime(path, (time.time() - 365 * 86400, time.time() - 365 * 86400))
    assert cache.get("forever") == {"data": 1}


# ── 损坏 / 错误处理 ─────────────────────────────────────────────────────────


def test_get_invalidates_corrupted_pickle(tmp_path: Path) -> None:
    """pickle 文件损坏 → 删除并返回 None，不 raise。"""
    cache = DataMatrixCache(cache_dir=tmp_path / "corrupt", max_size_gb=1.0)
    cache.cache_dir.mkdir(parents=True, exist_ok=True)
    bad_path = cache._key_path("corrupted")
    bad_path.write_bytes(b"not a valid pickle")
    assert cache.get("corrupted") is None
    assert not bad_path.exists()


def test_set_failure_does_not_raise(cache: DataMatrixCache) -> None:
    """write 失败应仅 warning，不抛异常。"""
    # 用一个不可 pickle 的对象（lambda）
    cache.set("unpicklable_key", lambda x: x)  # 不抛异常
    # 由于 pickle 失败，文件不应该存在
    assert cache.get("unpicklable_key") is None


# ── LRU eviction ───────────────────────────────────────────────────────────


def test_lru_eviction_drops_oldest(tmp_path: Path) -> None:
    """超 max_size 时按 mtime 升序淘汰。"""
    # 设小 max_size，每次 set 一个 ~10KB 的 list
    cache = DataMatrixCache(
        cache_dir=tmp_path / "lru",
        max_size_gb=0.0001,  # ~100 KB
        ttl_days=30,
    )
    payload = list(range(2000))  # ~16 KB pickle
    for i in range(20):
        cache.set(f"key_{i}", payload)
        # 微小延迟保证 mtime 单调递增
        time.sleep(0.005)
    files = list(cache.cache_dir.glob("datamatrix_*.pkl"))
    # 应该被淘汰一些（具体数量取决于 pickle 大小）
    assert len(files) < 20, f"Expected eviction, but got {len(files)} files"
    # 最后写入的 key 应该还在（最新）
    latest_path = cache._key_path("key_19")
    assert latest_path.exists(), "Latest entry should not be evicted"


def test_eviction_skipped_when_under_limit(tmp_path: Path) -> None:
    cache = DataMatrixCache(
        cache_dir=tmp_path / "no_evict",
        max_size_gb=10.0,  # 充足
    )
    for i in range(5):
        cache.set(f"key_{i}", {"data": i})
    files = list(cache.cache_dir.glob("datamatrix_*.pkl"))
    assert len(files) == 5  # 全部保留


# ── clear / stats ──────────────────────────────────────────────────────────


def test_clear_removes_all_cache_files(cache: DataMatrixCache) -> None:
    for i in range(3):
        cache.set(f"k{i}", {"v": i})
    assert cache.clear() == 3
    assert list(cache.cache_dir.glob("datamatrix_*.pkl")) == []


def test_clear_on_nonexistent_dir_returns_zero(tmp_path: Path) -> None:
    cache = DataMatrixCache(cache_dir=tmp_path / "never_created")
    assert cache.clear() == 0


def test_stats_reports_files_and_size(cache: DataMatrixCache) -> None:
    cache.set("a", {"data": list(range(100))})
    cache.set("b", {"data": list(range(100))})
    s = cache.stats()
    assert s["files"] == 2
    assert s["size_bytes"] > 0
    assert s["oldest_mtime"] is not None


def test_stats_empty_dir(tmp_path: Path) -> None:
    s = DataMatrixCache(cache_dir=tmp_path / "empty").stats()
    assert s == {"files": 0, "size_bytes": 0, "oldest_mtime": None}


# ── default_cache singleton ────────────────────────────────────────────────


def test_default_cache_is_singleton() -> None:
    a = default_cache()
    b = default_cache()
    assert a is b


# ── hash_indicator_set 鲁棒性 ──────────────────────────────────────────────


def test_hash_indicator_set_stable_or_unknown() -> None:
    """正常应返回 8 位 hash；config 加载失败时返回 'unknown'。"""
    h = hash_indicator_set()
    assert isinstance(h, str)
    assert len(h) == 8 or h == "unknown"


def test_hash_indicator_set_falls_back_when_config_unavailable() -> None:
    with patch(
        "src.config.indicator_config.get_global_config_manager",
        side_effect=ImportError("no config"),
    ):
        # ImportError raised inside try → fallback "unknown"
        h = hash_indicator_set()
        assert h == "unknown"
