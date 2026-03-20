from __future__ import annotations

from datetime import date, datetime, timezone
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
from src.clients.economic_calendar import EconomicCalendarError, EconomicCalendarEvent
from src.calendar import EconomicCalendarService

router = APIRouter(prefix="/economic", tags=["economic"])


def _normalize_datetime(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_model(event: EconomicCalendarEvent) -> EconomicCalendarEventModel:
    return EconomicCalendarEventModel(
        scheduled_at=event.scheduled_at.isoformat(),
        scheduled_at_local=event.scheduled_at_local.isoformat() if event.scheduled_at_local else None,
        local_timezone=event.local_timezone,
        scheduled_at_release=event.scheduled_at_release.isoformat() if event.scheduled_at_release else None,
        release_timezone=event.release_timezone,
        event_uid=event.event_uid,
        source=event.source,
        provider_event_id=event.provider_event_id,
        event_name=event.event_name,
        country=event.country,
        category=event.category,
        currency=event.currency,
        reference=event.reference,
        actual=event.actual,
        previous=event.previous,
        forecast=event.forecast,
        revised=event.revised,
        importance=event.importance,
        unit=event.unit,
        release_id=event.release_id,
        source_url=event.source_url,
        all_day=event.all_day,
        session_bucket=event.session_bucket,
        is_asia_session=event.is_asia_session,
        is_europe_session=event.is_europe_session,
        is_us_session=event.is_us_session,
        status=event.status,
        first_seen_at=event.first_seen_at.isoformat(),
        last_seen_at=event.last_seen_at.isoformat(),
        released_at=event.released_at.isoformat() if event.released_at else None,
        last_value_check_at=event.last_value_check_at.isoformat() if event.last_value_check_at else None,
        ingested_at=event.ingested_at.isoformat(),
        last_updated=event.last_updated.isoformat(),
    )


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
        metadata={
            "data_source": "economic_calendar_refresh",
            "job_type": job_type,
            "sources": sources or ["tradingeconomics", "fred"],
            "countries": countries,
        },
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
    items = service.get_events(
        _normalize_datetime(start_time),
        _normalize_datetime(end_time),
        limit=limit,
        sources=sources,
        countries=countries,
        currencies=currencies,
        session_buckets=sessions,
        statuses=statuses,
        importance_min=importance_min,
    )
    models = [_to_model(item) for item in items]
    return ApiResponse.success_response(
        data=models,
        metadata={
            "count": len(models),
            "sources": sources,
            "countries": countries,
            "currencies": currencies,
            "sessions": sessions,
            "statuses": statuses,
            "importance_min": importance_min,
            "time_range": {
                "start": start_time.isoformat() if start_time else None,
                "end": end_time.isoformat() if end_time else None,
                "first": models[0].scheduled_at if models else None,
                "last": models[-1].scheduled_at if models else None,
            },
            "data_source": "timescaledb",
            "data_freshness": "historical",
        },
    )


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
    items = service.get_upcoming(
        hours=hours,
        limit=limit,
        sources=sources,
        countries=countries,
        currencies=currencies,
        session_buckets=sessions,
        statuses=statuses,
        importance_min=importance_min,
    )
    models = [_to_model(item) for item in items]
    return ApiResponse.success_response(
        data=models,
        metadata={
            "count": len(models),
            "hours": hours,
            "sources": sources,
            "countries": countries,
            "currencies": currencies,
            "sessions": sessions,
            "statuses": statuses,
            "importance_min": importance_min,
            "data_source": "timescaledb",
            "data_freshness": "current",
        },
    )


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
    items = service.get_high_impact_events(
        hours=hours,
        limit=limit,
        sources=sources,
        countries=countries,
        currencies=currencies,
        session_buckets=sessions,
        statuses=statuses,
        importance_min=importance_min,
    )
    models = [_to_model(item) for item in items]
    return ApiResponse.success_response(
        data=models,
        metadata={
            "count": len(models),
            "hours": hours,
            "sources": sources,
            "countries": countries,
            "currencies": currencies,
            "sessions": sessions,
            "statuses": statuses,
            "importance_min": importance_min if importance_min is not None else service.settings.high_importance_threshold,
            "data_source": "timescaledb",
            "data_freshness": "current",
        },
    )


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
    items = service.get_curated_events(
        hours=hours,
        limit=limit,
        sources=sources,
        countries=countries,
        currencies=currencies,
        statuses=statuses,
        importance_min=importance_min,
        include_all_day=include_all_day,
    )
    models = [_to_model(item) for item in items]
    return ApiResponse.success_response(
        data=models,
        metadata={
            "count": len(models),
            "hours": hours,
            "sources": sources if sources is not None else service.settings.curated_sources,
            "countries": countries if countries is not None else service.settings.curated_countries,
            "currencies": currencies if currencies is not None else service.settings.curated_currencies,
            "statuses": statuses if statuses is not None else service.settings.curated_statuses,
            "importance_min": (
                importance_min if importance_min is not None else service.settings.curated_importance_min
            ),
            "include_all_day": (
                include_all_day if include_all_day is not None else service.settings.curated_include_all_day
            ),
            "data_source": "timescaledb",
            "data_freshness": "current",
        },
    )


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
    windows = service.get_risk_windows(
        hours=hours,
        limit=limit,
        sources=sources,
        countries=countries,
        currencies=currencies,
        session_buckets=sessions,
        statuses=statuses,
        importance_min=importance_min,
    )
    models = [EconomicCalendarRiskWindowModel(**item) for item in windows]
    return ApiResponse.success_response(
        data=models,
        metadata={
            "count": len(models),
            "hours": hours,
            "sources": sources,
            "countries": countries,
            "currencies": currencies,
            "sessions": sessions,
            "statuses": statuses,
            "importance_min": importance_min if importance_min is not None else service.settings.high_importance_threshold,
            "pre_event_buffer_minutes": service.settings.pre_event_buffer_minutes,
            "post_event_buffer_minutes": service.settings.post_event_buffer_minutes,
            "data_source": "timescaledb",
            "data_freshness": "current",
        },
    )


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
    windows = service.get_merged_risk_windows(
        hours=hours,
        limit=limit,
        sources=sources,
        countries=countries,
        currencies=currencies,
        session_buckets=sessions,
        statuses=statuses,
        importance_min=importance_min,
    )
    models = [EconomicCalendarMergedRiskWindowModel(**item) for item in windows]
    return ApiResponse.success_response(
        data=models,
        metadata={
            "count": len(models),
            "hours": hours,
            "sources": sources,
            "countries": countries,
            "currencies": currencies,
            "sessions": sessions,
            "statuses": statuses,
            "importance_min": importance_min if importance_min is not None else service.settings.high_importance_threshold,
            "pre_event_buffer_minutes": service.settings.pre_event_buffer_minutes,
            "post_event_buffer_minutes": service.settings.post_event_buffer_minutes,
            "data_source": "timescaledb",
            "data_freshness": "current",
        },
    )


@router.get("/calendar/status", response_model=ApiResponse[EconomicCalendarStatusModel])
def calendar_status(
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
) -> ApiResponse[EconomicCalendarStatusModel]:
    return ApiResponse.success_response(
        data=EconomicCalendarStatusModel(**service.stats()),
        metadata={"data_source": "economic_calendar_service"},
    )


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
    normalized_time = _normalize_datetime(at_time)
    result = service.get_trade_guard(
        symbol=symbol,
        at_time=normalized_time,
        lookahead_minutes=lookahead_minutes,
        lookback_minutes=lookback_minutes,
        limit=limit,
        importance_min=importance_min,
    )
    return ApiResponse.success_response(
        data=EconomicCalendarTradeGuardModel(**result),
        metadata={
            "data_source": "economic_calendar_service",
            "symbol": symbol,
            "lookahead_minutes": lookahead_minutes,
            "lookback_minutes": lookback_minutes,
        },
    )


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
    items = service.get_updates(
        start_time=_normalize_datetime(start_time),
        end_time=_normalize_datetime(end_time),
        limit=limit,
        event_uid=event_uid,
        snapshot_reasons=snapshot_reasons,
        job_types=job_types,
    )
    models = [EconomicCalendarUpdateModel(**item) for item in items]
    return ApiResponse.success_response(
        data=models,
        metadata={
            "count": len(models),
            "event_uid": event_uid,
            "snapshot_reasons": snapshot_reasons,
            "job_types": job_types,
            "data_source": "timescaledb",
            "data_freshness": "historical",
        },
    )
