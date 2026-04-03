from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel


class GoldImpactView(BaseModel):
    above_forecast: str
    below_forecast: str
    bullish_pct: Optional[float] = None
    avg_30m_range: Optional[float] = None
    sample_count: int = 0
    source: Optional[str] = None


class EnrichedCalendarItemView(BaseModel):
    event_uid: str
    event_name: str
    country: Optional[str] = None
    currency: Optional[str] = None
    importance: Optional[int] = None
    status: Optional[str] = None
    scheduled_at: str
    scheduled_at_local: Optional[str] = None
    countdown_minutes: float
    forecast: Optional[str] = None
    previous: Optional[str] = None
    actual: Optional[str] = None
    gold_impact: Optional[GoldImpactView] = None


class MarketImpactUpcomingItemView(BaseModel):
    event_uid: str
    event_name: str
    scheduled_at: str
    country: Optional[str] = None
    importance: Optional[int] = None
    status: Optional[str] = None
    impact_forecast: Optional[Dict[str, Any]] = None


class MarketImpactStatusView(BaseModel):
    enabled: bool

    model_config = {"extra": "allow"}


class MarketImpactStatsItemView(BaseModel):
    model_config = {"extra": "allow"}
