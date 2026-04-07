from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol

if TYPE_CHECKING:
    from src.indicators.manager import UnifiedIndicatorManager


class IndicatorSource(Protocol):
    def get_indicator(self, symbol: str, timeframe: str, indicator_name: str) -> Dict[str, Any] | None:
        ...

    def get_all_indicators(self, symbol: str, timeframe: str) -> Dict[str, Dict[str, Any]]:
        ...

    def list_indicators(self) -> List[Dict[str, Any]]:
        ...

    def get_recent_bars(
        self,
        symbol: str,
        timeframe: str,
        *,
        end_time: Optional[datetime] = None,
        limit: int = 5,
    ) -> List[Any]:
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

    def get_recent_bars(
        self,
        symbol: str,
        timeframe: str,
        *,
        end_time: Optional[datetime] = None,
        limit: int = 5,
    ) -> List[Any]:
        market_service = getattr(self._manager, "market_service", None)
        if market_service is None:
            return []
        if end_time is not None and hasattr(market_service, "get_ohlc_window"):
            return list(
                market_service.get_ohlc_window(
                    symbol,
                    timeframe,
                    end_time=end_time,
                    limit=limit,
                )
            )
        if hasattr(market_service, "get_ohlc_closed"):
            return list(market_service.get_ohlc_closed(symbol, timeframe, limit=limit))
        return []
