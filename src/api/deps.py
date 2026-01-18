"""
依赖与单例：集中管理服务/采集器，供路由模块复用。
"""

from contextlib import asynccontextmanager

from src.core.market_service import MarketDataService
from src.core.account_service import AccountService
from src.core.trading_service import TradingService
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter
from src.ingestion.ingestor import BackgroundIngestor
from src.clients.mt5_market import MT5MarketClient
from src.indicators.worker import IndicatorWorker
from src.config import (
    load_mt5_settings,
    load_db_settings,
    load_ingest_settings,
    load_storage_settings,
    load_market_settings,
    load_indicator_settings,
)

mt5_settings = load_mt5_settings()
db_settings = load_db_settings()
ingest_settings = load_ingest_settings()
storage_settings = load_storage_settings()
market_settings = load_market_settings()
indicator_settings = load_indicator_settings()

mt5_client = MT5MarketClient(mt5_settings)
service = MarketDataService(client=mt5_client, market_settings=market_settings)
storage_writer = StorageWriter(TimescaleWriter(db_settings), storage_settings=storage_settings)
ingestor = BackgroundIngestor(service=service, storage=storage_writer, ingest_settings=ingest_settings)
indicator_worker = IndicatorWorker(
    service=service,
    symbols=ingest_settings.ingest_symbols,
    timeframes=ingest_settings.ingest_ohlc_timeframes,
    tasks=[],  # 由 config/indicators.ini 热加载
    indicator_settings=indicator_settings,
    storage=storage_writer,
)
account_service = AccountService()
trading_service = TradingService()


def get_market_service() -> MarketDataService:
    return service


def get_account_service() -> AccountService:
    return account_service


def get_trading_service() -> TradingService:
    return trading_service


@asynccontextmanager
async def lifespan(_app):
    try:
        storage_writer.start()
        ingestor.start()
        indicator_worker.start()
    except Exception as exc:  # pragma: no cover
        import logging

        logging.getLogger(__name__).exception("Failed to start ingestor: %s", exc)
    try:
        yield
    finally:
        ingestor.stop()
        storage_writer.stop()
        indicator_worker.stop()
