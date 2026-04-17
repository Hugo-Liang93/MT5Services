from __future__ import annotations

from src.clients.mt5_market import MT5MarketClient
from src.ingestion.ingestor import BackgroundIngestor
from src.market import MarketDataService


def create_market_service(mt5_settings, market_settings) -> MarketDataService:
    client = MT5MarketClient(mt5_settings)
    return MarketDataService(client=client, market_settings=market_settings)


def create_ingestor(
    service: MarketDataService,
    storage_writer,
    ingest_settings,
) -> BackgroundIngestor:
    return BackgroundIngestor(
        service=service,
        storage=storage_writer,
        ingest_settings=ingest_settings,
    )
