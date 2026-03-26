from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, List, Optional

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


# ── XAUUSD 静态影响知识库 ─────────────────────────────────────────────
#
# 当 MarketImpactAnalyzer 历史样本不足时，回退到这里。
# 逻辑：对于 USD 计价的黄金（XAUUSD），美元走强 → 金价利空，反之利多。
#
# "strength" 类（GDP/NFP/零售/消费者信心/ISM）：
#   实际 > 预测 → 经济更强 → 美元走强 → 黄金利空
# "weakness" 类（失业率/初请失业金）：
#   实际 > 预测 → 经济更弱 → 美元走弱 → 黄金利多
# "inflation" 类（CPI/PPI/PCE）：
#   实际 > 预测 → 通胀更高 → 黄金作为抗通胀资产 → 利多
# "rate" 类（联邦基金利率/央行利率）：
#   实际 > 预测 → 利率更高 → 持金机会成本上升 → 利空

# (event_name_keyword, type) — keyword 匹配事件名（中英文关键词）
_GOLD_IMPACT_RULES: list[tuple[list[str], str]] = [
    # 经济强度指标 — 强于预期 → 利空黄金
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
    # 经济弱势指标 — 高于预期 → 利多黄金
    (["失业率", "unemployment"], "weakness"),
    (["初请失业金", "初请", "jobless claim", "申请失业金"], "weakness"),
    (["裁员", "layoff", "challenger"], "weakness"),
    # 通胀指标 — 高于预期 → 利多黄金
    (["cpi", "消费者物价", "消费物价", "未季调cpi"], "inflation"),
    (["ppi", "生产者物价", "生产物价"], "inflation"),
    (["pce", "个人消费支出"], "inflation"),
    (["进口物价", "import price"], "inflation"),
    # 利率指标 — 高于预期 → 利空黄金
    (["利率决议", "interest rate", "联邦基金", "federal fund"], "rate"),
    # 原油库存 — 库存增加 → 经济放缓预期 → 美元弱 → 利多黄金
    (["原油库存", "crude oil inventor", "eia原油", "eia库存"], "weakness"),
    (["天然气库存", "natural gas"], "weakness"),
]

_TYPE_TO_IMPACT: dict[str, dict[str, str]] = {
    "strength": {"above_forecast": "利空", "below_forecast": "利多"},
    "weakness":  {"above_forecast": "利多", "below_forecast": "利空"},
    "inflation": {"above_forecast": "利多", "below_forecast": "利空"},
    "rate":      {"above_forecast": "利空", "below_forecast": "利多"},
}


def _gold_impact_fallback(
    event_name: str, currency: str
) -> Optional[dict[str, Any]]:
    """静态知识库：根据事件名关键词匹配黄金影响方向。"""
    name_lower = event_name.lower()
    for keywords, impact_type in _GOLD_IMPACT_RULES:
        if any(kw in name_lower for kw in keywords):
            direction = _TYPE_TO_IMPACT[impact_type]
            return {
                "above_forecast": direction["above_forecast"],
                "below_forecast": direction["below_forecast"],
                "bullish_pct": None,
                "avg_30m_range": None,
                "sample_count": 0,
            }
    return None


# ── 前端日历面板精简端点 ──────────────────────────────────────────────


