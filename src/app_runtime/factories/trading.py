from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.calendar import EconomicCalendarService
from src.clients.economic_calendar import (
    FredCalendarClient,
    TradingEconomicsCalendarClient,
)
from src.clients.economic_calendar_registry import ProviderRegistry
from src.config import EconomicConfig
from src.trading import TradingAccountRegistry, TradingModule

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradingComponents:
    economic_calendar_service: EconomicCalendarService
    trade_registry: TradingAccountRegistry
    trade_module: TradingModule
    default_account_alias: str


def build_provider_registry(settings: EconomicConfig) -> ProviderRegistry:
    """根据配置构建 Provider 注册表。"""
    registry = ProviderRegistry()
    if settings.tradingeconomics_enabled:
        registry.register(TradingEconomicsCalendarClient(settings))
    if settings.fred_enabled:
        registry.register(FredCalendarClient(settings))
    # FMP — 延迟导入，仅在启用时加载
    if settings.fmp_enabled:
        try:
            from src.clients.fmp_calendar import FmpCalendarClient

            registry.register(FmpCalendarClient(settings))
        except ImportError:
            logger.warning("FMP calendar client not available (src.clients.fmp_calendar)")
    # Jin10 — 延迟导入，仅在启用时加载（无需 API Key）
    if settings.jin10_enabled:
        try:
            from src.clients.jin10_calendar import Jin10CalendarClient

            registry.register(Jin10CalendarClient(settings))
        except ImportError:
            logger.warning("Jin10 calendar client not available (src.clients.jin10_calendar)")
    # Alpha Vantage — 延迟导入，仅在启用时加载
    if settings.alphavantage_enabled:
        try:
            from src.clients.alphavantage_calendar import AlphaVantageClient

            registry.register(AlphaVantageClient(settings))
        except ImportError:
            logger.warning("Alpha Vantage client not available (src.clients.alphavantage_calendar)")
    return registry


def build_trading_components(storage_writer, economic_settings) -> TradingComponents:
    registry = build_provider_registry(economic_settings)
    economic_calendar_service = EconomicCalendarService(
        db_writer=storage_writer.db,
        settings=economic_settings,
        storage_writer=storage_writer,
        provider_registry=registry,
    )
    trade_registry = TradingAccountRegistry(
        economic_calendar_service=economic_calendar_service
    )
    default_account_alias = trade_registry.default_account_alias()
    trade_module = TradingModule(
        registry=trade_registry,
        db_writer=storage_writer.db,
        active_account_alias=default_account_alias,
    )
    return TradingComponents(
        economic_calendar_service=economic_calendar_service,
        trade_registry=trade_registry,
        trade_module=trade_module,
        default_account_alias=default_account_alias,
    )
