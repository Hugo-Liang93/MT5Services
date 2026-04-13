from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.market import MarketDataService


class ConfirmedIndicatorSource:
    """从共享持久化 OHLC 中读取最近一根确认 K 线的指标快照。"""

    def __init__(self, market_service: MarketDataService) -> None:
        self._market_service = market_service

    def get_indicator(
        self,
        symbol: str,
        timeframe: str,
        indicator_name: str,
    ) -> dict[str, Any] | None:
        indicators = self.get_all_indicators(symbol, timeframe)
        payload = indicators.get(indicator_name)
        if payload is None:
            return None
        if isinstance(payload, Mapping):
            return dict(payload)
        return {"value": payload}

    def get_all_indicators(
        self,
        symbol: str,
        timeframe: str,
    ) -> dict[str, dict[str, Any]]:
        latest_bar = self._market_service.get_latest_ohlc(symbol, timeframe)
        if latest_bar is None:
            return {}
        raw = getattr(latest_bar, "indicators", None) or {}
        if not isinstance(raw, Mapping):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for name, payload in raw.items():
            key = str(name).strip()
            if not key:
                continue
            if isinstance(payload, Mapping):
                normalized[key] = dict(payload)
            else:
                normalized[key] = {"value": payload}
        return normalized