@router.get("/calendar/enriched")
def calendar_enriched(
    symbol: str = Query("XAUUSD"),
    hours: int = Query(48, ge=1, le=168),
    importance_min: int = Query(2, ge=1, le=3),
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
):
    """精简日历：事件基本信息 + 对黄金的历史方向影响。

    返回前端日历面板所需的核心维度：
    - 距当前时间（countdown_minutes）
    - 预测值 / 前值 / 实际值
    - 事件影响程度
    - 实际 > 预测 / 实际 < 预测 对黄金的历史影响方向
    """
    from datetime import timedelta
    from src.utils.timezone import utc_now

    now = utc_now()
    # 未来事件
    future_events = service.get_events(
        start_time=now - timedelta(hours=2),  # 含近2小时可能刚公布的
        end_time=now + timedelta(hours=hours),
        importance_min=importance_min,
    )
    # 最近已公布事件（有 actual 值，用于复盘参考）
    recent_released = service.get_events(
        start_time=now - timedelta(days=7),
        end_time=now - timedelta(hours=2),
        importance_min=importance_min,
        statuses=["released"],
    )
    # 合并去重
    seen_uids: set[str] = set()
    events = []
    for e in future_events + recent_released:
        if e.event_uid not in seen_uids:
            seen_uids.add(e.event_uid)
            events.append(e)
    analyzer = getattr(service, "market_impact_analyzer", None)

    results = []
    for e in events:
        scheduled = e.scheduled_at
        countdown = (scheduled - now).total_seconds() / 60 if scheduled > now else 0

        # 基本信息
        item = {
            "event_uid": e.event_uid,
            "event_name": e.event_name,
            "country": e.country,
            "currency": e.currency,
            "importance": e.importance,
            "status": e.status,
            "scheduled_at": scheduled.isoformat(),
            "scheduled_at_local": e.scheduled_at_local.isoformat() if e.scheduled_at_local else None,
            "countdown_minutes": round(countdown, 1),
            "forecast": e.forecast,
            "previous": e.previous,
            "actual": e.actual,
            "gold_impact": None,
        }

        # 黄金影响方向：优先用历史统计，无数据时回退到静态知识库
        impact = None
        if analyzer is not None:
            forecast_data = analyzer.get_impact_forecast(e.event_name, symbol=symbol)
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
        # 回退到静态知识库
        if impact is None:
            static = _gold_impact_fallback(e.event_name, e.currency)
            if static:
                impact = {**static, "source": "static"}
        item["gold_impact"] = impact

        results.append(item)

    # 按 scheduled_at 排序（最近的在前）
    results.sort(key=lambda x: x["scheduled_at"])

    return ApiResponse.success_response(
        data=results,
        metadata={
            "count": len(results),
            "symbol": symbol,
            "hours": hours,
            "importance_min": importance_min,
        },
    )


# ── Market Impact 行情影响统计端点 ────────────────────────────────────


def _get_market_impact_analyzer(
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
):
    analyzer = getattr(service, "market_impact_analyzer", None)
    if analyzer is None:
        raise HTTPException(status_code=503, detail="Market impact analyzer not enabled")
    return analyzer


@router.get("/calendar/market-impact/stats")
def market_impact_stats(
    event_name: Optional[str] = None,
    country: Optional[str] = None,
    importance_min: Optional[int] = None,
    symbol: str = Query("XAUUSD"),
    timeframe: str = Query("M5"),
    limit: int = Query(50, ge=1, le=500),
    analyzer=Depends(_get_market_impact_analyzer),
):
    """聚合统计：按事件类型分组的历史行情影响。"""
    stats = analyzer.get_aggregated_stats(
        event_name=event_name,
        country=country,
        importance_min=importance_min,
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
    )
    return ApiResponse.success_response(
        data=stats,
        metadata={"count": len(stats), "symbol": symbol, "timeframe": timeframe},
    )


@router.get("/calendar/market-impact/upcoming")
def market_impact_upcoming(
    symbol: str = Query("XAUUSD"),
    hours: int = Query(24, ge=1, le=168),
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
):
    """即将到来的事件 + 基于历史统计的预期影响。"""
    analyzer = getattr(service, "market_impact_analyzer", None)
    upcoming_events = service.get_high_impact_events(hours=hours)
    results = []
    for event in upcoming_events:
        item = {
            "event_uid": event.event_uid,
            "event_name": event.event_name,
            "scheduled_at": event.scheduled_at.isoformat(),
            "country": event.country,
            "importance": event.importance,
            "status": event.status,
            "impact_forecast": None,
        }
        if analyzer is not None:
            forecast = analyzer.get_impact_forecast(event.event_name, symbol=symbol)
            item["impact_forecast"] = forecast
        results.append(item)
    return ApiResponse.success_response(
        data=results,
        metadata={"count": len(results), "symbol": symbol, "hours": hours},
    )


@router.get("/calendar/market-impact/status")
def market_impact_status(
    service: EconomicCalendarService = Depends(get_economic_calendar_service),
):
    """Market Impact Analyzer 运行时状态。"""
    analyzer = getattr(service, "market_impact_analyzer", None)
    if analyzer is None:
        return ApiResponse.success_response(data={"enabled": False})
    return ApiResponse.success_response(data=analyzer.stats())


@router.get("/calendar/market-impact/{event_uid}")
def market_impact_detail(
    event_uid: str,
    symbol: str = Query("XAUUSD"),
    analyzer=Depends(_get_market_impact_analyzer),
):
    """查询单个事件对指定品种的行情影响详情。"""
    result = analyzer.get_event_impact(event_uid, symbol)
    if result is None:
        return ApiResponse.success_response(data=None, metadata={"found": False})
    return ApiResponse.success_response(data=result, metadata={"found": True})
