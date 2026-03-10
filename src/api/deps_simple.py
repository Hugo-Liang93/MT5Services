"""
Simplified dependency container used by app_simple.
"""

from contextlib import asynccontextmanager
import logging
from typing import Any, Dict

from src.core.market_service import MarketDataService
from src.core.account_service import AccountService
from src.core.trading_service import TradingService
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter
from src.ingestion.ingestor import BackgroundIngestor
from src.clients.mt5_market import MT5MarketClient
from src.indicators.optimized.worker_enhanced import EnhancedIndicatorWorker
from src.monitoring.health_check import get_health_monitor, get_monitoring_manager
from src.config.simple_config import (
    get_cached_market_settings,
    get_cached_indicator_settings,
    get_cached_ingest_settings,
    get_cached_mt5_settings,
)

logger = logging.getLogger(__name__)


class _Obj:
    """Small helper to expose dict keys as attributes."""

    def __init__(self, data: Dict[str, Any]):
        for key, value in data.items():
            setattr(self, key, value)


def _build_mt5_settings() -> _Obj:
    raw = get_cached_mt5_settings().copy()
    return _Obj(
        {
            "mt5_login": raw.get("mt5_login"),
            "mt5_password": raw.get("mt5_password"),
            "mt5_server": raw.get("mt5_server"),
            "mt5_path": raw.get("mt5_path"),
            "timezone": raw.get("timezone", "UTC"),
        }
    )


def _build_market_settings() -> _Obj:
    raw = get_cached_market_settings().copy()
    return _Obj(raw)


def _build_indicator_settings() -> _Obj:
    raw = get_cached_indicator_settings().copy()
    return _Obj(raw)


def _build_ingest_settings() -> _Obj:
    raw = get_cached_ingest_settings().copy()
    return _Obj(
        {
            "tick_cache_size": int(raw.get("tick_cache_size", 5000)),
            "tick_initial_lookback_seconds": int(raw.get("tick_initial_lookback_seconds", 20)),
            "ingest_symbols": raw.get("ingest_symbols", ["XAUUSD"]),
            "ingest_tick_interval": float(raw.get("tick_interval_seconds", 0.5)),
            "ingest_ohlc_timeframes": raw.get("ingest_timeframes", ["M1", "H1"]),
            "ingest_ohlc_interval": float(raw.get("ohlc_interval_seconds", 30.0)),
            "ingest_ohlc_intervals": raw.get("ingest_ohlc_intervals", {}),
            "ohlc_backfill_limit": int(raw.get("ohlc_backfill_limit", 500)),
            "intrabar_enabled": bool(raw.get("intrabar_enabled", True)),
        }
    )


def _build_db_settings() -> _Obj:
    return _Obj(
        {
            "pg_host": "localhost",
            "pg_port": 5432,
            "pg_user": "postgres",
            "pg_password": "postgres",
            "pg_database": "mt5",
            "pg_schema": "public",
        }
    )


def _build_storage_settings() -> _Obj:
    return _Obj(
        {
            "flush_retry_attempts": 3,
            "flush_retry_backoff": 1.0,
            "ohlc_upsert_open_bar": False,
            "quote_flush_enabled": False,
            "intrabar_enabled": True,
        }
    )


mt5_settings = _build_mt5_settings()
market_settings = _build_market_settings()
ingest_settings = _build_ingest_settings()
indicator_settings = _build_indicator_settings()
db_settings = _build_db_settings()
storage_settings = _build_storage_settings()

_mt5_client: MT5MarketClient | None = None
_market_service: MarketDataService | None = None
_account_service: AccountService | None = None
_trading_service: TradingService | None = None
_timescale_writer: TimescaleWriter | None = None
_storage_writer: StorageWriter | None = None
_ingestor: BackgroundIngestor | None = None
_enhanced_indicator_worker: EnhancedIndicatorWorker | None = None
_health_monitor = None
_monitoring_manager = None


def get_mt5_client() -> MT5MarketClient:
    global _mt5_client
    if _mt5_client is None:
        _mt5_client = MT5MarketClient(settings=mt5_settings)
    return _mt5_client


def get_market_service() -> MarketDataService:
    global _market_service
    if _market_service is None:
        _market_service = MarketDataService(client=get_mt5_client(), market_settings=market_settings)
    return _market_service


def get_account_service() -> AccountService:
    global _account_service
    if _account_service is None:
        _account_service = AccountService()
    return _account_service


def get_trading_service() -> TradingService:
    global _trading_service
    if _trading_service is None:
        _trading_service = TradingService()
    return _trading_service


def get_timescale_writer() -> TimescaleWriter:
    global _timescale_writer
    if _timescale_writer is None:
        _timescale_writer = TimescaleWriter(settings=db_settings)
    return _timescale_writer


def get_storage_writer() -> StorageWriter:
    global _storage_writer
    if _storage_writer is None:
        _storage_writer = StorageWriter(
            db_writer=get_timescale_writer(),
            storage_settings=storage_settings,
        )
    return _storage_writer


def get_ingestor() -> BackgroundIngestor:
    global _ingestor
    if _ingestor is None:
        _ingestor = BackgroundIngestor(
            service=get_market_service(),
            storage=get_storage_writer(),
            ingest_settings=ingest_settings,
        )
    return _ingestor


def get_enhanced_indicator_worker() -> EnhancedIndicatorWorker:
    global _enhanced_indicator_worker
    if _enhanced_indicator_worker is None:
        _enhanced_indicator_worker = EnhancedIndicatorWorker(
            service=get_market_service(),
            symbols=ingest_settings.ingest_symbols,
            timeframes=ingest_settings.ingest_ohlc_timeframes,
            tasks=[],
            indicator_settings=indicator_settings,
            storage=get_storage_writer(),
            event_store_db_path="events_simple.db",
        )
    return _enhanced_indicator_worker


def get_health_monitor_simple():
    global _health_monitor
    if _health_monitor is None:
        _health_monitor = get_health_monitor("health_monitor_simple.db")
    return _health_monitor


def get_monitoring_manager_simple():
    global _monitoring_manager
    if _monitoring_manager is None:
        _monitoring_manager = get_monitoring_manager(get_health_monitor_simple())
    return _monitoring_manager


@asynccontextmanager
async def lifespan(app):
    logger.info("Starting MT5Services simple mode...")

    storage = get_storage_writer()
    ingestor = get_ingestor()
    worker = get_enhanced_indicator_worker()
    get_health_monitor_simple()

    try:
        storage.start()
        ingestor.start()
        worker.start()
        yield
    finally:
        worker.stop()
        ingestor.stop()
        storage.stop()
        logger.info("Stopped MT5Services simple mode")
