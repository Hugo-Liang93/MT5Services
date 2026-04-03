from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import get_economic_calendar_service
from src.api.schemas import ApiResponse
from src.calendar import EconomicCalendarService
from src.utils.timezone import utc_now
from .view_models import (
    EnrichedCalendarItemView,
    MarketImpactStatsItemView,
    MarketImpactStatusView,
    MarketImpactUpcomingItemView,
)

router = APIRouter(prefix="/economic", tags=["economic"])

_GOLD_IMPACT_RULES: list[tuple[list[str], str]] = [
    (["非农", "nonfarm", "non-farm", "payroll"], "strength"),
    (["gdp", "国内生产总值"], "strength"),
    (["零售", "retail"], "strength"),
    (["消费者信心", "consumer confidence", "consumer sentiment", "密歇根"], "strength"),
    (["ism", "制造业pmi", "服务业pmi", "pmi"], "strength"),
    (["耐用品", "durable"], "strength"),
    (["工业产出", "industrial production"], "strength"),
    (["新屋", "housing start", "building permit"], "strength"),
    (["adp就业", "adp"], "strength"),
    (["经常帐", "current account"], "strength"),
    (["贸易帐", "trade balance", "进出口"], "strength"),
    (["失业率", "unemployment"], "weakness"),
    (["初请失业金", "初请", "jobless claim", "申请失业金"], "weakness"),
    (["裁员", "layoff", "challenger"], "weakness"),
    (["cpi", "消费者物价", "消费物价", "未季调cpi"], "inflation"),
    (["ppi", "生产者物价", "生产物价"], "inflation"),
    (["pce", "个人消费支出"], "inflation"),
    (["进口物价", "import price"], "inflation"),
    (["利率决议", "interest rate", "联邦基金", "federal fund"], "rate"),
    (["原油库存", "crude oil inventor", "eia原油", "eia库存"], "weakness"),
    (["天然气库存", "natural gas"], "weakness"),
]
_TYPE_TO_IMPACT: dict[str, dict[str, str]] = {
    "strength": {"above_forecast": "利空", "below_forecast": "利多"},
    "weakness": {"above_forecast": "利多", "below_forecast": "利空"},
    "inflation": {"above_forecast": "利多", "below_forecast": "利空"},
    "rate": {"above_forecast": "利空", "below_forecast": "利多"},
}


def _gold_impact_fallback(event_name: str, currency: str) -> Optional[dict[str, Any]]:
    del currency
    name_lower = event_name.lower()
    for keywords, impact_type in _GOLD_IMPACT_RULES:
        if any(kw in name_lower for kw in keywords):
            direction = _TYPE_TO_IMPACT[impact_type]
            return {"above_forecast": direction["above_forecast"], "below_forecast": direction["below_forecast"], "bullish_pct": None, "avg_30m_range": None, "sample_count": 0}
    return None


@router.get("/calendar/enriched", response_model=ApiResponse[list[EnrichedCalendarItemView]])
def calendar_enriched(
    symbol: str = Query("XAUUSD"),
    hours: int = Query(48, ge=1, le=168),
    importance_min: int = Query(2, ge=1, le=3),
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
):
    now = utc_now()
    future_events = service.get_events(start_time=now - timedelta(hours=2), end_time=now + timedelta(hours=hours), importance_min=importance_min)
    recent_released = service.get_events(start_time=now - timedelta(days=7), end_time=now - timedelta(hours=2), importance_min=importance_min, statuses=["released"])
    seen_uids: set[str] = set()
    events = []
    for event in future_events + recent_released:
        if event.event_uid not in seen_uids:
            seen_uids.add(event.event_uid)
            events.append(event)
    analyzer = getattr(service, "market_impact_analyzer", None)
    results = []
    for event in events:
        scheduled = event.scheduled_at
        countdown = (scheduled - now).total_seconds() / 60 if scheduled > now else 0
        item = {
            "event_uid": event.event_uid,
            "event_name": event.event_name,
            "country": event.country,
            "currency": event.currency,
            "importance": event.importance,
            "status": event.status,
            "scheduled_at": scheduled.isoformat(),
            "scheduled_at_local": event.scheduled_at_local.isoformat() if event.scheduled_at_local else None,
            "countdown_minutes": round(countdown, 1),
            "forecast": event.forecast,
            "previous": event.previous,
            "actual": event.actual,
            "gold_impact": None,
        }
        impact = None
        if analyzer is not None:
            forecast_data = analyzer.get_impact_forecast(event.event_name, symbol=symbol)
            if forecast_data:
                bullish_pct = forecast_data.get("directional_bias")
                surprise_pos = forecast_data.get("avg_post_30m_change", 0) or 0
                impact = {
                    "above_forecast": "利多" if surprise_pos > 0 else "利空",
                    "below_forecast": "利空" if surprise_pos > 0 else "利多",
                    "bullish_pct": round(bullish_pct, 2) if bullish_pct is not None else None,
                    "avg_30m_range": forecast_data.get("avg_post_30m_range"),
                    "sample_count": forecast_data.get("sample_count", 0),
                    "source": "historical",
                }
        if impact is None:
            static = _gold_impact_fallback(event.event_name, event.currency)
            if static:
                impact = {**static, "source": "static"}
        item["gold_impact"] = impact
        results.append(item)
    results.sort(key=lambda x: x["scheduled_at"])
    return ApiResponse.success_response(
        data=[EnrichedCalendarItemView(**item) for item in results],
        metadata={"count": len(results), "symbol": symbol, "hours": hours, "importance_min": importance_min},
    )


