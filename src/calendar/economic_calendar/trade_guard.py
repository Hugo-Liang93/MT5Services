from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.clients.economic_calendar import EconomicCalendarEvent

from .gold_relevance import (
    EventRelevanceMatcher,
    EventSummary,
    GoldRelevancePolicy,
    build_relevance_matcher,
)


def _build_relevance_matcher_from_settings(settings: Any) -> Optional[EventRelevanceMatcher]:
    """从 settings 契约构造相关性匹配器。

    settings 必须提供三个字段：
      - trade_guard_relevance_filter_enabled: bool
      - gold_impact_keywords: str (CSV)
      - gold_impact_categories: str (CSV)

    设计：
      - relevance_filter 未启用 → 返回 None（不过滤）
      - 启用但 policy 为空 → raise ValueError（配置矛盾，调用方应修复配置）
      - 启用且 policy 非空 → 返回 matcher
    """
    if not settings.trade_guard_relevance_filter_enabled:
        return None
    policy = GoldRelevancePolicy.from_csv(
        keywords_csv=settings.gold_impact_keywords,
        categories_csv=settings.gold_impact_categories,
    )
    return build_relevance_matcher(policy)

_CURRENCY_TO_COUNTRY = {
    "AUD": "Australia",
    "CAD": "Canada",
    "CHF": "Switzerland",
    "CNY": "China",
    "EUR": "Euro Area",
    "GBP": "United Kingdom",
    "JPY": "Japan",
    "NZD": "New Zealand",
    "USD": "United States",
}

_INDEX_SYMBOL_COUNTRY_HINTS = {
    "US": "United States",
    "DE": "Germany",
    "GER": "Germany",
    "UK": "United Kingdom",
    "JP": "Japan",
    "HK": "China",
    "CN": "China",
    "AU": "Australia",
}


def _utc_now() -> datetime:
    from src.calendar.service import _utc_now as _service_utc_now

    return _service_utc_now()


def compute_window_bounds(service, event: EconomicCalendarEvent) -> tuple[datetime, datetime]:
    pre_buffer = service.settings.pre_event_buffer_minutes
    post_buffer = service.settings.post_event_buffer_minutes

    # 动态扩大保护窗口：如果有 MarketImpactAnalyzer 且历史数据显示高波动
    analyzer = getattr(service, "market_impact_analyzer", None)
    if analyzer is not None and event.event_name:
        # 阈值从 settings 读取，可通过 economic.ini [market_impact] 配置化
        high_spike = getattr(service.settings, "market_impact_high_spike_threshold", 3.0)
        med_spike = getattr(service.settings, "market_impact_med_spike_threshold", 2.0)
        high_buffer = getattr(service.settings, "market_impact_high_spike_buffer_minutes", 60)
        med_buffer = getattr(service.settings, "market_impact_med_spike_buffer_minutes", 45)
        try:
            forecast = analyzer.get_impact_forecast(event.event_name)
            if forecast:
                spike = forecast.get("expected_volatility_spike")
                if spike is not None and spike > high_spike:
                    pre_buffer = max(pre_buffer, high_buffer)
                    post_buffer = max(post_buffer, high_buffer)
                elif spike is not None and spike > med_spike:
                    pre_buffer = max(pre_buffer, med_buffer)
                    post_buffer = max(post_buffer, med_buffer)
        except Exception:
            pass

    start = event.scheduled_at - timedelta(minutes=pre_buffer)
    end = event.scheduled_at + timedelta(minutes=post_buffer)
    return start, end


def build_window(service, event: EconomicCalendarEvent) -> Dict[str, Any]:
    window_start, window_end = compute_window_bounds(service, event)
    return {
        "event_uid": event.event_uid,
        "event_name": event.event_name,
        "source": event.source,
        "country": event.country,
        "currency": event.currency,
        "category": getattr(event, "category", None),
        "importance": event.importance,
        "session_bucket": event.session_bucket,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "scheduled_at": event.scheduled_at.isoformat(),
        "scheduled_at_local": event.scheduled_at_local.isoformat() if event.scheduled_at_local else None,
        "scheduled_at_release": event.scheduled_at_release.isoformat() if event.scheduled_at_release else None,
    }


def normalize_symbol(symbol: str) -> str:
    return "".join(ch for ch in (symbol or "").upper() if ch.isalnum())


