from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from src.clients.mt5_market import OHLC, Quote, Tick
from src.ingestion.ingestor import BackgroundIngestor
from src.indicators.runtime import event_io, event_loops
from src.market import MarketDataService
from src.utils.event_store import LocalEventStore


class _DummyClient:
    def __init__(
        self,
        *,
        quote: Quote | None = None,
        ticks: list[Tick] | None = None,
        bars: list[OHLC] | None = None,
    ) -> None:
        self.tz = timezone.utc
        self._quote = quote
        self._ticks = list(ticks or [])
        self._bars = list(bars or [])

    def _to_tz(self, dt: datetime) -> datetime:
        return dt

    def list_symbols(self) -> list[str]:
        return ["XAUUSD"]

    def get_symbol_info(self, symbol: str) -> dict[str, object]:
        return {"symbol": symbol, "point": 0.01}

    def health(self) -> dict[str, object]:
        return {"connected": True}

    def get_quote(self, symbol: str) -> Quote:
        assert self._quote is not None
        return self._quote

    def get_ticks(
        self,
        symbol: str,
        limit: int,
        start: datetime | None = None,
    ) -> list[Tick]:
        return list(self._ticks)

    def get_ohlc(self, symbol: str, timeframe: str, limit: int) -> list[OHLC]:
        return list(self._bars)

    def get_ohlc_from(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        limit: int,
    ) -> list[OHLC]:
        return list(self._bars)


class _CapturingStorage:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            quote_flush_enabled=True,
            intrabar_enabled=False,
        )
        self.rows: dict[str, list[tuple]] = {
            "quotes": [],
            "ticks": [],
            "ohlc": [],
            "intrabar": [],
        }

    def enqueue(self, channel: str, item: tuple) -> None:
        self.rows.setdefault(channel, []).append(item)


def _market_settings() -> SimpleNamespace:
    return SimpleNamespace(
        default_symbol="XAUUSD",
        tick_limit=100,
        ohlc_limit=100,
        quote_stale_seconds=30.0,
        stream_interval_seconds=1.0,
        tick_cache_size=100,
        ohlc_cache_limit=200,
        intrabar_max_points=20,
        ohlc_event_queue_size=32,
    )


def _ingest_settings() -> SimpleNamespace:
    return SimpleNamespace(
        ingest_symbols=["XAUUSD"],
        ingest_ohlc_timeframes=["M5"],
        ingest_poll_interval=0.5,
        ingest_ohlc_interval=10.0,
        ingest_ohlc_intervals={},
        tick_cache_size=100,
        ohlc_backfill_limit=50,
        max_concurrent_symbols=1,
        connection_timeout=2.0,
        symbol_error_threshold=5,
        symbol_cooldown_seconds=60.0,
        symbol_max_cooldown_seconds=300.0,
        intrabar_enabled=False,
    )


def test_ingestor_cycle_updates_cache_persists_closed_bar_and_emits_event() -> None:
    now = datetime.now(timezone.utc)
    quote = Quote(
        symbol="XAUUSD",
        bid=2320.1,
        ask=2320.4,
        last=2320.25,
        volume=12.0,
        time=now,
    )
    tick = Tick(
        symbol="XAUUSD",
        price=2320.3,
        volume=1.5,
        time=now - timedelta(seconds=5),
        time_msc=int((now - timedelta(seconds=5)).timestamp() * 1000),
    )
    closed_bar = OHLC(
        symbol="XAUUSD",
        timeframe="M5",
        time=now - timedelta(minutes=10),
        open=2318.0,
        high=2321.0,
        low=2317.5,
        close=2320.0,
        volume=88.0,
        indicators={},
    )
    open_bar = OHLC(
        symbol="XAUUSD",
        timeframe="M5",
        time=now - timedelta(minutes=1),
        open=2320.0,
        high=2321.2,
        low=2319.8,
        close=2320.8,
        volume=21.0,
        indicators={},
    )
    client = _DummyClient(quote=quote, ticks=[tick], bars=[closed_bar, open_bar])
    service = MarketDataService(client=client, market_settings=_market_settings())
    storage = _CapturingStorage()
    ingestor = BackgroundIngestor(service, storage, _ingest_settings())

    try:
        ingestor._ingest_quote("XAUUSD")
        ingestor._ingest_ticks("XAUUSD")
        ingestor._ingest_ohlc("XAUUSD", {})

        assert service.get_quote("XAUUSD") == quote
        assert service.get_ticks("XAUUSD", limit=10) == [tick]

        closed_bars = service.get_ohlc_closed("XAUUSD", "M5", limit=10)
        assert [bar.time for bar in closed_bars] == [closed_bar.time]
        assert service.get_latest_ohlc("XAUUSD", "M5").time == closed_bar.time

        event = service.get_ohlc_event(timeout=0.2)
        assert event == ("XAUUSD", "M5", closed_bar.time)

        assert len(storage.rows["quotes"]) == 1
        assert len(storage.rows["ticks"]) == 1
        assert len(storage.rows["ohlc"]) == 1
        assert storage.rows["ohlc"][0][7] == closed_bar.time.isoformat()
    finally:
        service.shutdown()


def test_closed_bar_event_sink_persists_durable_indicator_events(
    tmp_path: Path,
) -> None:
    service = MarketDataService(
        client=_DummyClient(),
        market_settings=_market_settings(),
    )
    event_store = LocalEventStore(str(tmp_path / "events.db"))
    manager = SimpleNamespace(
        state=SimpleNamespace(
            stop_event=threading.Event(),
            event_write_queue=queue.Queue(maxsize=16),
        ),
        event_store=event_store,
    )

    service.set_ohlc_event_sink(
        lambda symbol, timeframe, bar_time: event_io.publish_closed_bar_event(
            manager,
            symbol,
            timeframe,
            bar_time,
        )
    )
    writer_thread = threading.Thread(
        target=event_loops.run_event_writer_loop,
        args=(manager,),
        name="test-indicator-event-writer",
        daemon=True,
    )
    writer_thread.start()

    bar_time = datetime(2026, 4, 11, 14, 0, tzinfo=timezone.utc)
    stats = {}
    try:
        service.enqueue_ohlc_closed_event("XAUUSD", "M5", bar_time)

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            stats = event_store.get_stats()
            if stats.get("total") == 1 and stats.get("pending") == 1:
                break
            time.sleep(0.05)

        assert stats["total"] == 1
        assert stats["pending"] == 1

        claimed = event_store.claim_next_event()
        assert claimed is not None
        assert claimed.symbol == "XAUUSD"
        assert claimed.timeframe == "M5"
        assert claimed.bar_time == bar_time
    finally:
        manager.state.stop_event.set()
        writer_thread.join(timeout=5.0)
        service.shutdown()
        event_store.close()
