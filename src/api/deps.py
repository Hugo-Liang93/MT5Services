"""
Unified dependency container.

Single runtime mode with all major capabilities enabled:
- ingestion + storage
- enhanced indicator worker
- monitoring manager
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import Optional

from src.core.market_service import MarketDataService
from src.core.account_service import AccountService
from src.core.trading_service import TradingService
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter
from src.ingestion.ingestor import BackgroundIngestor
from src.clients.mt5_market import MT5MarketClient
from src.indicators.optimized.worker_enhanced import EnhancedIndicatorWorker
from src.monitoring.health_check import get_health_monitor, get_monitoring_manager
from src.config import (
    load_mt5_settings,
    load_db_settings,
    load_ingest_settings,
    load_storage_settings,
    load_market_settings,
    load_indicator_settings,
)

logger = logging.getLogger(__name__)

# Lazily initialized singletons
_service: Optional[MarketDataService] = None
_storage_writer: Optional[StorageWriter] = None
_ingestor: Optional[BackgroundIngestor] = None
_indicator_worker: Optional[EnhancedIndicatorWorker] = None
_account_service: Optional[AccountService] = None
_trading_service: Optional[TradingService] = None
_health_monitor = None
_monitoring_manager = None


def _ensure_initialized() -> None:
    global _service
    global _storage_writer
    global _ingestor
    global _indicator_worker
    global _account_service
    global _trading_service
    global _health_monitor
    global _monitoring_manager

    if _service is not None:
        return

    mt5_settings = load_mt5_settings()
    db_settings = load_db_settings()
    ingest_settings = load_ingest_settings()
    storage_settings = load_storage_settings()
    market_settings = load_market_settings()
    indicator_settings = load_indicator_settings()

    mt5_client = MT5MarketClient(mt5_settings)
    _service = MarketDataService(client=mt5_client, market_settings=market_settings)
    _storage_writer = StorageWriter(TimescaleWriter(db_settings), storage_settings=storage_settings)
    _ingestor = BackgroundIngestor(service=_service, storage=_storage_writer, ingest_settings=ingest_settings)
    _indicator_worker = EnhancedIndicatorWorker(
        service=_service,
        symbols=ingest_settings.ingest_symbols,
        timeframes=ingest_settings.ingest_ohlc_timeframes,
        tasks=[],
        indicator_settings=indicator_settings,
        storage=_storage_writer,
        event_store_db_path="events.db",
    )
    _account_service = AccountService()
    _trading_service = TradingService()
    _health_monitor = get_health_monitor("health_monitor.db")
    _monitoring_manager = get_monitoring_manager(_health_monitor, check_interval=60)


def get_runtime_mode() -> str:
    return "unified"


def is_monitoring_enabled() -> bool:
    return True


def get_market_service() -> MarketDataService:
    _ensure_initialized()
    return _service  # type: ignore[return-value]


def get_account_service() -> AccountService:
    _ensure_initialized()
    return _account_service  # type: ignore[return-value]


def get_trading_service() -> TradingService:
    _ensure_initialized()
    return _trading_service  # type: ignore[return-value]


def get_ingestor() -> BackgroundIngestor:
    _ensure_initialized()
    return _ingestor  # type: ignore[return-value]


def get_indicator_worker() -> EnhancedIndicatorWorker:
    _ensure_initialized()
    return _indicator_worker  # type: ignore[return-value]


def get_health_monitor_instance():
    _ensure_initialized()
    return _health_monitor


def get_monitoring_manager_instance():
    _ensure_initialized()
    return _monitoring_manager


@asynccontextmanager
async def lifespan(_app):
    _ensure_initialized()

    try:
        _storage_writer.start()  # type: ignore[union-attr]
        _ingestor.start()  # type: ignore[union-attr]
        _indicator_worker.start()  # type: ignore[union-attr]

        _monitoring_manager.register_component(  # type: ignore[union-attr]
            "data_ingestion",
            _ingestor,
            ["queue_stats"],
        )
        _monitoring_manager.register_component(  # type: ignore[union-attr]
            "indicator_calculation",
            _indicator_worker,
            ["indicator_freshness", "cache_stats", "performance_stats"],
        )
        _monitoring_manager.register_component(  # type: ignore[union-attr]
            "market_data",
            _service,
            ["data_latency"],
        )
        _monitoring_manager.start()  # type: ignore[union-attr]
        _health_monitor.record_metric(  # type: ignore[union-attr]
            "system",
            "startup",
            1.0,
            {"version": "unified", "timestamp": "now"},
        )
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to start unified services: %s", exc)
        raise

    try:
        yield
    finally:
        _monitoring_manager.stop()  # type: ignore[union-attr]
        _indicator_worker.stop()  # type: ignore[union-attr]
        _ingestor.stop()  # type: ignore[union-attr]
        _storage_writer.stop()  # type: ignore[union-attr]
        _health_monitor.record_metric(  # type: ignore[union-attr]
            "system",
            "shutdown",
            1.0,
            {"timestamp": "now"},
        )
