from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import get_economic_calendar_service
from src.api.schemas import (
    ApiResponse,
    EconomicCalendarEventModel,
    EconomicCalendarMergedRiskWindowModel,
    EconomicCalendarRefreshModel,
    EconomicCalendarRiskWindowModel,
    EconomicCalendarStatusModel,
    EconomicCalendarTradeGuardModel,
    EconomicCalendarUpdateModel,
)
from src.calendar import EconomicCalendarService
from src.clients.economic_calendar import EconomicCalendarError

from .common import normalize_datetime, to_model

router = APIRouter(prefix="/economic", tags=["economic"])


@router.post("/calendar/refresh", response_model=ApiResponse[EconomicCalendarRefreshModel])
def refresh_calendar(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    job_type: str = Query(default="calendar_sync", pattern="^(calendar_sync|near_term_sync|release_watch)$"),
    sources: Optional[List[str]] = Query(default=None),
    countries: Optional[List[str]] = Query(default=None),
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
) -> ApiResponse[EconomicCalendarRefreshModel]:
    try:
        result = service.refresh(start_date, end_date, countries=countries, sources=sources, job_type=job_type)
    except EconomicCalendarError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ApiResponse.success_response(
        data=EconomicCalendarRefreshModel(**result),
        metadata={"data_source": "economic_calendar_refresh", "job_type": job_type, "sources": sources or ["tradingeconomics", "fred"], "countries": countries},
    )


@router.get("/calendar", response_model=ApiResponse[List[EconomicCalendarEventModel]])
def calendar_history(
    start_time: Optional[datetime] = Query(default=None),
    end_time: Optional[datetime] = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=10000),
    sources: Optional[List[str]] = Query(default=None),
    countries: Optional[List[str]] = Query(default=None),
    currencies: Optional[List[str]] = Query(default=None),
    sessions: Optional[List[str]] = Query(default=None),
    statuses: Optional[List[str]] = Query(default=None),
    importance_min: Optional[int] = Query(default=None, ge=1, le=10),
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
) -> ApiResponse[List[EconomicCalendarEventModel]]:
    items = service.get_events(normalize_datetime(start_time), normalize_datetime(end_time), limit=limit, sources=sources, countries=countries, currencies=currencies, session_buckets=sessions, statuses=statuses, importance_min=importance_min)
    models = [to_model(item) for item in items]
    return ApiResponse.success_response(data=models, metadata={"count": len(models), "sources": sources, "countries": countries, "currencies": currencies, "sessions": sessions, "statuses": statuses, "importance_min": importance_min})


@router.get("/calendar/upcoming", response_model=ApiResponse[List[EconomicCalendarEventModel]])
def upcoming_calendar(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=200, ge=1, le=5000),
    sources: Optional[List[str]] = Query(default=None),
    countries: Optional[List[str]] = Query(default=None),
    currencies: Optional[List[str]] = Query(default=None),
    sessions: Optional[List[str]] = Query(default=None),
    statuses: Optional[List[str]] = Query(default=None),
    importance_min: Optional[int] = Query(default=None, ge=1, le=10),
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
) -> ApiResponse[List[EconomicCalendarEventModel]]:
    items = service.get_upcoming(hours=hours, limit=limit, sources=sources, countries=countries, currencies=currencies, session_buckets=sessions, statuses=statuses, importance_min=importance_min)
    return ApiResponse.success_response(data=[to_model(item) for item in items], metadata={"count": len(items), "hours": hours})


@router.get("/calendar/high-impact", response_model=ApiResponse[List[EconomicCalendarEventModel]])
def high_impact_calendar(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=200, ge=1, le=5000),
    sources: Optional[List[str]] = Query(default=None),
    countries: Optional[List[str]] = Query(default=None),
    currencies: Optional[List[str]] = Query(default=None),
    sessions: Optional[List[str]] = Query(default=None),
    statuses: Optional[List[str]] = Query(default=None),
    importance_min: Optional[int] = Query(default=None, ge=1, le=10),
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
) -> ApiResponse[List[EconomicCalendarEventModel]]:
    items = service.get_high_impact_events(hours=hours, limit=limit, sources=sources, countries=countries, currencies=currencies, session_buckets=sessions, statuses=statuses, importance_min=importance_min)
    return ApiResponse.success_response(data=[to_model(item) for item in items], metadata={"count": len(items), "hours": hours})


