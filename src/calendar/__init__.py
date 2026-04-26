from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .economic_calendar.trade_guard import (
        get_trade_guard,
        infer_symbol_context,
    )
    from .economic_decay import EconomicDecayService
    from .read_only_provider import ReadOnlyEconomicCalendarProvider
    from .service import EconomicCalendarService

__all__ = [
    "EconomicCalendarService",
    "EconomicDecayService",
    "ReadOnlyEconomicCalendarProvider",
    "get_trade_guard",
    "infer_symbol_context",
]


def __getattr__(name: str):
    if name == "EconomicCalendarService":
        from .service import EconomicCalendarService

        return EconomicCalendarService
    if name == "EconomicDecayService":
        from .economic_decay import EconomicDecayService

        return EconomicDecayService
    if name == "ReadOnlyEconomicCalendarProvider":
        from .read_only_provider import ReadOnlyEconomicCalendarProvider

        return ReadOnlyEconomicCalendarProvider
    if name == "infer_symbol_context":
        from .economic_calendar.trade_guard import infer_symbol_context

        return infer_symbol_context
    if name == "get_trade_guard":
        from .economic_calendar.trade_guard import get_trade_guard

        return get_trade_guard
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
