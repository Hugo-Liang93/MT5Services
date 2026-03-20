from __future__ import annotations

from dataclasses import dataclass

from src.calendar import EconomicCalendarService
from src.trading import TradingAccountRegistry, TradingModule


@dataclass(frozen=True)
class TradingComponents:
    economic_calendar_service: EconomicCalendarService
    trade_registry: TradingAccountRegistry
    trade_module: TradingModule
    default_account_alias: str


def build_trading_components(storage_writer, economic_settings) -> TradingComponents:
    economic_calendar_service = EconomicCalendarService(
        db_writer=storage_writer.db,
        settings=economic_settings,
        storage_writer=storage_writer,
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