def _get_market_impact_analyzer(service: EconomicCalendarService = Depends(get_economic_calendar_service)):
    analyzer = getattr(service, "market_impact_analyzer", None)
    if analyzer is None:
        raise HTTPException(status_code=503, detail="Market impact analyzer not enabled")
    return analyzer


@router.get("/calendar/market-impact/stats", response_model=ApiResponse[list[MarketImpactStatsItemView]])
def market_impact_stats(
    event_name: Optional[str] = None,
    country: Optional[str] = None,
    importance_min: Optional[int] = None,
    symbol: str = Query("XAUUSD"),
    timeframe: str = Query("M5"),
    limit: int = Query(50, ge=1, le=500),
    analyzer=Depends(_get_market_impact_analyzer),
):
    stats = analyzer.get_aggregated_stats(event_name=event_name, country=country, importance_min=importance_min, symbol=symbol, timeframe=timeframe, limit=limit)
    return ApiResponse.success_response(
        data=[MarketImpactStatsItemView(**item) for item in stats],
        metadata={"count": len(stats), "symbol": symbol, "timeframe": timeframe},
    )


@router.get("/calendar/market-impact/upcoming", response_model=ApiResponse[list[MarketImpactUpcomingItemView]])
def market_impact_upcoming(symbol: str = Query("XAUUSD"), hours: int = Query(24, ge=1, le=168), service: EconomicCalendarService = Depends(get_economic_calendar_service)):
    analyzer = getattr(service, "market_impact_analyzer", None)
    upcoming_events = service.get_high_impact_events(hours=hours)
    results = []
    for event in upcoming_events:
        item = {"event_uid": event.event_uid, "event_name": event.event_name, "scheduled_at": event.scheduled_at.isoformat(), "country": event.country, "importance": event.importance, "status": event.status, "impact_forecast": None}
        if analyzer is not None:
            item["impact_forecast"] = analyzer.get_impact_forecast(event.event_name, symbol=symbol)
        results.append(item)
    return ApiResponse.success_response(
        data=[MarketImpactUpcomingItemView(**item) for item in results],
        metadata={"count": len(results), "symbol": symbol, "hours": hours},
    )


@router.get("/calendar/market-impact/status", response_model=ApiResponse[MarketImpactStatusView])
def market_impact_status(service: EconomicCalendarService = Depends(get_economic_calendar_service)):
    analyzer = getattr(service, "market_impact_analyzer", None)
    if analyzer is None:
        return ApiResponse.success_response(data=MarketImpactStatusView(enabled=False))
    return ApiResponse.success_response(data=MarketImpactStatusView(**analyzer.stats()))


@router.get("/calendar/market-impact/{event_uid}")
def market_impact_detail(event_uid: str, symbol: str = Query("XAUUSD"), analyzer=Depends(_get_market_impact_analyzer)):
    result = analyzer.get_event_impact(event_uid, symbol)
    if result is None:
        return ApiResponse.success_response(data=None, metadata={"found": False})
    return ApiResponse.success_response(data=result, metadata={"found": True})
