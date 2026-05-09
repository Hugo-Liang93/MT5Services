from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.clients.mt5_market import MT5MarketError
from src.config.runtime import IngestSettings
from src.ingestion.ingestor import BackgroundIngestor
from src.utils.common import ohlc_key


class FakeQuote:
    symbol = "XAUUSD"
    bid = 3000.0
    ask = 3000.2
    last = 3000.1
    volume = 1.0
    time = datetime(2026, 5, 5, tzinfo=timezone.utc)


class FakeTick:
    symbol = "XAUUSD"
    bid = 3000.0
    ask = 3000.2
    last = 3000.1
    price = 3000.1
    volume = 1.0
    time = datetime(2026, 5, 5, tzinfo=timezone.utc)
    time_msc = 0
    flags = 6


class FakeClient:
    def get_quote(self, symbol: str):
        return FakeQuote()

    def get_ticks(self, symbol: str, limit: int, start_time=None):
        return [FakeTick()]


class FakeMarketService:
    def __init__(self) -> None:
        self.client = FakeClient()
        self.market_settings = SimpleNamespace(ohlc_limit=10, ohlc_cache_limit=10)
        self.quotes = []
        self.ticks = []

    def set_quote(self, symbol, quote):
        self.quotes.append((symbol, quote))

    def extend_ticks(self, symbol, ticks):
        self.ticks.append((symbol, list(ticks)))


class FakeStorage:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(quote_flush_enabled=False)
        self.rows = []

    def enqueue(self, kind, row):
        self.rows.append((kind, row))

    def stats(self):
        return {"queues": {}, "threads": {}}


def _ingestor() -> BackgroundIngestor:
    return BackgroundIngestor(
        FakeMarketService(),
        FakeStorage(),
        IngestSettings(connection_timeout=0.01, ingest_ohlc_timeframes=[]),
    )


def test_quote_ingest_uses_mt5_timeout_boundary():
    ingestor = _ingestor()
    calls = []

    def call_with_timeout(label, fn):
        calls.append(label)
        return fn()

    ingestor._call_mt5_with_timeout = call_with_timeout

    ingestor._ingest_quote("XAUUSD")

    assert calls == ["quote:XAUUSD"]


def test_tick_ingest_uses_mt5_timeout_boundary():
    ingestor = _ingestor()
    calls = []

    def call_with_timeout(label, fn):
        calls.append(label)
        return fn()

    ingestor._call_mt5_with_timeout = call_with_timeout

    ingestor._ingest_ticks("XAUUSD")

    assert calls == ["ticks:XAUUSD"]


def test_backfill_ohlc_uses_mt5_timeout_boundary():
    ingestor = _ingestor()
    ingestor.settings.ingest_symbols = ["XAUUSD"]
    ingestor.settings.ingest_ohlc_timeframes = ["M1"]
    ingestor.storage.db = SimpleNamespace()
    key = ohlc_key("XAUUSD", "M1")
    start = datetime(2026, 5, 5, tzinfo=timezone.utc)
    ingestor._backfill_progress[key] = start
    ingestor._backfill_cutoff[key] = start + timedelta(minutes=1)
    calls = []

    def call_with_timeout(label, fn):
        calls.append(label)
        return []

    ingestor._call_mt5_with_timeout = call_with_timeout

    ingestor._backfill_ohlc()

    assert calls == ["backfill_ohlc:XAUUSD:M1"]


def test_mt5_timeout_boundary_raises_market_error_quickly():
    ingestor = _ingestor()

    with pytest.raises(MT5MarketError, match="quote:XAUUSD"):
        ingestor._call_mt5_with_timeout(
            "quote:XAUUSD",
            lambda: time.sleep(0.2),
        )

    ingestor._fetch_executor.shutdown(wait=False)


def test_symbol_cycle_prioritizes_ohlc_before_quote_and_ticks():
    ingestor = _ingestor()
    calls = []

    ingestor._ingest_ohlc = lambda symbol, next_ohlc_at: calls.append("ohlc")
    ingestor._ingest_quote = lambda symbol: calls.append("quote")
    ingestor._ingest_ticks = lambda symbol: calls.append("ticks")

    ingestor._ingest_symbol_cycle("XAUUSD", {})

    assert calls == ["ohlc", "quote", "ticks"]


def test_mt5_timeout_boundary_opens_circuit_when_workers_are_abandoned():
    ingestor = _ingestor()
    called_after_open = False

    for index in range(ingestor._mt5_call_workers()):
        with pytest.raises(MT5MarketError, match=f"quote:{index}"):
            ingestor._call_mt5_with_timeout(
                f"quote:{index}",
                lambda: time.sleep(0.2),
            )

    def should_not_run():
        nonlocal called_after_open
        called_after_open = True

    with pytest.raises(MT5MarketError, match="circuit open"):
        ingestor._call_mt5_with_timeout("quote:after-open", should_not_run)

    assert called_after_open is False
    ingestor._fetch_executor.shutdown(wait=False)


