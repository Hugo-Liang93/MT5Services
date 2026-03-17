from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Protocol

if TYPE_CHECKING:
    from src.indicators.manager import UnifiedIndicatorManager


class IndicatorSource(Protocol):
    def get_indicator(self, symbol: str, timeframe: str, indicator_name: str) -> Dict[str, Any] | None:
        ...

    def get_all_indicators(self, symbol: str, timeframe: str) -> Dict[str, Dict[str, Any]]:
        ...

    def list_indicators(self) -> List[Dict[str, Any]]:
        ...


class UnifiedIndicatorSourceAdapter:
    """Adapter to keep signal module decoupled from indicator manager internals."""

    def __init__(self, manager: "UnifiedIndicatorManager"):
        self._manager = manager

    def get_indicator(self, symbol: str, timeframe: str, indicator_name: str) -> Dict[str, Any] | None:
        return self._manager.get_indicator(symbol, timeframe, indicator_name)

    def get_all_indicators(self, symbol: str, timeframe: str) -> Dict[str, Dict[str, Any]]:
        return self._manager.get_all_indicators(symbol, timeframe)

    def list_indicators(self) -> List[Dict[str, Any]]:
        return self._manager.list_indicators()
