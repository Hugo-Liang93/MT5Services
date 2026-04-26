from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from src.monitoring.pipeline.event_bus import PipelineEvent, PipelineEventBus
from src.trading.execution.intrabar_health import build_execution_intrabar_health
from src.trading.execution.reasons import REASON_QUOTE_STALE


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _safe_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    return []


def _build_reason(
    *,
    code: str,
    stage: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": str(code or "unknown"),
        "stage": str(stage or "account_risk"),
        "message": str(message or "unknown"),
        "details": dict(details or {}),
    }


def _stage_from_check(
    check_name: str,
    *,
    event_blocked: bool,
    calendar_health_degraded: bool,
) -> str:
    normalized = str(check_name or "").strip().lower()
    if event_blocked or calendar_health_degraded:
        return "market_tradability"
    if normalized.startswith("deployment_") or normalized.startswith("strategy_"):
        return "execution_gate"
    if "session" in normalized or "timeframe" in normalized:
        return "execution_gate"
    if "calendar" in normalized or "event" in normalized:
        return "market_tradability"
    return "account_risk"


def append_admission_report_event(
    *,
    pipeline_event_bus: PipelineEventBus | None,
    trace_id: str | None,
    symbol: str,
    timeframe: str,
    scope: str,
    report: Mapping[str, Any],
) -> None:
    normalized_trace_id = str(trace_id or "").strip()
    if pipeline_event_bus is None or not normalized_trace_id:
        return
    payload = dict(report)
    payload.setdefault("trace_id", normalized_trace_id)
    pipeline_event_bus.emit(
        PipelineEvent(
            type="admission_report_appended",
            trace_id=normalized_trace_id,
            symbol=str(symbol or ""),
            timeframe=str(timeframe or ""),
            scope=str(scope or "confirmed"),
            ts=_utc_now_iso(),
            payload=payload,
        )
    )


