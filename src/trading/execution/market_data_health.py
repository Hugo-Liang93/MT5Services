"""Execution-facing market data health projection.

The ingestor owns raw market data health facts.  The execution chain consumes this
small projection so new-entry gates do not need to know ingestor internals.
"""

from __future__ import annotations

from typing import Any, Mapping


def _safe_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _lane_matches_symbol(lane: str, symbol: str | None) -> bool:
    if not symbol:
        return True
    parts = str(lane or "").split(":")
    if len(parts) < 2:
        return False
    return parts[1].strip().upper() == str(symbol).strip().upper()


def _lane_blocking(lane: str, lane_payload: Mapping[str, Any] | None) -> bool:
    payload = _safe_dict(lane_payload)
    if not payload:
        return True
    status = str(payload.get("status") or "").strip().lower()
    return bool(
        payload.get("stale", False)
        or status in {"critical", "stale", "warming_up", "unavailable"}
    )


def normalize_market_data_health_snapshot(
    snapshot: Mapping[str, Any],
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Normalize ingestor health into an execution/admission decision contract."""
    raw = _safe_dict(snapshot)
    if not raw:
        return {
            "status": "unavailable",
            "blocking": False,
            "blocked_lanes": [],
            "dependency_contract": {"required_lanes": []},
        }

    dependency_contract = _safe_dict(raw.get("dependency_contract"))
    required_lanes = [
        str(lane)
        for lane in dependency_contract.get("required_lanes", []) or []
        if str(lane or "").strip()
    ]
    lanes = _safe_dict(raw.get("lanes"))
    if not lanes:
        lanes = _safe_dict(_safe_dict(raw.get("freshness")).get("lanes"))
    explicit_blocked_lanes = [
        str(lane)
        for lane in raw.get("blocked_lanes", []) or []
        if str(lane or "").strip()
    ]

    relevant_required = [
        lane for lane in required_lanes if _lane_matches_symbol(lane, symbol)
    ]
    blocked_lanes = {
        lane for lane in relevant_required if _lane_blocking(lane, lanes.get(lane))
    }
    blocked_lanes.update(
        lane for lane in explicit_blocked_lanes if _lane_matches_symbol(lane, symbol)
    )

    mt5 = _safe_dict(raw.get("mt5"))
    symbols = _safe_dict(raw.get("symbols"))
    symbol_health = _safe_dict(symbols.get(str(symbol))) if symbol else {}
    circuit_open = bool(mt5.get("circuit_open", False))
    symbol_backoff = bool(symbol_health.get("backoff_active", False))
    explicit_blocking = bool(raw.get("blocking", False))
    blocking = bool(
        circuit_open
        or symbol_backoff
        or blocked_lanes
        or (explicit_blocking and symbol is None)
        or (
            explicit_blocking
            and symbol is not None
            and (
                not explicit_blocked_lanes
                or any(
                    _lane_matches_symbol(lane, symbol)
                    for lane in explicit_blocked_lanes
                )
            )
        )
    )

    status = "critical" if blocking else str(raw.get("status") or "unknown")
    normalized = dict(raw)
    normalized["status"] = status
    normalized["source_status"] = str(raw.get("status") or "unknown")
    normalized["blocking"] = blocking
    normalized["blocked_lanes"] = sorted(blocked_lanes)
    normalized["dependency_contract"] = {
        **dependency_contract,
        "required_lanes": sorted(required_lanes),
        "required_lane_count": len(required_lanes),
    }
    if symbol:
        normalized["symbol"] = str(symbol)
        normalized["symbol_health"] = symbol_health
    return normalized


def build_execution_market_data_health(
    ingestor: Any,
    symbol: str | None,
) -> dict[str, Any]:
    if ingestor is None:
        return {
            "status": "unavailable",
            "blocking": False,
            "blocked_lanes": [],
            "dependency_contract": {"required_lanes": []},
        }
    health_snapshot = getattr(ingestor, "health_snapshot", None)
    if not callable(health_snapshot):
        return {
            "status": "unavailable",
            "blocking": False,
            "blocked_lanes": [],
            "dependency_contract": {"required_lanes": []},
        }
    try:
        snapshot = health_snapshot()
    except Exception as exc:
        return {
            "status": "critical",
            "source_status": "probe_failed",
            "blocking": True,
            "blocked_lanes": [],
            "dependency_contract": {"required_lanes": []},
            "error": str(exc),
        }
    if not isinstance(snapshot, Mapping):
        return {
            "status": "critical",
            "source_status": "invalid_snapshot",
            "blocking": True,
            "blocked_lanes": [],
            "dependency_contract": {"required_lanes": []},
        }
    return normalize_market_data_health_snapshot(snapshot, symbol=symbol)
