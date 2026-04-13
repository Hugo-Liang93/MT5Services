from __future__ import annotations

from typing import Any, Mapping

from ..models import TradeExecutionDetails


def _normalize_payload(raw_result: Any) -> dict[str, Any]:
    if isinstance(raw_result, TradeExecutionDetails):
        return raw_result.to_dict()
    if isinstance(raw_result, Mapping):
        return dict(raw_result)
    return {"result": raw_result}


def _normalize_checks(raw_checks: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in list(raw_checks or []):
        if isinstance(item, Mapping):
            normalized.append(dict(item))
    return normalized


def _normalize_warnings(raw_warnings: Any) -> list[str]:
    normalized: list[str] = []
    for item in list(raw_warnings or []):
        warning = str(item or "").strip()
        if warning:
            normalized.append(warning)
    return normalized


def _normalize_mapping(raw_value: Any) -> dict[str, Any]:
    if isinstance(raw_value, Mapping):
        return dict(raw_value)
    return {}


def build_blocked_trade_precheck_result(
    *,
    symbol: str,
    request_id: str,
    reason: str,
    checks: Any,
    warnings: Any = None,
    suggested_adjustment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "enabled": True,
        "mode": "strict",
        "blocked": True,
        "verdict": "block",
        "reason": reason,
        "symbol": symbol,
        "active_windows": [],
        "upcoming_windows": [],
        "warnings": _normalize_warnings(warnings),
        "calendar_health_mode": "warn_only",
        "calendar_health": {},
        "checks": _normalize_checks(checks),
        "estimated_margin": None,
        "margin_error": None,
        "intent": {},
        "request_id": request_id,
        "executable": False,
        "suggested_adjustment": dict(suggested_adjustment) if suggested_adjustment else None,
    }


def build_disabled_trade_precheck_result(
    *,
    symbol: str,
    request_id: str,
) -> dict[str, Any]:
    return {
        "enabled": False,
        "mode": "off",
        "blocked": False,
        "verdict": "allow",
        "reason": None,
        "symbol": symbol,
        "active_windows": [],
        "upcoming_windows": [],
        "warnings": [],
        "calendar_health_mode": "warn_only",
        "calendar_health": {},
        "checks": [],
        "estimated_margin": None,
        "margin_error": None,
        "intent": {},
        "request_id": request_id,
        "executable": True,
        "suggested_adjustment": None,
    }


def build_trade_precheck_result(
    assessment: Mapping[str, Any],
    *,
    request_id: str,
    checks: Any = None,
    warnings: Any = None,
    estimated_margin: float | None = None,
) -> dict[str, Any]:
    merged_checks = _normalize_checks(checks) + _normalize_checks(assessment.get("checks"))
    merged_warnings = _normalize_warnings(warnings) + _normalize_warnings(assessment.get("warnings"))
    if estimated_margin is None:
        merged_warnings = [*merged_warnings, "Margin estimate unavailable"]

    verdict = str(assessment.get("verdict") or "allow").strip().lower() or "allow"
    executable = verdict != "block"
    payload = {
        "enabled": bool(assessment.get("enabled", True)),
        "mode": str(assessment.get("mode") or "strict"),
        "blocked": bool(assessment.get("blocked", verdict == "block")),
        "verdict": verdict,
        "reason": assessment.get("reason"),
        "symbol": str(assessment.get("symbol") or ""),
        "active_windows": list(assessment.get("active_windows") or []),
        "upcoming_windows": list(assessment.get("upcoming_windows") or []),
        "warnings": merged_warnings,
        "calendar_health_mode": str(assessment.get("calendar_health_mode") or "warn_only"),
        "calendar_health": _normalize_mapping(assessment.get("calendar_health")),
        "checks": merged_checks,
        "estimated_margin": estimated_margin,
        "margin_error": assessment.get("margin_error"),
        "intent": _normalize_mapping(assessment.get("intent")),
        "request_id": request_id,
        "executable": executable,
        "suggested_adjustment": (
            None if executable else {"verdict": "review_risk_windows"}
        ),
    }
    for key, value in assessment.items():
        if key in payload:
            continue
        payload[key] = value
    return payload


def build_trade_execution_result(
    raw_result: Any,
    *,
    request_id: str,
    dry_run: bool,
    symbol: str,
    volume: float,
    side: str,
    order_kind: str,
    requested_price: float | None,
    comment: str,
    estimated_margin: float | None,
    pre_trade_risk: Mapping[str, Any] | None,
    precheck: Mapping[str, Any] | None,
    state_consistency: Mapping[str, Any] | None = None,
    execution_attempts: int | None = None,
    execution_state: str | None = None,
) -> dict[str, Any]:
    payload = _normalize_payload(raw_result)
    payload["request_id"] = request_id
    payload["dry_run"] = dry_run
    payload["symbol"] = symbol
    payload["volume"] = volume
    payload["side"] = side
    payload["order_kind"] = order_kind
    payload["requested_price"] = requested_price
    payload["comment"] = comment
    payload["estimated_margin"] = estimated_margin
    payload["pre_trade_risk"] = _normalize_mapping(pre_trade_risk)
    payload["precheck"] = _normalize_mapping(precheck)
    payload["state_consistency"] = _normalize_mapping(state_consistency)
    if execution_attempts is not None:
        payload["execution_attempts"] = int(execution_attempts)
    if execution_state is not None:
        payload["execution_state"] = execution_state
    return payload


def build_trade_operation_result(
    raw_result: Any,
    *,
    trace_id: str,
    account_alias: str,
    operation_id: str,
) -> dict[str, Any]:
    payload = _normalize_payload(raw_result)
    payload["trace_id"] = trace_id
    payload["account_alias"] = account_alias
    payload["operation_id"] = operation_id
    return payload


def attach_dispatch_precheck(
    result: Any,
    *,
    precheck: Mapping[str, Any],
) -> Any:
    if not isinstance(result, Mapping):
        return result
    payload = dict(result)
    payload["dispatch_precheck"] = dict(precheck)
    return payload


def build_idempotent_trade_replay(
    raw_result: Any,
    *,
    source: str,
    operation_id: str | None = None,
) -> dict[str, Any]:
    payload = _normalize_payload(raw_result)
    if operation_id is not None:
        payload["operation_id"] = operation_id
    payload["idempotent_replay"] = True
    payload["idempotent_source"] = source
    return payload