class TradeAdmissionService:
    def __init__(
        self,
        *,
        command_service: Any,
        runtime_views: Any,
        pipeline_event_bus: PipelineEventBus | None = None,
    ) -> None:
        self._command_service = command_service
        self._runtime_views = runtime_views
        self._pipeline_event_bus = pipeline_event_bus

    def evaluate_trade_payload(
        self,
        payload: Mapping[str, Any],
        *,
        requested_operation: str,
        trace_id: str | None = None,
        signal_id: str | None = None,
        intent_id: str | None = None,
        action_id: str | None = None,
        scope: str = "confirmed",
    ) -> dict[str, Any]:
        normalized_payload = dict(payload or {})
        assessment = self._command_service.precheck_trade(**normalized_payload)
        report = self._build_report(
            payload=normalized_payload,
            assessment=assessment,
            requested_operation=requested_operation,
            trace_id=trace_id,
            signal_id=signal_id,
            intent_id=intent_id,
            action_id=action_id,
            scope=scope,
        )
        append_admission_report_event(
            pipeline_event_bus=self._pipeline_event_bus,
            trace_id=str(report.get("trace_id") or ""),
            symbol=str(normalized_payload.get("symbol") or ""),
            timeframe=str(
                normalized_payload.get("timeframe")
                or normalized_payload.get("metadata", {}).get("timeframe")
                or ""
            ),
            scope=scope,
            report=report,
        )
        return {
            "report": report,
            "assessment": assessment,
        }

    def _build_report(
        self,
        *,
        payload: dict[str, Any],
        assessment: Mapping[str, Any],
        requested_operation: str,
        trace_id: str | None,
        signal_id: str | None,
        intent_id: str | None,
        action_id: str | None,
        scope: str,
    ) -> dict[str, Any]:
        assessment_dict = dict(assessment or {})
        tradability = _safe_dict(self._runtime_views.tradability_state_summary())
        account_risk = _safe_dict(self._runtime_views.account_risk_state_summary())
        trade_control = _safe_dict(self._runtime_views.trade_control_summary())
        quote_health = _safe_dict(tradability.get("quote_health"))
        margin_guard = _safe_dict(tradability.get("margin_guard"))
        last_risk_block = str(account_risk.get("last_risk_block") or "").strip()
        payload_scope = (
            str(
                payload.get("scope")
                or _safe_dict(payload.get("metadata")).get("scope")
                or "confirmed"
            )
            .strip()
            .lower()
        )
        intrabar_health = build_execution_intrabar_health(
            _safe_dict(payload.get("metadata")),
            scope=payload_scope,
        )
        economic_guard = {
            "event_blocked": bool(assessment_dict.get("event_blocked", False)),
            "calendar_health_degraded": bool(
                assessment_dict.get("calendar_health_degraded", False)
            ),
            "calendar_health_mode": str(
                assessment_dict.get("calendar_health_mode") or "warn_only"
            ),
            "calendar_health": _safe_dict(assessment_dict.get("calendar_health")),
            "active_windows": _safe_list(assessment_dict.get("active_windows")),
            "upcoming_windows": _safe_list(assessment_dict.get("upcoming_windows")),
            "warnings": _safe_list(assessment_dict.get("warnings")),
        }
        reasons: list[dict[str, Any]] = []
        checks = _safe_list(assessment_dict.get("checks"))
        failed_checks = [
            item
            for item in checks
            if isinstance(item, dict) and item.get("passed") is False
        ]
        for check in failed_checks:
            stage = _stage_from_check(
                str(check.get("name") or ""),
                event_blocked=economic_guard["event_blocked"],
                calendar_health_degraded=economic_guard["calendar_health_degraded"],
            )
            reasons.append(
                _build_reason(
                    code=str(check.get("name") or "check_failed"),
                    stage=stage,
                    message=str(
                        check.get("message")
                        or assessment_dict.get("reason")
                        or "precheck failed"
                    ),
                    details=check,
                )
            )

        if not reasons and assessment_dict.get("reason"):
            reasons.append(
                _build_reason(
                    code="precheck_reason",
                    stage=_stage_from_check(
                        "precheck_reason",
                        event_blocked=economic_guard["event_blocked"],
                        calendar_health_degraded=economic_guard[
                            "calendar_health_degraded"
                        ],
                    ),
                    message=str(assessment_dict.get("reason")),
                    details={"verdict": assessment_dict.get("verdict")},
                )
            )

        if economic_guard["calendar_health_degraded"]:
            reasons.append(
                _build_reason(
                    code="calendar_health_degraded",
                    stage="market_tradability",
                    message="economic calendar health degraded",
                    details=economic_guard["calendar_health"],
                )
            )

        # P1 回归：multi_account main role 是受支持的 delegated 拓扑，
        # readmodel 已明示 verdict='not_applicable' / reason_code='not_executor_role'。
        # 此时本实例不直接执行交易，admission 不应把 runtime_present=False /
        # admission_enabled=False 视作运行时故障并 block。
        is_delegated_role = (
            str(tradability.get("verdict") or "").lower() == "not_applicable"
            and str(tradability.get("reason_code") or "").lower() == "not_executor_role"
        )
        if not is_delegated_role and not bool(
            tradability.get("runtime_present", False)
        ):
            reasons.append(
                _build_reason(
                    code="runtime_absent",
                    stage="market_tradability",
                    message="trade runtime not present",
                    details={"tradability": tradability},
                )
            )
        if not is_delegated_role and not bool(
            tradability.get("admission_enabled", True)
        ):
            reasons.append(
                _build_reason(
                    code="admission_disabled",
                    stage="market_tradability",
                    message="admission gate disabled",
                    details={"tradability": tradability},
                )
            )
        if bool(tradability.get("circuit_open", False)):
            reasons.append(
                _build_reason(
                    code="circuit_open",
                    stage="account_risk",
                    message="executor circuit breaker is open",
                    details={"tradability": tradability},
                )
            )
        if bool(quote_health.get("stale", False)):
            reasons.append(
                _build_reason(
                    code="quote_stale",
                    stage="market_tradability",
                    message="quote health is stale",
                    details=quote_health,
                )
            )
        if bool(intrabar_health.get("stale", False)):
            reasons.append(
                _build_reason(
                    code=(
                        "intrabar_synthesis_stale"
                        if bool(intrabar_health.get("configured", False))
                        else "intrabar_synthesis_unavailable"
                    ),
                    stage="market_tradability",
                    message=(
                        "intrabar synthesis is stale"
                        if bool(intrabar_health.get("configured", False))
                        else "intrabar synthesis metadata is unavailable"
                    ),
                    details=intrabar_health,
                )
            )
        if (
            bool(account_risk.get("should_block_new_trades", False))
            and last_risk_block != REASON_QUOTE_STALE
        ):
            reasons.append(
                _build_reason(
                    code="risk_block_new_trades",
                    stage="account_risk",
                    message=str(last_risk_block or "account risk blocked new trades"),
                    details={
                        "margin_guard": margin_guard,
                        "account_risk": account_risk,
                    },
                )
            )

        decision = str(assessment_dict.get("verdict") or "allow").lower()
        if any(reason["stage"] == "market_tradability" for reason in reasons) and (
            not bool(tradability.get("tradable", True))
            or economic_guard["event_blocked"]
        ):
            decision = "block"
        elif any(reason["stage"] == "account_risk" for reason in reasons) and (
            not bool(tradability.get("tradable", True))
            or str(assessment_dict.get("verdict") or "").lower() == "block"
        ):
            decision = "block"
        elif reasons or economic_guard["warnings"]:
            decision = "warn"

        stage = "account_risk"
        for candidate in ("market_tradability", "execution_gate", "account_risk"):
            if any(reason["stage"] == candidate for reason in reasons):
                stage = candidate
                break

        deployment_contract = {
            "strategy": payload.get("strategy"),
            "timeframe": payload.get("timeframe"),
            "locked_sessions": _safe_list(payload.get("locked_sessions")),
            "locked_timeframes": _safe_list(payload.get("locked_timeframes")),
            "require_pending_entry": payload.get("require_pending_entry"),
            "max_live_positions": payload.get("max_live_positions"),
            "applicable": bool(payload.get("strategy")),
        }
        position_limit_checks = [
            dict(item)
            for item in failed_checks
            if "position" in str(item.get("name") or "").lower()
            or "limit" in str(item.get("name") or "").lower()
        ]
        resolved_trace_id = (
            str(trace_id or "").strip()
            or str(signal_id or "").strip()
            or str(intent_id or "").strip()
            or str(action_id or "").strip()
            or str(assessment_dict.get("request_id") or "").strip()
            or f"admission_{requested_operation}_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        )
        runtime_identity = getattr(self._runtime_views, "runtime_identity", None)
        account_alias = (
            str(
                payload.get("account_alias")
                or assessment_dict.get("account_alias")
                or getattr(runtime_identity, "account_alias", "")
                or getattr(self._command_service, "active_account_alias", "")
                or ""
            ).strip()
            or None
        )
        account_key = (
            str(getattr(runtime_identity, "account_key", "") or "").strip() or None
        )
        return {
            "decision": decision,
            "stage": stage,
            "reasons": reasons,
            "deployment_contract": deployment_contract,
            "economic_guard": economic_guard,
            "quote_health": quote_health,
            "trade_control": trade_control,
            "margin_guard": margin_guard,
            "position_limits": {
                "checks": position_limit_checks,
                "managed_positions": int(
                    _safe_dict(self._runtime_views.trading_state_summary())
                    .get("managed_positions", {})
                    .get("count")
                    or 0
                ),
            },
            "generated_at": _utc_now_iso(),
            "requested_operation": requested_operation,
            "account_alias": account_alias,
            "account_key": account_key,
            "trace_id": resolved_trace_id,
            "signal_id": str(signal_id or payload.get("signal_id") or "").strip()
            or None,
            "intent_id": str(intent_id or payload.get("intent_id") or "").strip()
            or None,
            "action_id": str(action_id or payload.get("action_id") or "").strip()
            or None,
            "scope": scope,
            "source_assessment": assessment_dict,
        }
