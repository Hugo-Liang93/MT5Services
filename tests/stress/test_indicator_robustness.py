"""指标系统健壮性和压力测试。"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from src.indicators.core.base import sanitize_result


# ─── NaN/Inf Sanitization ──────────────────────────────────────────────


class TestSanitizeResult:
    """sanitize_result 边界条件测试。"""

    def test_normal_values_unchanged(self):
        result = {"rsi": 45.0, "rsi_d3": -2.1}
        assert sanitize_result(result) == result

    def test_nan_removed(self):
        result = {"rsi": 45.0, "rsi_d3": float("nan")}
        cleaned = sanitize_result(result)
        assert "rsi" in cleaned
        assert "rsi_d3" not in cleaned

    def test_inf_removed(self):
        result = {"adx": float("inf"), "plus_di": 22.0}
        cleaned = sanitize_result(result)
        assert "plus_di" in cleaned
        assert "adx" not in cleaned

    def test_neg_inf_removed(self):
        result = {"cci": float("-inf"), "close": 2650.0}
        cleaned = sanitize_result(result)
        assert "close" in cleaned
        assert "cci" not in cleaned

    def test_all_nan_returns_empty(self):
        result = {"a": float("nan"), "b": float("inf")}
        assert sanitize_result(result) == {}

    def test_empty_input(self):
        assert sanitize_result({}) == {}

    def test_non_numeric_preserved(self):
        result = {"direction": 1.0, "label": "up"}
        cleaned = sanitize_result(result)
        # label 非 float，不会被 isfinite 检查
        assert cleaned["direction"] == 1.0
        assert cleaned["label"] == "up"

    def test_integer_values_preserved(self):
        result = {"count": 14, "value": 0.0}
        assert sanitize_result(result) == result

    def test_zero_value_preserved(self):
        result = {"rsi": 0.0, "delta": 0}
        assert sanitize_result(result) == result


# ─── Preview Snapshot 线程安全 ─────────────────────────────────────────


class TestPreviewSnapshotConcurrency:
    """Preview Snapshot 并发读写安全测试。"""

    def test_concurrent_store_and_read(self):
        """多线程并发写入和读取 preview snapshot 不崩溃。"""
        from src.indicators.query_services.runtime import store_preview_snapshot

        # 模拟 manager 结构
        lock = threading.RLock()
        manager = SimpleNamespace(
            state=SimpleNamespace(
                last_preview_snapshot=OrderedDict(),
                preview_snapshot_max_entries=50,
                results_lock=lock,
            ),
        )

        errors: list[Exception] = []
        stop = threading.Event()

        def writer(thread_id: int):
            for i in range(200):
                if stop.is_set():
                    break
                try:
                    store_preview_snapshot(
                        manager,
                        f"XAUUSD",
                        f"M{thread_id}",
                        datetime(2026, 1, 1, i % 24, tzinfo=timezone.utc),
                        {"rsi14": {"rsi": 30.0 + i}},
                    )
                except Exception as exc:
                    errors.append(exc)
                    stop.set()

        def reader():
            for _ in range(200):
                if stop.is_set():
                    break
                try:
                    with lock:
                        list(manager.state.last_preview_snapshot.items())
                except Exception as exc:
                    errors.append(exc)
                    stop.set()

        threads = [
            threading.Thread(target=writer, args=(i,)) for i in range(5)
        ] + [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent errors: {errors}"


# ─── HTF 指标注入吞吐量 ───────────────────────────────────────────────


class TestHTFInjectionPerformance:
    """验证 HTF 注入不引入显著性能回归。"""

    def test_resolve_htf_1000_iterations(self):
        """1000 次 HTF 解析应在 100ms 内完成。"""
from src.signals.orchestration.runtime import SignalRuntime, SignalTarget

        class DummySource:
            snapshot_listeners = []
            def add_snapshot_listener(self, l): pass
            def remove_snapshot_listener(self, l): pass
            current_trace_id = None

            def get_current_trace_id(self): return self.current_trace_id

            def get_indicator(self, s, tf, name):
                return {"ema": 2650.0} if name == "ema50" else None

        class DummyService:
            soft_regime_enabled = False
            def strategy_capability_catalog(self):
                return [
                    {
                        "name": "s",
                        "valid_scopes": ["confirmed"],
                        "needed_indicators": ["ema50"],
                        "needs_intrabar": False,
                        "needs_htf": True,
                        "regime_affinity": {},
                        "htf_requirements": {"ema50": "H1", "adx14": "H1"},
                    }
                ]
            def strategy_requirements(self, s): return ("ema50",)
            def strategy_scopes(self, s): return ("confirmed",)
            def strategy_affinity_map(self, s): return {}
            def strategy_htf_indicators(self, s):
                return {"H1": ("ema50", "adx14")}

        targets = [
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="s"),
            SignalTarget(symbol="XAUUSD", timeframe="H1", strategy="s"),
        ]
        runtime = SignalRuntime(
            service=DummyService(),
            snapshot_source=DummySource(),
            targets=targets,
        )

        start = time.monotonic()
        for _ in range(1000):
            runtime._resolve_htf_indicators(
                "XAUUSD", "M5", {"H1": ("ema50", "adx14")}
            )
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 100, f"HTF resolve took {elapsed_ms:.1f}ms for 1000 iterations"


# ─── Intrabar 衰减 ────────────────────────────────────────────────────


class TestIntrabarDecay:
    """Intrabar 置信度衰减配置验证。"""

    def test_decay_factor_clamped(self):
        """衰减因子不应大于 1.0。"""
from src.signals.orchestration.runtime import SignalRuntime, SignalTarget

        class DummySource:
            snapshot_listeners = []
            def add_snapshot_listener(self, l): pass
            def remove_snapshot_listener(self, l): pass
            current_trace_id = None

            def get_current_trace_id(self): return self.current_trace_id

        class DummyService:
            soft_regime_enabled = False
            def strategy_capability_catalog(self):
                return [
                    {
                        "name": "s",
                        "valid_scopes": ["confirmed"],
                        "needed_indicators": [],
                        "needs_intrabar": False,
                        "needs_htf": False,
                        "regime_affinity": {},
                        "htf_requirements": {},
                    }
                ]
            def strategy_requirements(self, s): return ()
            def strategy_scopes(self, s): return ("confirmed",)
            def strategy_affinity_map(self, s): return {}
            def strategy_htf_indicators(self, s): return {}

        runtime = SignalRuntime(
            service=DummyService(),
            snapshot_source=DummySource(),
            targets=[SignalTarget("X", "M5", "s")],
            intrabar_confidence_factor=1.5,  # 超过 1.0
        )
        assert runtime._intrabar_confidence_factor <= 1.0