def infer_symbol_context(symbol: str) -> Dict[str, List[str]]:
    normalized = normalize_symbol(symbol)
    currencies: set[str] = set()
    countries: set[str] = set()
    if len(normalized) >= 6 and normalized[:6].isalpha():
        maybe_base = normalized[:3]
        maybe_quote = normalized[3:6]
        if maybe_base in _CURRENCY_TO_COUNTRY and maybe_quote in _CURRENCY_TO_COUNTRY:
            currencies.update([maybe_base, maybe_quote])
    for currency, country in _CURRENCY_TO_COUNTRY.items():
        if currency in normalized:
            currencies.add(currency)
            countries.add(country)
    for prefix, country in _INDEX_SYMBOL_COUNTRY_HINTS.items():
        if normalized.startswith(prefix):
            countries.add(country)
    if not countries:
        for currency in currencies:
            countries.add(_CURRENCY_TO_COUNTRY[currency])
    return {
        "symbol": symbol,
        "normalized_symbol": normalized,
        "currencies": sorted(currencies),
        "countries": sorted(countries),
    }


def window_to_datetimes(window: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **window,
        "window_start_dt": datetime.fromisoformat(window["window_start"]),
        "window_end_dt": datetime.fromisoformat(window["window_end"]),
    }


def merge_windows(windows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not windows:
        return []
    normalized = sorted((window_to_datetimes(window) for window in windows), key=lambda item: item["window_start_dt"])
    merged: List[Dict[str, Any]] = []
    for window in normalized:
        if not merged or window["window_start_dt"] > merged[-1]["window_end_dt"]:
            merged.append(
                {
                    "window_start_dt": window["window_start_dt"],
                    "window_end_dt": window["window_end_dt"],
                    "event_uids": [window["event_uid"]],
                    "event_names": [window["event_name"]],
                    "sources": sorted({window["source"]} if window["source"] else set()),
                    "countries": sorted({window["country"]} if window["country"] else set()),
                    "currencies": sorted({window["currency"]} if window["currency"] else set()),
                    "categories": sorted(
                        {window.get("category")} if window.get("category") else set()
                    ),
                    "sessions": sorted({window["session_bucket"]} if window["session_bucket"] else set()),
                    "max_importance": window["importance"],
                }
            )
            continue
        current = merged[-1]
        if window["window_end_dt"] > current["window_end_dt"]:
            current["window_end_dt"] = window["window_end_dt"]
        current["event_uids"].append(window["event_uid"])
        current["event_names"].append(window["event_name"])
        if window["source"]:
            current["sources"] = sorted(set(current["sources"]) | {window["source"]})
        if window["country"]:
            current["countries"] = sorted(set(current["countries"]) | {window["country"]})
        if window["currency"]:
            current["currencies"] = sorted(set(current["currencies"]) | {window["currency"]})
        if window.get("category"):
            current["categories"] = sorted(
                set(current.get("categories", [])) | {window["category"]}
            )
        if window["session_bucket"]:
            current["sessions"] = sorted(set(current["sessions"]) | {window["session_bucket"]})
        if window["importance"] is not None:
            current["max_importance"] = (
                window["importance"]
                if current["max_importance"] is None
                else max(current["max_importance"], window["importance"])
            )
    return [
        {
            "window_start": item["window_start_dt"].isoformat(),
            "window_end": item["window_end_dt"].isoformat(),
            "event_count": len(item["event_uids"]),
            "event_uids": item["event_uids"],
            "event_names": item["event_names"],
            "sources": item["sources"],
            "countries": item["countries"],
            "currencies": item["currencies"],
            "categories": item.get("categories", []),
            "sessions": item["sessions"],
            "max_importance": item["max_importance"],
        }
        for item in merged
    ]


def get_risk_windows(
    service,
    hours: int = 24,
    limit: int = 200,
    sources: Optional[List[str]] = None,
    countries: Optional[List[str]] = None,
    currencies: Optional[List[str]] = None,
    session_buckets: Optional[List[str]] = None,
    statuses: Optional[List[str]] = None,
    importance_min: Optional[int] = None,
) -> List[Dict[str, Any]]:
    events = service.get_high_impact_events(
        hours=hours,
        limit=limit,
        sources=sources,
        countries=countries,
        currencies=currencies,
        session_buckets=session_buckets,
        statuses=statuses,
        importance_min=importance_min,
    )
    return [build_window(service, event) for event in events]


def get_merged_risk_windows(service, **kwargs) -> List[Dict[str, Any]]:
    return merge_windows(get_risk_windows(service, **kwargs))


def get_trade_guard(
    service,
    symbol: str,
    at_time: Optional[datetime] = None,
    lookahead_minutes: int = 180,
    lookback_minutes: int = 0,
    limit: int = 500,
    importance_min: Optional[int] = None,
) -> Dict[str, Any]:
    service._ensure_worker_running()
    evaluation_time = at_time or _utc_now()
    if evaluation_time.tzinfo is None:
        evaluation_time = evaluation_time.replace(tzinfo=timezone.utc)
    else:
        evaluation_time = evaluation_time.astimezone(timezone.utc)
    context = infer_symbol_context(symbol)
    events = service.get_events(
        start_time=evaluation_time - timedelta(minutes=max(0, lookback_minutes)),
        end_time=evaluation_time + timedelta(minutes=max(0, lookahead_minutes)),
        limit=limit,
        countries=context["countries"] or None,
        currencies=context["currencies"] or None,
        statuses=["scheduled", "imminent", "pending_release", "released"],
        importance_min=importance_min if importance_min is not None else service.settings.high_importance_threshold,
    )
    merged_windows = merge_windows([build_window(service, event) for event in events])
    active_windows = [
        window
        for window in merged_windows
        if datetime.fromisoformat(window["window_start"]) <= evaluation_time <= datetime.fromisoformat(window["window_end"])
    ]
    upcoming_windows = [
        window
        for window in merged_windows
        if evaluation_time < datetime.fromisoformat(window["window_start"])
    ]
    # 品种相关性过滤：非直接相关事件 importance 降 1 级
    matcher = _build_relevance_matcher_from_settings(service.settings)

    def _effective_importance(window: Dict[str, Any]) -> int:
        raw_imp = window.get("max_importance") or 0
        if matcher is None:
            return raw_imp
        # merge_windows 已聚合 event_names + categories（多事件合并窗口）；
        # 任一 (name, category) 组合被判定相关即视为相关窗口。
        names: List[str] = list(window.get("event_names") or [])
        if not names:
            single = window.get("event_name")
            if single:
                names = [str(single)]
        categories: List[Optional[str]] = [
            str(c) for c in (window.get("categories") or [])
        ]
        if not categories:
            categories = [None]
        for name in names:
            for cat in categories:
                try:
                    event = EventSummary(name=name, category=cat)
                except ValueError:
                    continue  # name 空串等非法输入不算相关
                if matcher.is_relevant(event):
                    return raw_imp
        return max(0, raw_imp - 1)

    # 分级压制：区分 block 和 warn
    block_min: int = service.settings.trade_guard_block_importance_min
    block_windows = [w for w in active_windows if _effective_importance(w) >= block_min]
    warn_windows = [w for w in active_windows if _effective_importance(w) < block_min and _effective_importance(w) >= 1]
    # 低 importance 事件使用更窄的保护窗口
    # block 窗口 = scheduled_at ± pre/post_event_buffer（已由 build_window 计算）
    # warn 窗口 = scheduled_at ± warn_pre/post_buffer（更窄，需要缩小已有窗口）
    if warn_windows:
        warn_pre: int = service.settings.trade_guard_warn_pre_buffer_minutes
        warn_post: int = service.settings.trade_guard_warn_post_buffer_minutes
        full_pre: int = service.settings.pre_event_buffer_minutes
        full_post: int = service.settings.post_event_buffer_minutes
        # 从 block 窗口边界向内收缩到 warn 窗口
        shrink_start = timedelta(minutes=full_pre - warn_pre)
        shrink_end = timedelta(minutes=full_post - warn_post)
        warn_windows = [
            w for w in warn_windows
            if (datetime.fromisoformat(w["window_start"]) + shrink_start)
            <= evaluation_time
            <= (datetime.fromisoformat(w["window_end"]) - shrink_end)
        ]
    return {
        "symbol": symbol,
        "evaluation_time": evaluation_time.isoformat(),
        "blocked": bool(block_windows),
        "warned": bool(warn_windows),
        "severity": "block" if block_windows else ("warn" if warn_windows else "none"),
        "currencies": context["currencies"],
        "countries": context["countries"],
        "active_windows": active_windows,
        "block_windows": block_windows,
        "warn_windows": warn_windows,
        "upcoming_windows": upcoming_windows,
        "importance_min": importance_min if importance_min is not None else service.settings.high_importance_threshold,
        "block_importance_min": block_min,
    }
