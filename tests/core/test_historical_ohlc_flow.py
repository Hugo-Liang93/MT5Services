from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from src.clients.mt5_market import OHLC
from src.core.market_service import MarketDataService
from src.indicators.manager import UnifiedIndicatorManager
from src.indicators.engine.parallel_executor import ParallelExecutor
from src.indicators.engine.pipeline import OptimizedPipeline
from src.config.indicator_config import CacheStrategy, PipelineConfig as IndicatorPipelineConfig


class DummyClient:
    def __init__(self) -> None:
        self.tz = timezone.utc

    def _to_tz(self, dt: datetime) -> datetime:
        return dt

    def list_symbols(self):
        return ["XAUUSD"]

    def get_symbol_info(self, symbol: str):
        return {"symbol": symbol}

    def health(self):
        return {"connected": True}


def _market_settings() -> SimpleNamespace:
    return SimpleNamespace(
        default_symbol="XAUUSD",
        tick_limit=200,
        ohlc_limit=200,
        quote_stale_seconds=1.0,
        stream_interval_seconds=1.0,
        tick_cache_size=100,
        ohlc_cache_limit=500,
        intrabar_max_points=50,
        ohlc_event_queue_size=100,
    )


def _bar(minute: int) -> OHLC:
    bar_time = datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc)
    return OHLC(
        symbol="XAUUSD",
        timeframe="M1",
        time=bar_time,
        open=100.0 + minute,
        high=101.0 + minute,
        low=99.0 + minute,
        close=100.5 + minute,
        volume=10.0 + minute,
        indicators={},
    )


def test_market_service_loads_historical_window_from_storage() -> None:
    service = MarketDataService(DummyClient(), market_settings=_market_settings())
    bars = [_bar(0), _bar(1), _bar(2)]
    rows = [
        (
            bar.symbol,
            bar.timeframe,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.time,
            bar.indicators,
        )
        for bar in bars
    ]
    storage = SimpleNamespace(db=SimpleNamespace(fetch_ohlc_before=lambda *args, **kwargs: rows))
    service.attach_storage(storage)

    result = service.get_ohlc_window("XAUUSD", "M1", bars[-1].time, limit=3)

    assert [bar.time for bar in result] == [bar.time for bar in bars]
    assert service.get_latest_ohlc("XAUUSD", "M1").time == bars[-1].time


def test_indicator_manager_loads_bar_time_window_from_market_service() -> None:
    target_bar = _bar(4)
    calls = {"window": 0, "latest": 0}

    class ServiceStub:
        def get_ohlc(self, *args, **kwargs):
            calls["latest"] += 1
            return [_bar(3), target_bar]

        def get_ohlc_window(self, *args, **kwargs):
            calls["window"] += 1
            return [_bar(2), _bar(3), target_bar]

    manager = object.__new__(UnifiedIndicatorManager)
    manager.market_service = ServiceStub()
    manager._get_max_lookback = lambda: 10

    bars = manager._load_bars("XAUUSD", "M1", bar_time=target_bar.time)

    assert calls["window"] == 1
    assert calls["latest"] == 0
    assert bars[-1].time == target_bar.time


def test_backfill_writes_closed_bars_into_cache_and_event_flow() -> None:
    initial_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    closed_bars = [_bar(1), _bar(2)]
    writes = []
    cached_batches = []
    events = []

    class ClientStub:
        def __init__(self) -> None:
            self.responses = [closed_bars, []]

        def get_ohlc_from(self, **kwargs):
            return self.responses.pop(0)

    service = SimpleNamespace(
        client=ClientStub(),
        set_ohlc_closed=lambda symbol, timeframe, bars: cached_batches.append((symbol, timeframe, list(bars))),
        enqueue_ohlc_closed_event=lambda symbol, timeframe, bar_time: events.append((symbol, timeframe, bar_time)),
    )
    storage = SimpleNamespace(
        db=SimpleNamespace(
            write_ohlc=lambda rows, upsert=True: writes.append((list(rows), upsert)),
        )
    )
    settings = SimpleNamespace(
        ingest_symbols=["XAUUSD"],
        ingest_ohlc_timeframes=["M1"],
        ohlc_backfill_limit=10,
    )

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
        from src.ingestion.ingestor import BackgroundIngestor

    ingestor = BackgroundIngestor(service=service, storage=storage, ingest_settings=settings)
    key = "XAUUSD:M1"
    ingestor._stop = threading.Event()
    ingestor._backfill_progress = {key: initial_time}
    ingestor._backfill_cutoff = {key: datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)}

    ingestor._backfill_ohlc()

    assert len(writes) == 1
    assert writes[0][1] is True
    assert len(writes[0][0]) == len(closed_bars)
    assert cached_batches == [("XAUUSD", "M1", closed_bars)]
    assert events == [("XAUUSD", "M1", bar.time) for bar in closed_bars]


