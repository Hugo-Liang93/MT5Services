from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MarketStructureContext:
    symbol: str
    timeframe: str
    analyzed_at: datetime
    current_session: str
    close_price: float | None
    previous_day_high: float | None = None
    previous_day_low: float | None = None
    asia_range_high: float | None = None
    asia_range_low: float | None = None
    london_open_high: float | None = None
    london_open_low: float | None = None
    new_york_open_high: float | None = None
    new_york_open_low: float | None = None
    compression_state: str = "unknown"
    breakout_state: str = "none"
    reclaim_state: str = "none"
    sweep_state: str = "none"
    sweep_confirmation_state: str = "none"
    first_pullback_state: str = "none"
    structure_bias: str = "neutral"
    range_reference: str | None = None
    confirmation_reference: str | None = None
    pullback_reference: str | None = None
    breached_levels: tuple[str, ...] = ()
    swept_levels: tuple[str, ...] = ()
    recent_range_avg: float | None = None
    baseline_range_avg: float | None = None
    bars_used: int = 0
    source: str = "market_structure"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "analyzed_at": self.analyzed_at.isoformat(),
            "current_session": self.current_session,
            "close_price": self.close_price,
            "previous_day_high": self.previous_day_high,
            "previous_day_low": self.previous_day_low,
            "asia_range_high": self.asia_range_high,
            "asia_range_low": self.asia_range_low,
            "london_open_high": self.london_open_high,
            "london_open_low": self.london_open_low,
            "new_york_open_high": self.new_york_open_high,
            "new_york_open_low": self.new_york_open_low,
            "compression_state": self.compression_state,
            "breakout_state": self.breakout_state,
            "reclaim_state": self.reclaim_state,
            "sweep_state": self.sweep_state,
            "sweep_confirmation_state": self.sweep_confirmation_state,
            "first_pullback_state": self.first_pullback_state,
            "structure_bias": self.structure_bias,
            "range_reference": self.range_reference,
            "confirmation_reference": self.confirmation_reference,
            "pullback_reference": self.pullback_reference,
            "breached_levels": list(self.breached_levels),
            "swept_levels": list(self.swept_levels),
            "recent_range_avg": self.recent_range_avg,
            "baseline_range_avg": self.baseline_range_avg,
            "bars_used": self.bars_used,
            "source": self.source,
        }
