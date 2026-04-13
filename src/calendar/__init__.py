from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import EconomicCalendarService
    from .read_only_provider import ReadOnlyEconomicCalendarProvider

__all__ = ["EconomicCalendarService", "ReadOnlyEconomicCalendarProvider"]


def __getattr__(name: str):
    if name == "EconomicCalendarService":
        from .service import EconomicCalendarService

        return EconomicCalendarService
    if name == "ReadOnlyEconomicCalendarProvider":
        from .read_only_provider import ReadOnlyEconomicCalendarProvider

        return ReadOnlyEconomicCalendarProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
