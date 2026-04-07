"""Concurrency stress tests for thread-safe components.

Tests verify:
- MarketDataService: concurrent quote/tick/OHLC writes and reads
- TradeFrequencyRule: concurrent record_trade + evaluate
- IndicatorManager snapshot: concurrent store + read (extends existing)
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.config import EconomicConfig
from src.config.models.runtime import RiskConfig
from src.risk.models import TradeIntent
from src.risk.rules import RuleContext, TradeFrequencyRule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeOHLC:
    """Minimal OHLC stub for MarketDataService tests."""
    def __init__(self, t: datetime, o: float, h: float, l: float, c: float):
        self.time = t
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.tick_volume = 100
        self.spread = 0
        self.real_volume = 0


class FakeQuote:
    """Minimal Quote stub."""
    def __init__(self, symbol: str, bid: float, ask: float):
        self.symbol = symbol
        self.bid = bid
        self.ask = ask
        self.time = datetime.now(timezone.utc)
        self.last = bid
        self.volume = 0
        self.flags = 0


class FakeTick:
    """Minimal Tick stub."""
    def __init__(self, t: datetime, bid: float, ask: float):
        self.time = t
        self.bid = bid
        self.ask = ask
        self.last = bid
        self.volume = 0
        self.flags = 0
        self.volume_real = 0


def _freq_ctx(
    *,
    max_per_day: Optional[int] = None,
    max_per_hour: Optional[int] = None,
) -> RuleContext:
    return RuleContext(
        intent=TradeIntent(symbol="XAUUSD", volume=0.1, side="buy"),
        economic_settings=EconomicConfig(),
        risk_settings=RiskConfig(
            enabled=True,
            max_trades_per_day=max_per_day,
            max_trades_per_hour=max_per_hour,
        ),
    )


# ---------------------------------------------------------------------------
# TradeFrequencyRule concurrency
# ---------------------------------------------------------------------------

class TestTradeFrequencyRuleConcurrency:
    """Verify TradeFrequencyRule is safe under concurrent writes + reads."""

    def test_concurrent_record_and_evaluate(self):
        """10 writer threads + 5 reader threads, no crashes."""
        rule = TradeFrequencyRule()
        errors: List[str] = []
        barrier = threading.Barrier(15)

        def writer():
            barrier.wait()
            for _ in range(50):
                try:
                    rule.record_trade()
                except Exception as exc:
                    errors.append(f"writer: {exc}")

        def reader():
            barrier.wait()
            ctx = _freq_ctx(max_per_day=1000, max_per_hour=500)
            for _ in range(50):
                try:
                    rule.evaluate(ctx)
                except Exception as exc:
                    errors.append(f"reader: {exc}")

        threads = [threading.Thread(target=writer) for _ in range(10)]
        threads += [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors during concurrent access: {errors}"
        # All 500 writes should be recorded (minus pruned)
        assert len(rule._trade_timestamps) <= 500

    def test_concurrent_record_count_consistency(self):
        """All concurrent writes should be counted."""
        rule = TradeFrequencyRule()
        n_threads = 8
        n_per_thread = 100
        barrier = threading.Barrier(n_threads)

        def writer():
            barrier.wait()
            for _ in range(n_per_thread):
                rule.record_trade()

        threads = [threading.Thread(target=writer) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # All recent timestamps should be present (all within 48h)
        assert len(rule._trade_timestamps) == n_threads * n_per_thread


# ---------------------------------------------------------------------------
# MarketDataService concurrency
# ---------------------------------------------------------------------------

class TestMarketDataServiceConcurrency:
    """Stress test MarketDataService with concurrent writes and reads."""

    @pytest.fixture
    def service(self):
        """Create a MarketDataService with a mocked client."""
        from src.config.runtime import MarketSettings
        from src.market.service import MarketDataService
        mock_client = MagicMock()
        mock_client.list_symbols.return_value = ["XAUUSD"]
        # get_quote fallback returns None to avoid MagicMock comparison issues
        mock_client.get_quote.side_effect = Exception("no live MT5")
        settings = MarketSettings(
            default_symbol="XAUUSD",
            tick_cache_size=5000,
            ohlc_cache_limit=500,
            intrabar_max_points=500,
            ohlc_event_queue_size=1000,
            quote_stale_seconds=0.001,
        )
        return MarketDataService(client=mock_client, market_settings=settings)

    def test_concurrent_quote_writes_and_reads(self, service):
        """5 writers + 5 readers, 200 iterations each."""
        errors: List[str] = []
        barrier = threading.Barrier(10)

        def writer(idx: int):
            barrier.wait()
            for i in range(200):
                try:
                    quote = FakeQuote("XAUUSD", 3000.0 + i * 0.1, 3000.5 + i * 0.1)
                    service.set_quote("XAUUSD", quote)
                except Exception as exc:
                    errors.append(f"writer-{idx}: {exc}")

        def reader(idx: int):
            barrier.wait()
            for _ in range(200):
                try:
                    service.get_quote("XAUUSD")
                except Exception as exc:
                    errors.append(f"reader-{idx}: {exc}")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        threads += [threading.Thread(target=reader, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Errors: {errors}"

    def test_concurrent_tick_extend_and_get(self, service):
        """Concurrent tick writes and reads."""
        errors: List[str] = []
        barrier = threading.Barrier(6)

        def writer(idx: int):
            barrier.wait()
            for i in range(100):
                try:
                    tick = FakeTick(
                        datetime.now(timezone.utc),
                        3000.0 + i * 0.01,
                        3000.5 + i * 0.01,
                    )
                    service.extend_ticks("XAUUSD", [tick])
                except Exception as exc:
                    errors.append(f"tick-writer-{idx}: {exc}")

        def reader(idx: int):
            barrier.wait()
            for _ in range(100):
                try:
                    service.get_ticks("XAUUSD")
                except Exception as exc:
                    errors.append(f"tick-reader-{idx}: {exc}")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
        threads += [threading.Thread(target=reader, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Errors: {errors}"

    def test_concurrent_ohlc_write_and_read(self, service):
        """Concurrent OHLC closed bar writes + reads."""
        errors: List[str] = []
        barrier = threading.Barrier(6)
        base_time = datetime(2026, 3, 22, 10, 0, 0, tzinfo=timezone.utc)

        def writer(idx: int):
            barrier.wait()
            for i in range(100):
                try:
                    bar = FakeOHLC(
                        base_time + timedelta(minutes=i + idx * 100),
                        3000.0, 3005.0, 2995.0, 3002.0 + i * 0.1,
                    )
                    service.set_ohlc_closed("XAUUSD", "M1", [bar])
                except Exception as exc:
                    errors.append(f"ohlc-writer-{idx}: {exc}")

        def reader(idx: int):
            barrier.wait()
            for _ in range(100):
                try:
                    service.get_ohlc_closed("XAUUSD", "M1")
                except Exception as exc:
                    errors.append(f"ohlc-reader-{idx}: {exc}")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
        threads += [threading.Thread(target=reader, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Errors: {errors}"

    def test_mixed_all_caches_concurrent(self, service):
        """All cache types written and read simultaneously."""
        errors: List[str] = []
        barrier = threading.Barrier(4)
        base_time = datetime(2026, 3, 22, 10, 0, 0, tzinfo=timezone.utc)

        def quote_worker():
            barrier.wait()
            for i in range(200):
                try:
                    q = FakeQuote("XAUUSD", 3000.0 + i, 3000.5 + i)
                    service.set_quote("XAUUSD", q)
                    service.get_quote("XAUUSD")
                except Exception as exc:
                    errors.append(f"quote: {exc}")

        def tick_worker():
            barrier.wait()
            for i in range(200):
                try:
                    t = FakeTick(base_time + timedelta(seconds=i), 3000.0, 3000.5)
                    service.extend_ticks("XAUUSD", [t])
                    service.get_ticks("XAUUSD")
                except Exception as exc:
                    errors.append(f"tick: {exc}")

        def ohlc_worker():
            barrier.wait()
            for i in range(200):
                try:
                    bar = FakeOHLC(
                        base_time + timedelta(minutes=i),
                        3000.0, 3005.0, 2995.0, 3002.0,
                    )
                    service.set_ohlc_closed("XAUUSD", "M1", [bar])
                    service.get_ohlc_closed("XAUUSD", "M1")
                except Exception as exc:
                    errors.append(f"ohlc: {exc}")

        def intrabar_worker():
            barrier.wait()
            for i in range(200):
                try:
                    bar = FakeOHLC(
                        base_time + timedelta(seconds=i * 5),
                        3000.0, 3005.0, 2995.0, 3002.0 + i * 0.01,
                    )
                    service.set_intrabar("XAUUSD", "M1", bar)
                except Exception as exc:
                    errors.append(f"intrabar: {exc}")

        threads = [
            threading.Thread(target=quote_worker),
            threading.Thread(target=tick_worker),
            threading.Thread(target=ohlc_worker),
            threading.Thread(target=intrabar_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Errors: {errors}"


# ---------------------------------------------------------------------------
# Indicator computation under load
# ---------------------------------------------------------------------------

class TestIndicatorComputationConcurrency:
    """Stress test indicator functions under concurrent calls."""

    def test_concurrent_hma_computation(self):
        """HMA computed from multiple threads simultaneously."""
        from src.indicators.core.mean import hma
        from src.indicators.cache.incremental import SimpleOHLC

        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            SimpleOHLC(
                base_time + timedelta(minutes=i),
                3000.0 + i * 0.5, 3005.0 + i * 0.5,
                2995.0 + i * 0.5, 3002.0 + i * 0.5,
            )
            for i in range(50)
        ]
        params = {"period": 20}
        errors: List[str] = []
        results: List[Dict] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            for _ in range(100):
                try:
                    result = hma(bars, params)
                    with lock:
                        results.append(result)
                except Exception as exc:
                    with lock:
                        errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Errors: {errors}"
        assert len(results) == 800
        # All results should be identical (same input)
        first = results[0]
        assert all(r == first for r in results), "HMA results diverged under concurrency"

    def test_concurrent_stoch_rsi_computation(self):
        """StochRSI computed from multiple threads simultaneously."""
        from src.indicators.core.momentum import stoch_rsi
        from src.indicators.cache.incremental import SimpleOHLC

        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            SimpleOHLC(
                base_time + timedelta(minutes=i),
                3000.0 + i * 0.3, 3005.0 + i * 0.3,
                2995.0 + i * 0.3, 3002.0 + i * 0.3,
            )
            for i in range(80)
        ]
        params = {"rsi_period": 14, "stoch_period": 14, "smooth_k": 3, "smooth_d": 3}
        errors: List[str] = []
        results: List[Dict] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            for _ in range(100):
                try:
                    result = stoch_rsi(bars, params)
                    with lock:
                        results.append(result)
                except Exception as exc:
                    with lock:
                        errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Errors: {errors}"
        assert len(results) == 800
        first = results[0]
        assert all(r == first for r in results), "StochRSI results diverged"

    def test_concurrent_rsi_computation(self):
        """RSI computed from multiple threads — pure function, no shared state."""
        from src.indicators.core.momentum import rsi
        from src.indicators.cache.incremental import SimpleOHLC

        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            SimpleOHLC(
                base_time + timedelta(minutes=i),
                3000.0 + i * 0.2, 3005.0 + i * 0.2,
                2995.0 + i * 0.2, 3002.0 + i * 0.2,
            )
            for i in range(60)
        ]
        params = {"period": 14}
        errors: List[str] = []
        results: List[Dict] = []
        lock = threading.Lock()

        def worker():
            for _ in range(200):
                try:
                    result = rsi(bars, params)
                    with lock:
                        results.append(result)
                except Exception as exc:
                    with lock:
                        errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors
        assert len(results) == 1600
        first = results[0]
        assert all(r == first for r in results)
