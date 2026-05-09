"""Execution-facing tick feature health projection."""

from __future__ import annotations

from typing import Any, Mapping


def _safe_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _blocking_status(status: str) -> bool:
    return status in {"missing", "unavailable", "stale", "blocked", "sparse", "critical"}


def normalize_tick_feature_health_snapshot(
    snapshot: Mapping[str, Any],
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    raw = _safe_dict(snapshot)
    if not raw:
        return {
            "status": "unavailable",
            "blocking": True,
            "symbol": symbol,
            "last_reasons": ["no_tick_feature_health"],
        }

    symbols = _safe_dict(raw.get("symbols"))
    if symbol and symbols:
        payload = _safe_dict(symbols.get(str(symbol).strip().upper()))
        if not payload:
            return {
                "status": "missing",
                "blocking": True,
                "symbol": symbol,
                "last_reasons": ["no_symbol_tick_feature_health"],
            }
    else:
        payload = raw

    status = str(payload.get("status") or raw.get("status") or "unknown").lower()
    blocking = bool(payload.get("blocking", raw.get("blocking", False))) or _blocking_status(status)
    result = dict(payload)
    result["status"] = status
    result["source_status"] = str(payload.get("status") or raw.get("status") or "unknown")
    result["blocking"] = blocking
    if symbol:
        result["symbol"] = str(symbol)
    if "last_reasons" not in result:
        result["last_reasons"] = list(raw.get("last_reasons") or [])
    return result


def build_execution_tick_feature_health(
    health_store: Any,
    feature_bus: Any,
    symbol: str | None,
) -> dict[str, Any]:
    if health_store is None or not symbol:
        return normalize_tick_feature_health_snapshot({}, symbol=symbol)
    bus_stats = {}
    if feature_bus is not None and callable(getattr(feature_bus, "stats", None)):
        try:
            bus_stats = feature_bus.stats()
        except Exception:
            bus_stats = {}
    health_payload = getattr(health_store, "health_payload", None)
    health_for = getattr(health_store, "health_for", None)
    if not callable(health_payload) and not callable(health_for):
        return normalize_tick_feature_health_snapshot({}, symbol=symbol)
    try:
        if callable(health_payload):
            payload = health_payload(symbol, bus_stats=bus_stats)
        else:
            health = health_for(symbol, bus_stats=bus_stats)
            to_dict = getattr(health, "to_dict", None)
            if callable(to_dict):
                payload = to_dict()
            elif isinstance(health, Mapping):
                payload = dict(health)
            else:
                payload = dict(getattr(health, "__dict__", {}) or {})
    except Exception as exc:
        return {
            "status": "critical",
            "blocking": True,
            "symbol": symbol,
            "last_reasons": ["probe_failed"],
            "error": str(exc),
        }
    return normalize_tick_feature_health_snapshot(payload, symbol=symbol)