@router.get("/calendar/curated", response_model=ApiResponse[List[EconomicCalendarEventModel]])
def curated_calendar(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=200, ge=1, le=5000),
    sources: Optional[List[str]] = Query(default=None),
    countries: Optional[List[str]] = Query(default=None),
    currencies: Optional[List[str]] = Query(default=None),
    statuses: Optional[List[str]] = Query(default=None),
    importance_min: Optional[int] = Query(default=None, ge=1, le=10),
    include_all_day: Optional[bool] = Query(default=None),
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
) -> ApiResponse[List[EconomicCalendarEventModel]]:
    items = service.get_curated_events(hours=hours, limit=limit, sources=sources, countries=countries, currencies=currencies, statuses=statuses, importance_min=importance_min, include_all_day=include_all_day)
    return ApiResponse.success_response(data=[to_model(item) for item in items], metadata={"count": len(items), "hours": hours})


@router.get("/calendar/risk-windows", response_model=ApiResponse[List[EconomicCalendarRiskWindowModel]])
def calendar_risk_windows(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=200, ge=1, le=5000),
    sources: Optional[List[str]] = Query(default=None),
    countries: Optional[List[str]] = Query(default=None),
    currencies: Optional[List[str]] = Query(default=None),
    sessions: Optional[List[str]] = Query(default=None),
    statuses: Optional[List[str]] = Query(default=None),
    importance_min: Optional[int] = Query(default=None, ge=1, le=10),
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
) -> ApiResponse[List[EconomicCalendarRiskWindowModel]]:
    windows = service.get_risk_windows(hours=hours, limit=limit, sources=sources, countries=countries, currencies=currencies, session_buckets=sessions, statuses=statuses, importance_min=importance_min)
    return ApiResponse.success_response(data=[EconomicCalendarRiskWindowModel(**item) for item in windows], metadata={"count": len(windows), "hours": hours})


@router.get("/calendar/risk-windows/merged", response_model=ApiResponse[List[EconomicCalendarMergedRiskWindowModel]])
def merged_calendar_risk_windows(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=200, ge=1, le=5000),
    sources: Optional[List[str]] = Query(default=None),
    countries: Optional[List[str]] = Query(default=None),
    currencies: Optional[List[str]] = Query(default=None),
    sessions: Optional[List[str]] = Query(default=None),
    statuses: Optional[List[str]] = Query(default=None),
    importance_min: Optional[int] = Query(default=None, ge=1, le=10),
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
) -> ApiResponse[List[EconomicCalendarMergedRiskWindowModel]]:
    windows = service.get_merged_risk_windows(hours=hours, limit=limit, sources=sources, countries=countries, currencies=currencies, session_buckets=sessions, statuses=statuses, importance_min=importance_min)
    return ApiResponse.success_response(data=[EconomicCalendarMergedRiskWindowModel(**item) for item in windows], metadata={"count": len(windows), "hours": hours})


@router.get("/calendar/status", response_model=ApiResponse[EconomicCalendarStatusModel])
def calendar_status(service: EconomicCalendarService = Depends(get_economic_calendar_service)) -> ApiResponse[EconomicCalendarStatusModel]:
    return ApiResponse.success_response(data=EconomicCalendarStatusModel(**service.stats()), metadata={"data_source": "economic_calendar_service"})


@router.get("/calendar/trade-guard", response_model=ApiResponse[EconomicCalendarTradeGuardModel])
def calendar_trade_guard(
    symbol: str = Query(..., min_length=1),
    at_time: Optional[datetime] = Query(default=None),
    lookahead_minutes: int = Query(default=180, ge=1, le=24 * 60),
    lookback_minutes: int = Query(default=0, ge=0, le=24 * 60),
    limit: int = Query(default=500, ge=1, le=5000),
    importance_min: Optional[int] = Query(default=None, ge=1, le=10),
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
) -> ApiResponse[EconomicCalendarTradeGuardModel]:
    result = service.get_trade_guard(symbol=symbol, at_time=normalize_datetime(at_time), lookahead_minutes=lookahead_minutes, lookback_minutes=lookback_minutes, limit=limit, importance_min=importance_min)
    return ApiResponse.success_response(data=EconomicCalendarTradeGuardModel(**result), metadata={"data_source": "economic_calendar_service", "symbol": symbol})


@router.get("/calendar/updates", response_model=ApiResponse[List[EconomicCalendarUpdateModel]])
def calendar_updates(
    start_time: Optional[datetime] = Query(default=None),
    end_time: Optional[datetime] = Query(default=None),
    event_uid: Optional[str] = Query(default=None),
    snapshot_reasons: Optional[List[str]] = Query(default=None),
    job_types: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
) -> ApiResponse[List[EconomicCalendarUpdateModel]]:
    items = service.get_updates(start_time=normalize_datetime(start_time), end_time=normalize_datetime(end_time), limit=limit, event_uid=event_uid, snapshot_reasons=snapshot_reasons, job_types=job_types)
    return ApiResponse.success_response(data=[EconomicCalendarUpdateModel(**item) for item in items], metadata={"count": len(items), "event_uid": event_uid})