def test_indicator_manager_normalizes_legacy_flat_persisted_values() -> None:
    manager = object.__new__(UnifiedIndicatorManager)
    manager.config = SimpleNamespace(
        indicators=[
            SimpleNamespace(name="macd", enabled=True),
            SimpleNamespace(name="rsi14", enabled=True),
        ]
    )

    normalized = manager._normalize_persisted_indicator_snapshot(
        {
            "macd": 1.2,
            "macd_signal": 0.7,
            "macd_hist": 0.5,
            "rsi14": 55.0,
        }
    )

    assert normalized == {
        "macd": {"macd": 1.2, "signal": 0.7, "hist": 0.5},
        "rsi14": {"rsi14": 55.0},
    }


def test_write_back_results_persists_grouped_indicator_payload() -> None:
    latest_bar = _bar(3)
    stored_rows = []

    class ServiceStub:
        def update_ohlc_indicators(self, symbol, timeframe, bar_time, indicators):
            if latest_bar.time == bar_time:
                latest_bar.indicators = indicators

    manager = object.__new__(UnifiedIndicatorManager)
    manager.market_service = ServiceStub()
    manager.storage_writer = SimpleNamespace(
        enqueue=lambda channel, row: stored_rows.append((channel, row)),
    )
    manager._store_results = lambda *args, **kwargs: None

    grouped = manager._write_back_results(
        "XAUUSD",
        "M1",
        [latest_bar],
        {"macd": {"macd": 1.1, "signal": 0.8}},
        compute_time_ms=1.0,
        bar_time=latest_bar.time,
    )

    assert grouped == {"macd": {"macd": 1.1, "signal": 0.8}}
    assert latest_bar.indicators == {"macd": {"macd": 1.1, "signal": 0.8}}
    assert stored_rows[0][0] == "ohlc_indicators"
    assert stored_rows[0][1][8] == {"macd": {"macd": 1.1, "signal": 0.8}}


def test_indicator_manager_batches_same_scope_events_into_single_window_load() -> None:
    completed = []
    window_calls = []
    bars = [_bar(1), _bar(2), _bar(3)]

    class ServiceStub:
        def get_ohlc_window(self, symbol, timeframe, end_time, limit):
            window_calls.append((symbol, timeframe, end_time, limit))
            return bars

    manager = object.__new__(UnifiedIndicatorManager)
    manager.market_service = ServiceStub()
    manager.event_store = SimpleNamespace(
        mark_event_completed=lambda symbol, timeframe, bar_time: completed.append((symbol, timeframe, bar_time)),
        mark_event_failed=lambda *args, **kwargs: None,
    )
    manager.pipeline = SimpleNamespace(compute=lambda *args, **kwargs: {"rsi14": {"rsi": 50.0}})
    manager._get_max_lookback = lambda: 5
    manager._resolve_indicator_names = lambda indicator_names=None: ["rsi14"]
    manager._write_back_results = lambda *args, **kwargs: None

    manager._process_closed_bar_events_batch(
        [
            ("XAUUSD", "M1", bars[1].time),
            ("XAUUSD", "M1", bars[2].time),
        ],
        durable_event=True,
    )

    assert len(window_calls) == 1
    assert completed == [
        ("XAUUSD", "M1", bars[1].time),
        ("XAUUSD", "M1", bars[2].time),
    ]


def test_indicator_manager_completes_early_history_event_without_retry() -> None:
    early_bar = _bar(0)
    completed = []
    failed = []

    class ServiceStub:
        def get_ohlc_window(self, symbol, timeframe, end_time, limit):
            return [early_bar]

    manager = object.__new__(UnifiedIndicatorManager)
    manager.market_service = ServiceStub()
    manager.event_store = SimpleNamespace(
        mark_event_completed=lambda symbol, timeframe, bar_time: completed.append((symbol, timeframe, bar_time)),
        mark_event_failed=lambda symbol, timeframe, bar_time, error: failed.append((symbol, timeframe, bar_time, error)),
    )
    manager.pipeline = SimpleNamespace(compute=lambda *args, **kwargs: {"rsi14": {"rsi": 50.0}})
    manager._get_max_lookback = lambda: 5
    manager._resolve_indicator_names = lambda indicator_names=None: ["rsi14"]
    manager._write_back_results = lambda *args, **kwargs: None

    manager._process_closed_bar_events_batch(
        [("XAUUSD", "M1", early_bar.time)],
        durable_event=True,
    )

    assert completed == [("XAUUSD", "M1", early_bar.time)]
    assert failed == []


