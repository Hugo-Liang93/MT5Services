from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.clients.economic_calendar import EconomicCalendarEvent

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
    start = event.scheduled_at - timedelta(minutes=service.settings.pre_event_buffer_minutes)
    end = event.scheduled_at + timedelta(minutes=service.settings.post_event_buffer_minutes)
    return start, end


def build_window(service, event: EconomicCalendarEvent) -> Dict[str, Any]:
    window_start, window_end = compute_window_bounds(service, event)
    return {
        "event_uid": event.event_uid,
        "event_name": event.event_name,
        "source": event.source,
        "country": event.country,
        "currency": event.currency,
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
    return {
        "symbol": symbol,
        "evaluation_time": evaluation_time.isoformat(),
        "blocked": bool(active_windows),
        "currencies": context["currencies"],
        "countries": context["countries"],
        "active_windows": active_windows,
        "upcoming_windows": upcoming_windows,
        "importance_min": importance_min if importance_min is not None else service.settings.high_importance_threshold,
    }
