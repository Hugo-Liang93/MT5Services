"""tests/signals/test_htf_cache.py — HTFStateCache 完整单元测试。

## 覆盖场景
1. HTFDirectionContext dataclass 不可变性、字段正确性
2. 基本 get_htf_direction / get_htf_context 命中、缺失、过期
3. on_signal_event 写入缓存、stable_bars 递增、cancelled 清除、策略过滤
4. max_age 过期处理
5. describe() 命中率统计
6. 多线程读写安全
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

import pytest

from src.signals.strategies.htf_cache import (
    HTFDirectionContext,
    HTFStateCache,
    _DEFAULT_HTF_MAP,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal_event(
    *,
    symbol: str = "XAUUSD",
    timeframe: str = "H1",
    strategy: str = "consensus",
    signal_state: str = "confirmed_buy",
    action: str = "buy",
    confidence: float = 0.80,
    regime: str = "trending",
) -> SimpleNamespace:
    """构建模拟 SignalEvent（SimpleNamespace），与 on_signal_event 所需属性匹配。"""
    return SimpleNamespace(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        action=action,
        confidence=confidence,
        generated_at=datetime.now(timezone.utc),
        metadata={
            "signal_state": signal_state,
            "_regime": regime,
        },
    )


# ---------------------------------------------------------------------------
# TestHTFDirectionContext
# ---------------------------------------------------------------------------

class TestHTFDirectionContext:
    """HTFDirectionContext dataclass 不可变性与字段正确性。"""

    def test_fields_correctly_stored(self):
        """构造后各字段应与传入值一致。"""
        now = datetime.now(timezone.utc)
        ctx = HTFDirectionContext(
            direction="buy",
            confidence=0.85,
            regime="trending",
            stable_bars=3,
            updated_at=now,
        )
        assert ctx.direction == "buy"
        assert ctx.confidence == 0.85
        assert ctx.regime == "trending"
        assert ctx.stable_bars == 3
        assert ctx.updated_at == now

    def test_frozen_dataclass_is_immutable(self):
        """frozen=True 的 dataclass 赋值应抛出 FrozenInstanceError。"""
        ctx = HTFDirectionContext(
            direction="sell",
            confidence=0.70,
            regime="ranging",
            stable_bars=1,
            updated_at=datetime.now(timezone.utc),
        )
        with pytest.raises(AttributeError):
            ctx.direction = "buy"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            ctx.stable_bars = 99  # type: ignore[misc]

    def test_equality_by_value(self):
        """同值的两个实例应相等（dataclass 默认 eq=True）。"""
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        a = HTFDirectionContext("buy", 0.8, "trending", 2, ts)
        b = HTFDirectionContext("buy", 0.8, "trending", 2, ts)
        assert a == b


# ---------------------------------------------------------------------------
# TestHTFStateCacheBasic
# ---------------------------------------------------------------------------

class TestHTFStateCacheBasic:
    """基本读取：get_htf_direction / get_htf_context 命中、缺失。"""

    def test_get_direction_returns_none_when_empty(self):
        """缓存为空时返回 None。"""
        cache = HTFStateCache()
        assert cache.get_htf_direction("XAUUSD", "M1") is None

    def test_get_direction_returns_none_for_unknown_ltf(self):
        """LTF 不在 htf_map 中时返回 None。"""
        cache = HTFStateCache()
        assert cache.get_htf_direction("XAUUSD", "W1") is None

    def test_get_direction_hit_after_signal(self):
        """写入 confirmed_buy 后，LTF 查询应命中对应 HTF 方向。"""
        cache = HTFStateCache()
        # 写入 H1 confirmed_buy → H1 缓存 buy
        event = _make_signal_event(timeframe="H1", signal_state="confirmed_buy")
        cache.on_signal_event(event)
        # M15 的 HTF 是 H1，应命中
        assert cache.get_htf_direction("XAUUSD", "M15") == "buy"

    def test_get_direction_sell(self):
        """confirmed_sell 应缓存 sell 方向。"""
        cache = HTFStateCache()
        event = _make_signal_event(
            timeframe="M5", signal_state="confirmed_sell", action="sell"
        )
        cache.on_signal_event(event)
        # M1 的 HTF 是 M5
        assert cache.get_htf_direction("XAUUSD", "M1") == "sell"

    def test_get_context_returns_full_object(self):
        """get_htf_context 应返回包含 confidence/regime/stable_bars 的完整对象。"""
        cache = HTFStateCache()
        event = _make_signal_event(
            timeframe="H4",
            signal_state="confirmed_buy",
            confidence=0.92,
            regime="breakout",
        )
        cache.on_signal_event(event)
        # H1 的 HTF 是 H4
        ctx = cache.get_htf_context("XAUUSD", "H1")
        assert ctx is not None
        assert ctx.direction == "buy"
        assert ctx.confidence == 0.92
        assert ctx.regime == "breakout"
        assert ctx.stable_bars == 1

    def test_get_context_returns_none_when_no_htf_mapping(self):
        """D1 没有 HTF 映射（默认 map 不含 D1→更高 TF），应返回 None。"""
        cache = HTFStateCache()
        event = _make_signal_event(timeframe="D1", signal_state="confirmed_buy")
        cache.on_signal_event(event)
        # D1 不在默认 htf_map 中（作为 LTF）
        assert cache.get_htf_context("XAUUSD", "D1") is None


# ---------------------------------------------------------------------------
# TestHTFStateCacheSignalListener
# ---------------------------------------------------------------------------

class TestHTFStateCacheSignalListener:
    """on_signal_event 信号监听器：写入、递增、清除、过滤。"""

    def test_confirmed_buy_writes_cache(self):
        """confirmed_buy 事件应写入缓存。"""
        cache = HTFStateCache()
        event = _make_signal_event(timeframe="M15", signal_state="confirmed_buy")
        cache.on_signal_event(event)
        # 直接检查内部缓存
        assert ("XAUUSD", "M15") in cache._cache
        assert cache._cache[("XAUUSD", "M15")].direction == "buy"

    def test_stable_bars_increments_on_same_direction(self):
        """同方向连续 confirmed 信号应递增 stable_bars。"""
        cache = HTFStateCache()
        for _ in range(3):
            event = _make_signal_event(timeframe="H1", signal_state="confirmed_buy")
            cache.on_signal_event(event)
        ctx = cache._cache.get(("XAUUSD", "H1"))
        assert ctx is not None
        assert ctx.stable_bars == 3

    def test_stable_bars_resets_on_direction_change(self):
        """方向切换后 stable_bars 应重置为 1。"""
        cache = HTFStateCache()
        # 2 次 buy
        for _ in range(2):
            cache.on_signal_event(
                _make_signal_event(timeframe="H1", signal_state="confirmed_buy")
            )
        assert cache._cache[("XAUUSD", "H1")].stable_bars == 2
        # 切换到 sell
        cache.on_signal_event(
            _make_signal_event(
                timeframe="H1", signal_state="confirmed_sell", action="sell"
            )
        )
        assert cache._cache[("XAUUSD", "H1")].stable_bars == 1
        assert cache._cache[("XAUUSD", "H1")].direction == "sell"

    def test_confirmed_cancelled_clears_cache(self):
        """confirmed_cancelled 应清除对应缓存条目。"""
        cache = HTFStateCache()
        cache.on_signal_event(
            _make_signal_event(timeframe="H1", signal_state="confirmed_buy")
        )
        assert ("XAUUSD", "H1") in cache._cache

        cache.on_signal_event(
            _make_signal_event(timeframe="H1", signal_state="confirmed_cancelled", action="hold")
        )
        assert ("XAUUSD", "H1") not in cache._cache

    def test_non_confirmed_events_ignored(self):
        """preview_buy / armed_sell 等非 confirmed 事件不写入缓存。"""
        cache = HTFStateCache()
        for state in ("preview_buy", "preview_sell", "armed_buy", "armed_sell"):
            cache.on_signal_event(
                _make_signal_event(timeframe="H1", signal_state=state)
            )
        assert len(cache._cache) == 0

    def test_source_strategies_filter(self):
        """非 source_strategies 中的策略事件应被过滤。"""
        cache = HTFStateCache(source_strategies=frozenset({"trend_vote"}))
        # consensus 不在 source_strategies 中，应被忽略
        cache.on_signal_event(
            _make_signal_event(
                timeframe="H1", strategy="consensus", signal_state="confirmed_buy"
            )
        )
        assert len(cache._cache) == 0

        # trend_vote 在 source_strategies 中，应写入
        cache.on_signal_event(
            _make_signal_event(
                timeframe="H1", strategy="trend_vote", signal_state="confirmed_buy"
            )
        )
        assert ("XAUUSD", "H1") in cache._cache

    def test_multiple_symbols_isolated(self):
        """不同品种的缓存应互相独立。"""
        cache = HTFStateCache()
        cache.on_signal_event(
            _make_signal_event(symbol="XAUUSD", timeframe="H1", signal_state="confirmed_buy")
        )
        cache.on_signal_event(
            _make_signal_event(
                symbol="EURUSD", timeframe="H1",
                signal_state="confirmed_sell", action="sell",
            )
        )
        assert cache._cache[("XAUUSD", "H1")].direction == "buy"
        assert cache._cache[("EURUSD", "H1")].direction == "sell"


# ---------------------------------------------------------------------------
# TestHTFStateCacheExpiration
# ---------------------------------------------------------------------------

class TestHTFStateCacheExpiration:
    """max_age 过期处理。"""

    def test_expired_entry_returns_none(self):
        """超过 max_age 的缓存条目应返回 None。"""
        cache = HTFStateCache(max_age_seconds=2.0)
        cache.on_signal_event(
            _make_signal_event(timeframe="H1", signal_state="confirmed_buy")
        )
        # 手动将 updated_at 回拨到过去
        key = ("XAUUSD", "H1")
        old_ctx = cache._cache[key]
        expired_ctx = HTFDirectionContext(
            direction=old_ctx.direction,
            confidence=old_ctx.confidence,
            regime=old_ctx.regime,
            stable_bars=old_ctx.stable_bars,
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        cache._cache[key] = expired_ctx

        assert cache.get_htf_direction("XAUUSD", "M15") is None
        assert cache.get_htf_context("XAUUSD", "M15") is None

    def test_fresh_entry_not_expired(self):
        """未过期的条目应正常返回。"""
        cache = HTFStateCache(max_age_seconds=3600.0)
        cache.on_signal_event(
            _make_signal_event(timeframe="H1", signal_state="confirmed_sell", action="sell")
        )
        assert cache.get_htf_direction("XAUUSD", "M15") == "sell"

    def test_max_age_floor_is_one_second(self):
        """max_age_seconds < 1 时应被截断为 1 秒。"""
        cache = HTFStateCache(max_age_seconds=0.001)
        assert cache._max_age == timedelta(seconds=1.0)


# ---------------------------------------------------------------------------
# TestHTFStateCacheStats
# ---------------------------------------------------------------------------

class TestHTFStateCacheStats:
    """describe() 命中率统计。"""

    def test_describe_empty_cache(self):
        """空缓存的 describe 应返回空条目和零统计。"""
        cache = HTFStateCache()
        info = cache.describe()
        assert info["entries"] == {}
        assert info["stats"]["hit"] == 0
        assert info["stats"]["miss"] == 0
        assert info["stats"]["expired"] == 0
        assert info["stats"]["total"] == 0
        assert info["stats"]["hit_rate"] is None

    def test_describe_hit_rate_calculation(self):
        """命中率 = hit / (hit + miss + expired)。"""
        cache = HTFStateCache(max_age_seconds=3600.0)
        cache.on_signal_event(
            _make_signal_event(timeframe="H1", signal_state="confirmed_buy")
        )
        # 1 hit: M15 → H1
        cache.get_htf_direction("XAUUSD", "M15")
        # 1 miss: 空缓存品种
        cache.get_htf_direction("EURUSD", "M15")
        # 1 miss: 未知 LTF
        cache.get_htf_direction("XAUUSD", "W1")

        info = cache.describe()
        assert info["stats"]["hit"] == 1
        assert info["stats"]["miss"] == 2
        assert info["stats"]["total"] == 3
        assert info["stats"]["hit_rate"] == pytest.approx(1 / 3, abs=0.001)

    def test_describe_expired_count(self):
        """过期查询应计入 expired 统计。"""
        cache = HTFStateCache(max_age_seconds=2.0)
        cache.on_signal_event(
            _make_signal_event(timeframe="H1", signal_state="confirmed_buy")
        )
        # 手动回拨时间使其过期
        key = ("XAUUSD", "H1")
        old_ctx = cache._cache[key]
        cache._cache[key] = HTFDirectionContext(
            direction=old_ctx.direction,
            confidence=old_ctx.confidence,
            regime=old_ctx.regime,
            stable_bars=old_ctx.stable_bars,
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )

        cache.get_htf_direction("XAUUSD", "M15")
        info = cache.describe()
        assert info["stats"]["expired"] == 1

    def test_describe_entries_content(self):
        """describe() 的 entries 应包含缓存条目的详细信息。"""
        cache = HTFStateCache(max_age_seconds=3600.0)
        cache.on_signal_event(
            _make_signal_event(
                timeframe="H1",
                signal_state="confirmed_buy",
                confidence=0.88,
                regime="trending",
            )
        )
        info = cache.describe()
        entry = info["entries"].get("XAUUSD/H1")
        assert entry is not None
        assert entry["direction"] == "buy"
        assert entry["confidence"] == 0.88
        assert entry["regime"] == "trending"
        assert entry["stable_bars"] == 1
        assert entry["expired"] is False
        assert "updated_at" in entry
        assert "age_seconds" in entry


# ---------------------------------------------------------------------------
# TestHTFStateCacheCustomHTFMap
# ---------------------------------------------------------------------------

class TestHTFStateCacheCustomHTFMap:
    """自定义 htf_map。"""

    def test_custom_htf_map_overrides_default(self):
        """传入自定义 htf_map 后应使用新映射。"""
        custom_map = {"M1": "H1", "M5": "H4"}
        cache = HTFStateCache(htf_map=custom_map)
        cache.on_signal_event(
            _make_signal_event(timeframe="H1", signal_state="confirmed_buy")
        )
        # M1 → H1（自定义），应命中
        assert cache.get_htf_direction("XAUUSD", "M1") == "buy"
        # M15 不在自定义 map 中，返回 None
        assert cache.get_htf_direction("XAUUSD", "M15") is None


# ---------------------------------------------------------------------------
# TestHTFStateCacheAttach
# ---------------------------------------------------------------------------

class TestHTFStateCacheAttach:
    """attach() 将缓存注册为信号监听器。"""

    def test_attach_registers_listener(self):
        """attach 应调用 runtime.add_signal_listener，注册的回调能正常写入缓存。"""
        cache = HTFStateCache()
        listeners: list = []
        mock_runtime = SimpleNamespace(
            add_signal_listener=lambda cb: listeners.append(cb),
        )
        cache.attach(mock_runtime)
        assert len(listeners) == 1
        # 验证注册的回调就是 on_signal_event（通过功能验证）
        event = _make_signal_event(timeframe="H1", signal_state="confirmed_buy")
        listeners[0](event)
        assert cache.get_htf_direction("XAUUSD", "M15") == "buy"


# ---------------------------------------------------------------------------
# TestHTFStateCacheConcurrency
# ---------------------------------------------------------------------------

class TestHTFStateCacheConcurrency:
    """多线程读写安全。"""

    def test_concurrent_writes_no_crash(self):
        """多线程同时写入不应引发异常或丢失数据。"""
        cache = HTFStateCache()
        errors: list[Exception] = []
        n_threads = 8
        n_events_per_thread = 50

        def writer(thread_id: int) -> None:
            try:
                for i in range(n_events_per_thread):
                    direction = "confirmed_buy" if i % 2 == 0 else "confirmed_sell"
                    action = "buy" if i % 2 == 0 else "sell"
                    cache.on_signal_event(
                        _make_signal_event(
                            symbol=f"SYM{thread_id}",
                            timeframe="H1",
                            signal_state=direction,
                            action=action,
                        )
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0
        # 每个线程写入自己的 symbol key
        assert len(cache._cache) == n_threads

    def test_concurrent_read_write_no_crash(self):
        """读写并发：写入线程不断更新，读取线程不断查询，不应异常。"""
        cache = HTFStateCache(max_age_seconds=3600.0)
        errors: list[Exception] = []
        stop_event = threading.Event()

        def writer() -> None:
            try:
                for i in range(200):
                    state = "confirmed_buy" if i % 2 == 0 else "confirmed_sell"
                    action = "buy" if i % 2 == 0 else "sell"
                    cache.on_signal_event(
                        _make_signal_event(
                            timeframe="H1", signal_state=state, action=action,
                        )
                    )
                stop_event.set()
            except Exception as e:
                errors.append(e)
                stop_event.set()

        def reader() -> None:
            try:
                while not stop_event.is_set():
                    cache.get_htf_direction("XAUUSD", "M15")
                    cache.get_htf_context("XAUUSD", "M15")
                    cache.describe()
            except Exception as e:
                errors.append(e)

        writer_thread = threading.Thread(target=writer)
        reader_threads = [threading.Thread(target=reader) for _ in range(4)]

        for t in reader_threads:
            t.start()
        writer_thread.start()

        writer_thread.join(timeout=10)
        stop_event.set()
        for t in reader_threads:
            t.join(timeout=5)

        assert len(errors) == 0

    def test_concurrent_cancelled_during_reads(self):
        """cancelled 和 read 并发：cancelled 清除缓存时读取线程不应异常。"""
        cache = HTFStateCache(max_age_seconds=3600.0)
        errors: list[Exception] = []
        stop_event = threading.Event()

        def flip() -> None:
            """交替写入 confirmed_buy 和 confirmed_cancelled。"""
            try:
                for i in range(200):
                    if i % 2 == 0:
                        cache.on_signal_event(
                            _make_signal_event(timeframe="H1", signal_state="confirmed_buy")
                        )
                    else:
                        cache.on_signal_event(
                            _make_signal_event(
                                timeframe="H1",
                                signal_state="confirmed_cancelled",
                                action="hold",
                            )
                        )
                stop_event.set()
            except Exception as e:
                errors.append(e)
                stop_event.set()

        def reader() -> None:
            try:
                while not stop_event.is_set():
                    result = cache.get_htf_direction("XAUUSD", "M15")
                    # result 可以是 None 或 "buy"，不应是其他值
                    assert result in (None, "buy")
            except Exception as e:
                errors.append(e)

        flip_thread = threading.Thread(target=flip)
        reader_threads = [threading.Thread(target=reader) for _ in range(4)]

        for t in reader_threads:
            t.start()
        flip_thread.start()

        flip_thread.join(timeout=10)
        stop_event.set()
        for t in reader_threads:
            t.join(timeout=5)

        assert len(errors) == 0