def test_indicator_manager_falls_back_to_per_event_window_when_batch_window_misses_bar() -> None:
    missing_from_batch = _bar(3)
    newer_bars = [_bar(20), _bar(21), _bar(22)]
    completed = []
    failed = []
    fallback_calls = []

    class ServiceStub:
        def get_ohlc_window(self, symbol, timeframe, end_time, limit):
            fallback_calls.append((symbol, timeframe, end_time, limit))
            if end_time == newer_bars[-1].time:
                return newer_bars
            if end_time == missing_from_batch.time:
                return [_bar(1), _bar(2), missing_from_batch]
            return []

    manager = object.__new__(UnifiedIndicatorManager)
    manager.market_service = ServiceStub()
    manager.event_store = SimpleNamespace(
        mark_event_completed=lambda symbol, timeframe, bar_time: completed.append((symbol, timeframe, bar_time)),
        mark_event_failed=lambda symbol, timeframe, bar_time, error: failed.append((symbol, timeframe, bar_time, error)),
    )
    manager.pipeline = SimpleNamespace(compute=lambda *args, **kwargs: {"rsi14": {"rsi": 50.0}})
    manager._get_max_lookback = lambda: 3
    manager._resolve_indicator_names = lambda indicator_names=None: ["rsi14"]
    manager._write_back_results = lambda *args, **kwargs: None

    manager._process_closed_bar_events_batch(
        [("XAUUSD", "M1", missing_from_batch.time), ("XAUUSD", "M1", newer_bars[-1].time)],
        durable_event=True,
    )

    assert completed == [
        ("XAUUSD", "M1", missing_from_batch.time),
        ("XAUUSD", "M1", newer_bars[-1].time),
    ]
    assert failed == []
    assert fallback_calls[0][2] == newer_bars[-1].time
    assert fallback_calls[1][2] == missing_from_batch.time


def test_parallel_executor_wait_for_tasks_does_not_emit_false_timeout_warning(caplog) -> None:
    executor = ParallelExecutor(max_workers=1, enable_cache=False)
    caplog.set_level(logging.WARNING)

    try:
        results = executor.execute_parallel(
            [
                (lambda: "ok", (), {}),
            ],
            task_ids=["task-1"],
            use_cache=False,
        )
    finally:
        executor.shutdown(wait=True)

    assert results["task-1"] is not None
    assert "Timeout waiting for task" not in caplog.text


def test_pipeline_accepts_enum_cache_strategy_without_warning(caplog) -> None:
    caplog.set_level(logging.WARNING)
    pipeline = OptimizedPipeline(
        IndicatorPipelineConfig(
            enable_parallel=False,
            cache_strategy=CacheStrategy.LRU_TTL,
        )
    )

    try:
        assert pipeline.config.cache_strategy == "lru_ttl"
    finally:
        pipeline.shutdown()

    assert "Unsupported cache strategy" not in caplog.text


def test_pipeline_logs_regular_completions_at_debug(caplog) -> None:
    pipeline = OptimizedPipeline(
        IndicatorPipelineConfig(
            enable_parallel=False,
            enable_cache=False,
        )
    )
    pipeline.dependency_manager = SimpleNamespace(
        indicator_funcs={"rsi14": lambda bars, params: {"rsi": 50.0}},
        indicator_params={"rsi14": {}},
        get_dependencies=lambda name: set(),
        get_parallelizable_groups=lambda indicators: [indicators],
    )
    caplog.set_level(logging.DEBUG)

    try:
        result = pipeline.compute("XAUUSD", "M1", [_bar(0), _bar(1)], ["rsi14"])
    finally:
        pipeline.shutdown()

    assert result == {"rsi14": {"rsi": 50.0}}
    assert "Pipeline computation completed" in caplog.text
    assert not any(record.levelname == "INFO" and "Pipeline computation completed" in record.message for record in caplog.records)


def test_indicator_manager_computes_only_eligible_indicators_for_short_history() -> None:
    manager = object.__new__(UnifiedIndicatorManager)
    manager.config = SimpleNamespace(
        indicators=[
            SimpleNamespace(name="rsi14", enabled=True, params={"min_bars": 15}),
            SimpleNamespace(name="ema50", enabled=True, params={"min_bars": 50}),
        ]
    )

    selected = manager._select_indicator_names_for_history(available_bars=20)

    assert selected == ["rsi14"]
