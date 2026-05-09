from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class TickFeatureConfig:
    window_seconds: float = 5.0
    emit_interval_seconds: float = 1.0
    max_quote_age_ms: int = 1500
    max_spread_points: float = 30.0
    min_ticks_per_window: int = 3
    point_size: float = 0.00001
    max_snapshot_age_seconds: float = 3.0
    bus_maxlen: int = 4096
    point_size_by_symbol: dict[str, float] = field(default_factory=dict)
    max_spread_points_by_symbol: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "point_size_by_symbol",
            _normalize_symbol_float_map(self.point_size_by_symbol),
        )
        object.__setattr__(
            self,
            "max_spread_points_by_symbol",
            _normalize_symbol_float_map(self.max_spread_points_by_symbol),
        )

    def point_size_for(self, symbol: str) -> float:
        return float(
            self.point_size_by_symbol.get(str(symbol or "").strip().upper())
            or self.point_size
        )

    def max_spread_points_for(self, symbol: str) -> float:
        return float(
            self.max_spread_points_by_symbol.get(str(symbol or "").strip().upper())
            or self.max_spread_points
        )


@dataclass(frozen=True)
class TickFeatureSnapshot:
    symbol: str
    window_start_msc: int
    window_end_msc: int
    generated_at: datetime
    tick_count: int
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    mid: Optional[float]
    spread_points: Optional[float]
    quote_age_ms: Optional[int]
    realized_range_points: Optional[float]
    price_change_points: Optional[float]
    buy_pressure: Optional[float]
    sell_pressure: Optional[float]
    status: str
    reasons: tuple[str, ...]


def _normalize_symbol_float_map(raw: dict[str, float]) -> dict[str, float]:
    return {
        str(symbol).strip().upper(): float(value)
        for symbol, value in dict(raw or {}).items()
        if str(symbol).strip()
    }
