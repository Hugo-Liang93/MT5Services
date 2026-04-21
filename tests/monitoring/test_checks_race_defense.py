"""Monitor race defense 回归测试。

历史教训（2026-04-21 事故遗留）：
  indicator manager 内部有 50+ dict 在 hot path 被并发 mutate，罕见情况下
  `get_performance_stats()` 在 iteration 时命中 writer 改 key 的瞬时窗口，
  抛 `RuntimeError: dictionary changed size during iteration`。
  - 修复前：ERROR 日志污染 errors.log，但该次采集丢失
  - 修复后：一次重试（race 通常瞬时）→ 仍失败则降级为 DEBUG + 返回空/None，
    不影响下一轮采集
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from src.monitoring.health import checks
from src.monitoring.manager import _safe_get_performance_stats


class _FlakyWorker:
    """第一次 get_performance_stats 抛 race，第二次成功。"""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload
        self._call_count = 0

    def get_performance_stats(self) -> Dict[str, Any]:
        self._call_count += 1
        if self._call_count == 1:
            raise RuntimeError("dictionary changed size during iteration")
        return self._payload


class _PersistentRaceWorker:
    """每次调用都抛 race——用来验证 defense 的最终降级行为。"""

    def __init__(self) -> None:
        self.call_count = 0

    def get_performance_stats(self) -> Dict[str, Any]:
        self.call_count += 1
        raise RuntimeError("dictionary changed size during iteration")


class _UnrelatedErrorWorker:
    """抛与 race 无关的 RuntimeError，不应被 defense 吞掉。"""

    def get_performance_stats(self) -> Dict[str, Any]:
        raise RuntimeError("unrelated failure")


class _RecordingMonitor:
    def __init__(self) -> None:
        self.records = []

    def record_metric(self, component, metric, value, extras) -> None:
        self.records.append((component, metric, value, extras))


def test_check_cache_stats_retries_on_race_then_succeeds() -> None:
    worker = _FlakyWorker({"cache_hits": 3, "cache_misses": 1})
    monitor = _RecordingMonitor()
    stats = checks.check_cache_stats(monitor, "indicator", worker)
    assert worker._call_count == 2
    assert stats["cache_hits"] == 3
    assert len(monitor.records) == 1
    assert monitor.records[0][1] == "cache_hit_rate"


def test_check_cache_stats_persistent_race_logs_debug_not_error(caplog) -> None:
    worker = _PersistentRaceWorker()
    monitor = _RecordingMonitor()
    with caplog.at_level(logging.DEBUG, logger="src.monitoring.health.checks"):
        stats = checks.check_cache_stats(monitor, "indicator", worker)
    assert stats == {}
    assert worker.call_count == 2  # 1 初 + 1 重试
    assert not any(
        r.levelno >= logging.WARNING for r in caplog.records
    ), "persistent race 不得产出 ERROR/WARNING 日志"


def test_check_cache_stats_unrelated_error_still_logs(caplog) -> None:
    worker = _UnrelatedErrorWorker()
    monitor = _RecordingMonitor()
    with caplog.at_level(logging.ERROR, logger="src.monitoring.health.checks"):
        stats = checks.check_cache_stats(monitor, "indicator", worker)
    assert stats == {}
    assert any(
        "Failed to check cache stats" in r.message for r in caplog.records
    ), "非 race 错误必须仍然 ERROR 日志"


def test_safe_get_performance_stats_retries_on_race() -> None:
    worker = _FlakyWorker({"success_rate": 99.0})
    result = _safe_get_performance_stats(worker)
    assert result == {"success_rate": 99.0}
    assert worker._call_count == 2


def test_safe_get_performance_stats_persistent_race_returns_none() -> None:
    worker = _PersistentRaceWorker()
    result = _safe_get_performance_stats(worker)
    assert result is None
    assert worker.call_count == 2


def test_safe_get_performance_stats_unrelated_error_raises() -> None:
    with pytest.raises(RuntimeError, match="unrelated"):
        _safe_get_performance_stats(_UnrelatedErrorWorker())
