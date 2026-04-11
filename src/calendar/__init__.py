from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import EconomicCalendarService

__all__ = ["EconomicCalendarService"]


def __getattr__(name: str):
    if name == "EconomicCalendarService":
        from .service import EconomicCalendarService

        return EconomicCalendarService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
