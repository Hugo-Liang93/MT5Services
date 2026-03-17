from __future__ import annotations

import queue
import sqlite3
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.clients.mt5_market import MT5MarketClient
from src.clients.mt5_market import Tick
from src.clients.base import MT5BaseClient
from src.core.market_service import MarketDataService
from src.persistence.validator import DataValidator
from src.utils.event_store import LocalEventStore


def test_event_store_marks_event_permanently_failed_after_three_retries(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    event_store = LocalEventStore(str(db_path))
    bar_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    event_store.publish_event("XAUUSD", "M1", bar_time)

    for _ in range(3):
        assert event_store.mark_event_failed("XAUUSD", "M1", bar_time, "boom")

    with sqlite3.connect(db_path) as conn:
        processed, retry_count = conn.execute(
            "SELECT processed, retry_count FROM ohlc_events WHERE symbol=? AND timeframe=? AND bar_time=?",
            ("XAUUSD", "M1", bar_time.isoformat()),
        ).fetchone()

    assert processed == 3
    assert retry_count == 3


def test_event_store_tracks_skipped_outcomes(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    event_store = LocalEventStore(str(db_path))
    bar_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    event_store.publish_event("XAUUSD", "M1", bar_time)
    assert event_store.get_next_event() == ("XAUUSD", "M1", bar_time)
    assert event_store.mark_event_skipped("XAUUSD", "M1", bar_time, "insufficient_history")

    stats = event_store.get_stats()

    assert stats["completed"] == 0
    assert stats["skipped"] == 1
    assert stats["retrying"] == 0
    assert stats["outcome_counts"]["skipped_insufficient_history"] == 1
    assert stats["recent_skips"][0]["outcome"] == "skipped_insufficient_history"
    assert stats["recent_errors"] == []
    assert stats["recent_retryable_errors"] == []


def test_event_store_separates_retrying_and_permanent_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    event_store = LocalEventStore(str(db_path))
    retrying_bar_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    failed_bar_time = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)

    event_store.publish_event("XAUUSD", "M1", retrying_bar_time)
    assert event_store.mark_event_failed("XAUUSD", "M1", retrying_bar_time, "retry me")

    event_store.publish_event("XAUUSD", "M1", failed_bar_time)
    for _ in range(3):
        assert event_store.mark_event_failed("XAUUSD", "M1", failed_bar_time, "give up")

    stats = event_store.get_stats()

    assert stats["retrying"] == 1
    assert stats["failed"] == 1
    assert stats["recent_retryable_errors"][0]["error_message"] == "retry me"
    assert stats["recent_errors"][0]["error_message"] == "give up"


def test_mt5_market_client_prefers_millisecond_tick_timestamp() -> None:
    client = object.__new__(MT5MarketClient)
    client._get_field = lambda obj, name, default=None: getattr(obj, name, default)
    client._to_tz = lambda dt: dt
    client._market_time_offset_seconds = None
    client.settings = SimpleNamespace(server_time_offset_hours=None)

    tick = SimpleNamespace(time=1704067200, time_msc=1704067200123)

    ts = client._tick_timestamp(tick)

    assert ts == datetime.fromtimestamp(1704067200.123, tz=timezone.utc)
    assert ts.microsecond == 123000


def test_mt5_market_client_applies_configured_server_time_offset() -> None:
    client = object.__new__(MT5MarketClient)
    client.settings = SimpleNamespace(server_time_offset_hours=3)
    client.tz = ZoneInfo("UTC")
    client._market_time_offset_seconds = 3 * 3600

    normalized = client._market_time_from_seconds(1773686137)

    assert normalized == datetime(2026, 3, 16, 15, 35, 37, tzinfo=timezone.utc)


def test_mt5_market_client_converts_ohlc_request_start_to_market_time() -> None:
    client = object.__new__(MT5MarketClient)
    client.settings = SimpleNamespace(server_time_offset_hours=3)
    client._market_time_offset_seconds = 3 * 3600
    client.metrics = SimpleNamespace(record=lambda *args, **kwargs: None)
    client.connect = lambda: None
    client._timeframe_to_mt5 = lambda timeframe: timeframe

    captured = {}
    with patch("src.clients.mt5_market.mt5.copy_rates_from", autospec=True) as copy_rates_from:
        copy_rates_from.side_effect = lambda symbol, tf, start, limit: captured.update(
            {"symbol": symbol, "tf": tf, "start": start, "limit": limit}
        ) or []

        client.get_ohlc_from(
            "XAUUSD",
            "M1",
            datetime(2026, 3, 16, 12, 45, tzinfo=timezone.utc),
            10,
        )

    assert captured["start"] == datetime(2026, 3, 16, 15, 45, tzinfo=timezone.utc)


def test_mt5_base_client_shutdown_resets_inferred_market_time_offset() -> None:
    client = object.__new__(MT5BaseClient)
    client._connected = True
    client._configured_market_time_offset_seconds = None
    client._market_time_offset_seconds = 3 * 3600

    with patch("src.clients.base.mt5", SimpleNamespace(shutdown=lambda: None)):
        client.shutdown()

    assert client._connected is False
    assert client._market_time_offset_seconds is None


def test_mt5_market_client_uses_default_tick_lookback_without_ingest_settings() -> None:
    client = object.__new__(MT5MarketClient)
    client.settings = SimpleNamespace()
    client.metrics = SimpleNamespace(record=lambda *args, **kwargs: None)
    client.connect = lambda: None
    client._extract_price = lambda tick: 100.0
    client._extract_volume = lambda tick: 1.0
    client._tick_timestamp = lambda tick: datetime(2026, 1, 1, tzinfo=timezone.utc)
    client._tick_time_msc = lambda tick: 1767225600000

    captured = {}

    with patch("src.clients.mt5_market.mt5.copy_ticks_from", autospec=True) as copy_ticks_from:
        copy_ticks_from.side_effect = lambda symbol, start, limit, flags: captured.update(
            {"symbol": symbol, "start": start, "limit": limit, "flags": flags}
        ) or [SimpleNamespace()]

        ticks = client.get_ticks("XAUUSD", 10, None)

    assert len(ticks) == 1
    assert captured["symbol"] == "XAUUSD"
    assert captured["limit"] == 10
    assert isinstance(captured["start"], datetime)


def test_mt5_market_client_normalizes_quote_last_price_from_bid_ask() -> None:
    client = object.__new__(MT5MarketClient)
    client.metrics = SimpleNamespace(record=lambda *args, **kwargs: None)
    client.connect = lambda: None
    client._get_field = lambda obj, name, default=None: getattr(obj, name, default)
    client._market_time_from_seconds = lambda value: datetime(2026, 1, 1, tzinfo=timezone.utc)

    with patch("src.clients.mt5_market.mt5.symbol_info_tick", autospec=True) as symbol_info_tick:
        symbol_info_tick.return_value = SimpleNamespace(
            bid=5000.0,
            ask=5000.4,
            last=0.0,
            volume=12.0,
            time=1767225600,
        )
        quote = client.get_quote("XAUUSD")

    assert quote.last == 5000.2


def test_quote_validator_allows_missing_last_trade_price() -> None:
    valid, message = DataValidator.validate_quote(
        "XAUUSD",
        5000.0,
        5000.4,
        0.0,
        0.0,
        "2026-01-01T00:00:00+00:00",
    )

    assert valid is True
    assert message == ""


def test_storage_writer_block_policy_waits_for_capacity_instead_of_dropping() -> None:
    psycopg2_stub = SimpleNamespace(
        OperationalError=RuntimeError,
        Error=RuntimeError,
    )
    extras_stub = SimpleNamespace(
        Json=lambda value: value,
        execute_batch=lambda *args, **kwargs: None,
        execute_values=lambda *args, **kwargs: None,
    )
    pool_stub = SimpleNamespace(SimpleConnectionPool=object)

    with patch.dict(
        sys.modules,
        {
            "psycopg2": psycopg2_stub,
            "psycopg2.extras": extras_stub,
            "psycopg2.pool": pool_stub,
        },
    ):
        from src.persistence.storage_writer import StorageWriter

    writer = object.__new__(StorageWriter)
    writer.settings = SimpleNamespace(queue_put_timeout=0.05)
    writer._stop = threading.Event()
    writer._thread = SimpleNamespace(is_alive=lambda: True)
    writer._channel_stats = {
        "ohlc": {
            "dropped_oldest": 0,
            "dropped_newest": 0,
            "blocked_puts": 0,
            "full_errors": 0,
        }
    }

    q: queue.Queue = queue.Queue(maxsize=1)
    q.put(("existing",))
    channel = {"queue": q, "type": "ohlc", "full_policy": "block"}

    def consumer() -> None:
        time.sleep(0.06)
        q.get_nowait()

    thread = threading.Thread(target=consumer)
    thread.start()
    writer._handle_queue_full("ohlc", channel, ("new",))
    thread.join(timeout=1.0)

    assert writer._channel_stats["ohlc"]["blocked_puts"] == 1
    assert writer._channel_stats["ohlc"]["dropped_newest"] == 0
    assert q.get_nowait() == ("new",)


def test_timescale_writer_normalizes_tick_rows_with_time_msc() -> None:
    psycopg2_stub = SimpleNamespace(
        OperationalError=RuntimeError,
        Error=RuntimeError,
    )
    extras_stub = SimpleNamespace(
        Json=lambda value: value,
        execute_batch=lambda *args, **kwargs: None,
        execute_values=lambda *args, **kwargs: None,
    )
    pool_stub = SimpleNamespace(SimpleConnectionPool=object)

    with patch.dict(
        sys.modules,
        {
            "psycopg2": psycopg2_stub,
            "psycopg2.extras": extras_stub,
            "psycopg2.pool": pool_stub,
        },
    ):
        from src.persistence.db import TimescaleWriter

    writer = object.__new__(TimescaleWriter)
    captured = []
    writer._batch = lambda sql, rows, page_size=1000: captured.extend(rows)

    writer.write_ticks([("XAUUSD", 100.0, 1.0, "2026-01-01T00:00:00.123000+00:00")])

    assert captured == [("XAUUSD", 100.0, 1.0, "2026-01-01T00:00:00.123000+00:00", 1767225600123)]


def test_storage_writer_stats_include_queue_summary() -> None:
    from src.persistence.storage_writer import StorageWriter

    writer = object.__new__(StorageWriter)
    writer._thread = SimpleNamespace(is_alive=lambda: True)
    writer._channels = {
        "ohlc": {
            "queue": queue.Queue(maxsize=10),
            "pending": deque([("pending",)] * 2),
            "full_policy": "block",
        }
    }
    writer._channel_stats = {
        "ohlc": {
            "dropped_oldest": 0,
            "dropped_newest": 0,
            "blocked_puts": 1,
            "full_errors": 0,
        }
    }

    for i in range(9):
        writer._channels["ohlc"]["queue"].put_nowait((i,))  # type: ignore[index]

    stats = writer.stats()

    assert stats["summary"]["high"] == 1
    assert stats["queues"]["ohlc"]["status"] == "high"
    assert stats["queues"]["ohlc"]["utilization_pct"] == 90.0


def test_timescale_writer_wraps_ohlc_indicators_with_json_adapter() -> None:
    json_calls = []
    from src.persistence.db import TimescaleWriter
    import src.persistence.db as persistence_db

    writer = object.__new__(TimescaleWriter)
    captured = []
    writer._batch = lambda sql, rows, page_size=1000: captured.extend(rows)

    with patch.object(
        persistence_db,
        "Json",
        side_effect=lambda value: json_calls.append(value) or ("json", value),
    ):
        writer.write_ohlc(
            [
                (
                    "XAUUSD",
                    "M1",
                    100.0,
                    101.0,
                    99.0,
                    100.5,
                    1.0,
                    "2026-01-01T00:00:00+00:00",
                    {"macd": {"value": 1.2}},
                )
            ],
            upsert=True,
        )

    assert json_calls == [{"macd": {"value": 1.2}}]
    assert captured[0][8] == ("json", {"macd": {"value": 1.2}})


def test_tick_schema_includes_time_msc_backfill_migration() -> None:
    from src.persistence.schema.ticks import DDL

    assert "ADD COLUMN IF NOT EXISTS time_msc" in DDL
    assert "SET time_msc = FLOOR(EXTRACT(EPOCH FROM time) * 1000)::bigint" in DDL


def test_market_service_merge_ticks_orders_by_time_msc() -> None:
    tick_late = Tick(
        symbol="XAUUSD",
        price=1.0,
        volume=1.0,
        time=datetime(2026, 1, 1, 0, 0, 0, 200000, tzinfo=timezone.utc),
        time_msc=1767225600200,
    )
    tick_early = Tick(
        symbol="XAUUSD",
        price=1.0,
        volume=1.0,
        time=datetime(2026, 1, 1, 0, 0, 0, 100000, tzinfo=timezone.utc),
        time_msc=1767225600100,
    )

    merged = MarketDataService._merge_ticks([tick_late], [tick_early])

    assert merged == [tick_early, tick_late]


def test_load_ingest_settings_exposes_intrabar_enabled() -> None:
    from src.config.compat import load_ingest_settings

    load_ingest_settings.cache_clear()
    settings = load_ingest_settings()

    assert hasattr(settings, "intrabar_enabled")


def test_market_service_removes_bound_method_listeners_by_callable_identity() -> None:
    service = MarketDataService(client=SimpleNamespace())

    class ListenerOwner:
        def __init__(self) -> None:
            self.closed_calls = 0
            self.intrabar_calls = 0

        def on_close(self, _symbol: str, _timeframe: str, _bar_time: datetime) -> None:
            self.closed_calls += 1

        def on_intrabar(self, _symbol: str, _timeframe: str, _bar: Tick) -> None:
            self.intrabar_calls += 1

    owner = ListenerOwner()

    service.add_ohlc_close_listener(owner.on_close)
    service.add_intrabar_listener(owner.on_intrabar)

    service.remove_ohlc_close_listener(owner.on_close)
    service.remove_intrabar_listener(owner.on_intrabar)

    service.enqueue_ohlc_closed_event("XAUUSD", "M1", datetime(2026, 1, 1, tzinfo=timezone.utc))
    service.set_intrabar("XAUUSD", "M1", SimpleNamespace(time=datetime(2026, 1, 1, tzinfo=timezone.utc)))

    assert owner.closed_calls == 0
    assert owner.intrabar_calls == 0
