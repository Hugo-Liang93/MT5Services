"""PipelineEventBus 单元测试。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.monitoring.pipeline import (
    PIPELINE_BAR_CLOSED,
    PIPELINE_EXECUTION_BLOCKED,
    PIPELINE_EXECUTION_DECIDED,
    PIPELINE_EXECUTION_FAILED,
    PIPELINE_EXECUTION_SUBMITTED,
    PIPELINE_INDICATOR_COMPUTED,
    PIPELINE_PENDING_ORDER_SUBMITTED,
    PIPELINE_SIGNAL_EVALUATED,
    PIPELINE_SNAPSHOT_PUBLISHED,
    PipelineEvent,
    PipelineEventBus,
)


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def bus() -> PipelineEventBus:
    return PipelineEventBus()


def _make_event(**overrides: object) -> PipelineEvent:
    defaults = {
        "type": PIPELINE_BAR_CLOSED,
        "trace_id": "t-001",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "scope": "confirmed",
        "ts": datetime.now(timezone.utc).isoformat(),
        "payload": {},
    }
    defaults.update(overrides)
    return PipelineEvent(**defaults)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════
# Listener 管理
# ═══════════════════════════════════════════════════════════════


class TestListenerManagement:
    def test_add_and_remove_listener(self, bus: PipelineEventBus) -> None:
        listener = MagicMock()
        assert bus.add_listener(listener) is True
        assert bus.stats()["listeners"] == 1

        bus.remove_listener(listener)
        assert bus.stats()["listeners"] == 0

    def test_add_listener_limit(self) -> None:
        bus = PipelineEventBus(max_listeners=2)
        l1, l2, l3 = MagicMock(), MagicMock(), MagicMock()
        assert bus.add_listener(l1) is True
        assert bus.add_listener(l2) is True
        assert bus.add_listener(l3) is False
        assert bus.stats()["listeners"] == 2

    def test_remove_nonexistent_listener(self, bus: PipelineEventBus) -> None:
        """移除不存在的 listener 不应报错。"""
        bus.remove_listener(MagicMock())
        assert bus.stats()["listeners"] == 0

    def test_same_listener_added_twice(self, bus: PipelineEventBus) -> None:
        listener = MagicMock()
        bus.add_listener(listener)
        bus.add_listener(listener)
        bus.emit(_make_event())
        assert listener.call_count == 2


# ═══════════════════════════════════════════════════════════════
# 事件发送
# ═══════════════════════════════════════════════════════════════


class TestEmit:
    def test_emit_calls_all_listeners(self, bus: PipelineEventBus) -> None:
        l1, l2 = MagicMock(), MagicMock()
        bus.add_listener(l1)
        bus.add_listener(l2)
        event = _make_event()
        bus.emit(event)

        l1.assert_called_once_with(event)
        l2.assert_called_once_with(event)

    def test_emit_increments_counter(self, bus: PipelineEventBus) -> None:
        bus.add_listener(MagicMock())
        bus.emit(_make_event())
        bus.emit(_make_event())
        assert bus.stats()["total_emitted"] == 2

    def test_listener_error_does_not_block_others(
        self, bus: PipelineEventBus
    ) -> None:
        bad = MagicMock(side_effect=RuntimeError("boom"))
        good = MagicMock()
        bus.add_listener(bad)
        bus.add_listener(good)
        bus.emit(_make_event())

        good.assert_called_once()
        assert bus.stats()["total_listener_errors"] == 1

    def test_emit_after_shutdown_is_noop(self, bus: PipelineEventBus) -> None:
        listener = MagicMock()
        bus.add_listener(listener)
        bus.shutdown()
        bus.emit(_make_event())
        listener.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 便利 emit 函数
# ═══════════════════════════════════════════════════════════════


class TestConvenienceEmitters:
    def test_emit_bar_closed(self, bus: PipelineEventBus) -> None:
        received: list[PipelineEvent] = []
        bus.add_listener(received.append)

        bar_time = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        bus.emit_bar_closed("t1", "XAUUSD", "M5", "confirmed", bar_time)

        assert len(received) == 1
        ev = received[0]
        assert ev.type == PIPELINE_BAR_CLOSED
        assert ev.trace_id == "t1"
        assert ev.payload["bar_time"] == bar_time.isoformat()
        assert "ohlc" not in ev.payload

    def test_emit_bar_closed_with_ohlc(self, bus: PipelineEventBus) -> None:
        received: list[PipelineEvent] = []
        bus.add_listener(received.append)

        ohlc = {"open": 2000.0, "high": 2010.0, "low": 1995.0, "close": 2005.0}
        bar_time = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        bus.emit_bar_closed("t2", "XAUUSD", "M1", "intrabar", bar_time, ohlc=ohlc)

        assert received[0].payload["ohlc"] == ohlc

    def test_emit_indicator_computed(self, bus: PipelineEventBus) -> None:
        received: list[PipelineEvent] = []
        bus.add_listener(received.append)

        bus.emit_indicator_computed(
            "t3", "XAUUSD", "M5", "confirmed", 12.345, 8,
            indicator_names=["rsi14", "atr14"],
        )

        ev = received[0]
        assert ev.type == PIPELINE_INDICATOR_COMPUTED
        assert ev.payload["compute_time_ms"] == 12.35  # rounded
        assert ev.payload["indicator_count"] == 8
        assert ev.payload["indicator_names"] == ["rsi14", "atr14"]

    def test_emit_indicator_computed_no_names(
        self, bus: PipelineEventBus
    ) -> None:
        received: list[PipelineEvent] = []
        bus.add_listener(received.append)
        bus.emit_indicator_computed("t4", "XAUUSD", "M5", "confirmed", 5.0, 3)
        assert "indicator_names" not in received[0].payload

    def test_emit_snapshot_published(self, bus: PipelineEventBus) -> None:
        received: list[PipelineEvent] = []
        bus.add_listener(received.append)

        bar_time = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        indicators = {"rsi14": {"rsi": 55.0}}
        bus.emit_snapshot_published(
            "t5", "XAUUSD", "M5", "confirmed", 5, bar_time, indicators=indicators
        )

        ev = received[0]
        assert ev.type == PIPELINE_SNAPSHOT_PUBLISHED
        assert ev.payload["indicator_count"] == 5
        assert ev.payload["indicators"] == indicators

    def test_emit_snapshot_published_no_indicators(
        self, bus: PipelineEventBus
    ) -> None:
        received: list[PipelineEvent] = []
        bus.add_listener(received.append)
        bar_time = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        bus.emit_snapshot_published("t6", "XAUUSD", "M5", "confirmed", 3, bar_time)
        assert "indicators" not in received[0].payload

    def test_emit_signal_evaluated(self, bus: PipelineEventBus) -> None:
        received: list[PipelineEvent] = []
        bus.add_listener(received.append)

        bus.emit_signal_evaluated(
            "t7", "XAUUSD", "M5", "confirmed",
            "rsi_reversion", "buy", 0.87654, "confirmed_buy",
        )

        ev = received[0]
        assert ev.type == PIPELINE_SIGNAL_EVALUATED
        assert ev.payload["strategy"] == "rsi_reversion"
        assert ev.payload["direction"] == "buy"
        assert ev.payload["confidence"] == 0.8765  # rounded to 4 dp
        assert ev.payload["signal_state"] == "confirmed_buy"

    def test_emit_execution_events(self, bus: PipelineEventBus) -> None:
        received: list[PipelineEvent] = []
        bus.add_listener(received.append)

        bus.emit_execution_decided(
            "t8", "XAUUSD", "M5", "confirmed",
            strategy="trendline", direction="buy", order_kind="market",
        )
        bus.emit_execution_blocked(
            "t8", "XAUUSD", "M5", "confirmed",
            strategy="trendline", direction="buy", reason="after_eod_block", category="eod_guard",
        )
        bus.emit_execution_submitted(
            "t8", "XAUUSD", "M5", "confirmed",
            strategy="trendline", direction="buy", order_kind="market", request_id="sig-1", ticket=1001,
        )
        bus.emit_pending_order_submitted(
            "t8", "XAUUSD", "M5", "confirmed",
            strategy="trendline", direction="buy", order_kind="limit", request_id="sig-2", ticket=1002,
        )
        bus.emit_execution_failed(
            "t8", "XAUUSD", "M5", "confirmed",
            strategy="trendline", direction="buy", order_kind="market", reason="network_error", category="dispatch",
        )

        assert [item.type for item in received] == [
            PIPELINE_EXECUTION_DECIDED,
            PIPELINE_EXECUTION_BLOCKED,
            PIPELINE_EXECUTION_SUBMITTED,
            PIPELINE_PENDING_ORDER_SUBMITTED,
            PIPELINE_EXECUTION_FAILED,
        ]
        assert received[1].payload["reason"] == "after_eod_block"
        assert received[2].payload["ticket"] == 1001
        assert received[3].payload["order_kind"] == "limit"
        assert received[4].payload["category"] == "dispatch"


# ═══════════════════════════════════════════════════════════════
# Shutdown
# ═══════════════════════════════════════════════════════════════


class TestShutdown:
    def test_shutdown_clears_listeners(self, bus: PipelineEventBus) -> None:
        bus.add_listener(MagicMock())
        bus.add_listener(MagicMock())
        bus.shutdown()
        assert bus.stats()["listeners"] == 0

    def test_shutdown_prevents_further_emit(
        self, bus: PipelineEventBus
    ) -> None:
        bus.shutdown()
        bus.emit(_make_event())
        assert bus.stats()["total_emitted"] == 0


# ═══════════════════════════════════════════════════════════════
# Stats
# ═══════════════════════════════════════════════════════════════


class TestStats:
    def test_stats_initial(self, bus: PipelineEventBus) -> None:
        s = bus.stats()
        assert s == {
            "listeners": 0,
            "total_emitted": 0,
            "total_listener_errors": 0,
        }

    def test_stats_after_activity(self, bus: PipelineEventBus) -> None:
        bad = MagicMock(side_effect=ValueError)
        bus.add_listener(bad)
        bus.emit(_make_event())
        bus.emit(_make_event())
        s = bus.stats()
        assert s["total_emitted"] == 2
        assert s["total_listener_errors"] == 2
        assert s["listeners"] == 1


# ═══════════════════════════════════════════════════════════════
# PipelineEvent (frozen dataclass)
# ═══════════════════════════════════════════════════════════════


class TestPipelineEvent:
    def test_frozen(self) -> None:
        event = _make_event()
        with pytest.raises(AttributeError):
            event.type = "other"  # type: ignore[misc]

    def test_default_payload(self) -> None:
        event = PipelineEvent(
            type="test", trace_id="t", symbol="X", timeframe="M1",
            scope="confirmed", ts="2025-01-01T00:00:00",
        )
        assert event.payload == {}


# ═══════════════════════════════════════════════════════════════
# 线程安全
# ═══════════════════════════════════════════════════════════════


class TestConcurrency:
    def test_concurrent_emit(self, bus: PipelineEventBus) -> None:
        """多线程并发 emit 不应报错。"""
        counter = {"n": 0}
        lock = threading.Lock()

        def counting_listener(ev: PipelineEvent) -> None:
            with lock:
                counter["n"] += 1

        bus.add_listener(counting_listener)

        threads = []
        for i in range(10):
            t = threading.Thread(
                target=lambda idx=i: bus.emit(
                    _make_event(trace_id=f"t-{idx}")
                )
            )
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        assert counter["n"] == 10
        assert bus.stats()["total_emitted"] == 10
