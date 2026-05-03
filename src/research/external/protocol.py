"""ExternalDataSource Protocol + DailyBar value type + source registry.

Extension contract:
1. Implement a class with attrs `name: str`, `fetch_daily(symbol, *, start, end)
   -> List[DailyBar]`, `supports_symbol(symbol) -> bool`.
2. Call `register_source("your_name", lambda: YourClient())` at import time.
3. The generic backfill CLI then accepts `--source your_name`.

`runtime_checkable` lets `isinstance(obj, ExternalDataSource)` validate
implementations at runtime — convenient for dynamic source discovery.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, List

from typing_extensions import Protocol, runtime_checkable


class UnknownSourceError(KeyError):
    """Raised when get_source() is called with an unregistered name."""


@dataclass(frozen=True)
class DailyBar:
    """Daily OHLCV value type — single-bar contract across all data sources."""

    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@runtime_checkable
class ExternalDataSource(Protocol):
    """Any source that can fetch daily OHLCV for a symbol over a date range."""

    name: str

    def fetch_daily(
        self, symbol: str, *, start: date, end: date
    ) -> List[DailyBar]: ...

    def supports_symbol(self, symbol: str) -> bool: ...


_REGISTRY: Dict[str, Callable[[], ExternalDataSource]] = {}


def register_source(name: str, factory: Callable[[], ExternalDataSource]) -> None:
    """Register a factory under `name`. Re-registering overwrites the previous factory.

    The factory must be a zero-arg callable that returns a fresh ExternalDataSource
    instance — passing an instance directly is a common mistake we catch here.
    """
    if not callable(factory):
        raise TypeError(
            f"register_source({name!r}): factory must be callable, "
            f"got {type(factory).__name__}"
        )
    _REGISTRY[name] = factory


def get_source(name: str) -> ExternalDataSource:
    """Instantiate the source registered under `name`. Raises UnknownSourceError."""
    if name not in _REGISTRY:
        raise UnknownSourceError(
            f"data source '{name}' not registered. "
            f"Currently registered: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]()


def list_registered_sources() -> List[str]:
    """Return all registered source names (sorted, for stable display)."""
    return sorted(_REGISTRY.keys())