def test_queue_stats_exposes_market_data_health_snapshot():
    ingestor = _ingestor()
    ingestor.settings.ingest_symbols = ["XAUUSD"]
    ingestor.settings.ingest_ohlc_timeframes = ["M1"]
    ingestor._ingest_quote("XAUUSD")
    ingestor._mt5_circuit_open_until = time.monotonic() + 10.0
    ingestor._record_lane_success(
        "quote",
        "XAUUSD",
        market_time=datetime.now(timezone.utc),
    )
    ingestor._record_lane_success(
        "tick",
        "XAUUSD",
        market_time=datetime.now(timezone.utc),
    )
    ingestor._set_last_ohlc_time_if_newer(
        "XAUUSD:M1",
        datetime.now(timezone.utc),
    )

    stats = ingestor.queue_stats()

    health = stats["market_data_health"]
    assert health["status"] == "critical"
    assert health["mt5"]["circuit_open"] is True
    assert health["mt5"]["abandoned_call_count"] == 0
    assert health["lanes"]["quote:XAUUSD"]["last_success_at"] is not None
    assert health["lanes"]["tick:XAUUSD"]["stale"] is False
    assert health["lanes"]["ohlc:XAUUSD:M1"]["stale"] is False
    assert health["freshness"]["stale_count"] == 0


def test_market_data_health_uses_market_time_for_tick_freshness():
    ingestor = _ingestor()
    ingestor.settings.ingest_symbols = ["XAUUSD"]
    ingestor.settings.ingest_ohlc_timeframes = []
    ingestor.settings.max_allowed_delay = 1.0
    old_market_time = datetime.now(timezone.utc) - timedelta(seconds=120)

    ingestor._record_lane_success(
        "tick",
        "XAUUSD",
        market_time=old_market_time,
    )

    health = ingestor.health_snapshot()
    tick = health["lanes"]["tick:XAUUSD"]

    assert tick["last_success_at"] is not None
    assert tick["last_market_time"] is not None
    assert tick["market_age_seconds"] >= 100
    assert tick["stale"] is True
    assert tick["stale_reason"] == "market_time_stale"
    assert tick["severity"] == "warning"
    assert health["freshness"]["warning_stale_count"] == 1
    assert health["freshness"]["critical_stale_count"] == 0
    assert health["status"] == "warning"


def test_required_tick_lane_is_critical_when_market_time_is_stale():
    ingestor = _ingestor()
    ingestor.settings.ingest_symbols = ["XAUUSD"]
    ingestor.settings.ingest_ohlc_timeframes = []
    ingestor.settings.max_allowed_delay = 1.0
    ingestor.set_required_market_data_lanes(["tick:XAUUSD"])
    old_market_time = datetime.now(timezone.utc) - timedelta(seconds=120)

    ingestor._record_lane_success(
        "tick",
        "XAUUSD",
        market_time=old_market_time,
    )

    health = ingestor.health_snapshot()
    tick = health["lanes"]["tick:XAUUSD"]

    assert tick["required"] is True
    assert tick["severity"] == "critical"
    assert tick["stale"] is True
    assert tick["stale_reason"] == "market_time_stale"
    assert health["freshness"]["critical_stale_count"] == 1
    assert health["freshness"]["warning_stale_count"] == 0
    assert health["status"] == "critical"
    assert health["dependency_contract"]["required_lanes"] == ["tick:XAUUSD"]


def test_required_tick_lane_missing_is_critical_during_warmup():
    ingestor = _ingestor()
    ingestor.settings.ingest_symbols = ["XAUUSD"]
    ingestor.settings.ingest_ohlc_timeframes = []
    ingestor.set_required_market_data_lanes(["tick:XAUUSD"])

    health = ingestor.health_snapshot()
    tick = health["lanes"]["tick:XAUUSD"]

    assert tick["required"] is True
    assert tick["status"] == "warming_up"
    assert tick["severity"] == "critical"
    assert health["freshness"]["critical_missing_count"] == 1
    assert health["freshness"]["missing_count"] >= 1
    assert health["status"] == "critical"


def test_ohlc_market_freshness_uses_timeframe_duration_not_poll_interval():
    ingestor = _ingestor()
    ingestor.settings.ingest_symbols = ["XAUUSD"]
    ingestor.settings.ingest_ohlc_timeframes = ["M5"]
    ingestor.settings.ingest_ohlc_intervals = {"M5": 5.0}
    recent_closed_bar = datetime.now(timezone.utc) - timedelta(seconds=180)

    ingestor._record_lane_success(
        "ohlc",
        "XAUUSD",
        "M5",
        market_time=recent_closed_bar,
    )

    health = ingestor.health_snapshot()
    lane = health["lanes"]["ohlc:XAUUSD:M5"]

    assert lane["market_stale_threshold_seconds"] >= 600
    assert lane["market_age_seconds"] < lane["market_stale_threshold_seconds"]
    assert lane["stale"] is False
    assert health["freshness"]["stale_count"] == 0
