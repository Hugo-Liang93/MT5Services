from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.monitoring.pipeline import PipelineEvent
from src.monitoring.pipeline.events import (
    PIPELINE_EXECUTION_FAILED,
    PIPELINE_EXECUTION_SKIPPED,
    PIPELINE_EXECUTION_SUCCEEDED,
)
from src.risk.service import PreTradeRiskBlockedError
from src.signals.metadata_keys import MetadataKey as MK
from src.trading.broker.comment_codec import build_trade_comment
from src.trading.admission.service import append_admission_report_event

from .reasons import SKIP_CATEGORY_DISPATCH, SKIP_CATEGORY_RISK_SERVICE
from .reasons import reason_category

if TYPE_CHECKING:
    from src.signals.models import SignalEvent

    from .executor import TradeExecutor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TerminalExecutionOutcome:
    status: str
    reason: str | None = None
    category: str | None = None
    error_code: str | None = None
    details: dict[str, Any] | None = None


def _build_terminal_result(
    *,
    status: str,
    reason: str,
    category: str,
    details: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    normalized_status = str(status or "").strip().lower() or "failed"
    normalized_reason = str(reason or normalized_status).strip() or normalized_status
    normalized_category = str(category or reason_category(normalized_reason)).strip() or reason_category(normalized_reason)
    result: dict[str, Any] = {
        "status": normalized_status,
        "reason": normalized_reason,
        "category": normalized_category,
    }
    if normalized_status == "skipped":
        result["skip_reason"] = normalized_reason
        result["skip_category"] = normalized_category
    if error_code:
        result["error_code"] = str(error_code)
    if details:
        result["details"] = dict(details)
    return result


def interpret_terminal_result(result: Any) -> TerminalExecutionOutcome:
    if result is None:
        return TerminalExecutionOutcome(
            status="skipped",
            reason="execution_result_unavailable",
            category=SKIP_CATEGORY_DISPATCH,
        )

    if not isinstance(result, dict):
        return TerminalExecutionOutcome(status="completed")

    normalized_status = str(result.get("status") or "").strip().lower()
    if normalized_status in {"blocked", "skipped"}:
        reason = (
            str(result.get("reason") or result.get("skip_reason") or "skipped").strip()
            or "skipped"
        )
        category = (
            str(
                result.get("category")
                or result.get("skip_category")
                or reason_category(reason)
            ).strip()
            or reason_category(reason)
        )
        details = result.get("details")
        return TerminalExecutionOutcome(
            status="skipped",
            reason=reason,
            category=category,
            error_code=str(result.get("error_code") or "").strip() or None,
            details=dict(details) if isinstance(details, dict) and details else None,
        )

    if normalized_status == "failed":
        reason = str(result.get("reason") or "failed").strip() or "failed"
        category = (
            str(result.get("category") or reason_category(reason)).strip()
            or reason_category(reason)
        )
        details = result.get("details")
        return TerminalExecutionOutcome(
            status="failed",
            reason=reason,
            category=category,
            error_code=str(result.get("error_code") or "").strip() or None,
            details=dict(details) if isinstance(details, dict) and details else None,
        )

    return TerminalExecutionOutcome(status="completed")


def emit_terminal_execution_event(
    *,
    pipeline_event_bus,
    event: "SignalEvent",
    result: Any,
    extra_payload: dict[str, Any] | None = None,
) -> TerminalExecutionOutcome:
    outcome = interpret_terminal_result(result)
    trace_id = trace_id_for_event(event)
    if pipeline_event_bus is None or not trace_id:
        return outcome

    payload = dict(extra_payload or {})
    payload.setdefault("status", outcome.status)
    payload.setdefault("signal_id", event.signal_id)
    payload.setdefault("signal_scope", event.scope)
    payload.setdefault("strategy", event.strategy)
    payload.setdefault("direction", event.direction)
    payload.setdefault("confidence", event.confidence)
    if outcome.reason:
        payload.setdefault("reason", outcome.reason)
    if outcome.category:
        payload.setdefault("category", outcome.category)
    if outcome.status == "skipped":
        if outcome.reason:
            payload.setdefault("skip_reason", outcome.reason)
        if outcome.category:
            payload.setdefault("skip_category", outcome.category)
    if outcome.error_code:
        payload.setdefault("error_code", outcome.error_code)
    if outcome.details:
        payload.setdefault("details", dict(outcome.details))
    if result is not None:
        payload.setdefault(
            "result", dict(result) if isinstance(result, dict) else result
        )

    event_type = {
        "completed": PIPELINE_EXECUTION_SUCCEEDED,
        "skipped": PIPELINE_EXECUTION_SKIPPED,
        "failed": PIPELINE_EXECUTION_FAILED,
    }[outcome.status]
    pipeline_event_bus.emit(
        PipelineEvent(
            type=event_type,
            trace_id=trace_id,
            symbol=event.symbol,
            timeframe=event.timeframe or "",
            scope=event.scope,
            ts=datetime.now(timezone.utc).isoformat(),
            payload=payload,
        )
    )
    return outcome


def notify_skip(
    executor: "TradeExecutor",
    signal_id: str,
    reason: str,
    timeframe: str = "",
) -> None:
    with executor.skip_lock:
        executor.skip_reasons[reason] = executor.skip_reasons.get(reason, 0) + 1
        if timeframe:
            tf_entry = executor.tf_stats.setdefault(
                timeframe, {"received": 0, "passed": 0, "skip_reasons": {}}
            )
            tf_entry["skip_reasons"][reason] = (
                tf_entry["skip_reasons"].get(reason, 0) + 1
            )
    if executor.on_execution_skip is not None and signal_id:
        try:
            executor.on_execution_skip(signal_id, reason)
        except Exception:
            logger.debug("on_execution_skip callback failed", exc_info=True)

def trace_id_for_event(event: "SignalEvent") -> str:
    return str(event.metadata.get(MK.SIGNAL_TRACE_ID) or "").strip()


def _account_context(executor: "TradeExecutor", event: "SignalEvent") -> dict[str, Any]:
    runtime_identity = getattr(executor, "runtime_identity", None)
    metadata = dict(event.metadata or {})
    return {
        "account_key": metadata.get("target_account_key")
        or getattr(runtime_identity, "account_key", None),
        "account_alias": metadata.get("target_account_alias")
        or getattr(runtime_identity, "account_alias", None),
        "intent_id": metadata.get("intent_id"),
    }


def _admission_stage_from_category(category: str) -> str:
    normalized = str(category or "").strip().lower()
    if normalized in {"risk_guard", "risk_service"}:
        return "account_risk"
    if normalized in {"cost_guard", "trade_params", "market_data"}:
        return "market_tradability"
    if normalized in {"execution_gate", "governance", "position", "duplicate_guard", "confidence", "cooldown"}:
        return "execution_gate"
    if normalized in {"eod_guard", "performance", "equity_filter"}:
        return "market_tradability"
    return "account_risk"


def emit_admission_report(
    executor: "TradeExecutor",
    event: "SignalEvent",
    *,
    decision: str,
    stage: str,
    reasons: list[dict[str, Any]] | None = None,
    requested_operation: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    trace_id = trace_id_for_event(event)
    if not trace_id:
        return
    context = _account_context(executor, event)
    payload = {
        "decision": str(decision or "allow"),
        "stage": str(stage or "account_risk"),
        "reasons": list(reasons or []),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested_operation": requested_operation
        or ("intrabar_execution" if event.scope == "intrabar" else "signal_execution"),
        "scope": event.scope,
        "trace_id": trace_id,
        "signal_id": event.signal_id,
        "intent_id": context.get("intent_id"),
        "account_key": context.get("account_key"),
        "account_alias": context.get("account_alias"),
        "deployment_contract": {
            "strategy": event.strategy,
            "timeframe": event.timeframe,
        },
    }
    if extra:
        payload.update(extra)
    append_admission_report_event(
        pipeline_event_bus=executor.get_pipeline_event_bus(),
        trace_id=trace_id,
        symbol=event.symbol,
        timeframe=event.timeframe or "",
        scope=event.scope,
        report=payload,
    )


def emit_blocked_admission_report(
    executor: "TradeExecutor",
    event: "SignalEvent",
    *,
    code: str,
    category: str,
    message: str,
    details: dict[str, Any] | None = None,
    requested_operation: str | None = None,
) -> None:
    emit_admission_report(
        executor,
        event,
        decision="block",
        stage=_admission_stage_from_category(category),
        requested_operation=requested_operation,
        reasons=[
            {
                "code": str(code or "blocked"),
                "stage": _admission_stage_from_category(category),
                "message": str(message or code or "blocked"),
                "details": dict(details or {}),
            }
        ],
    )


def emit_execution_decided(
    executor: "TradeExecutor", event: "SignalEvent", *, order_kind: str
) -> None:
    pipeline_bus = executor.get_pipeline_event_bus()
    trace_id = trace_id_for_event(event)
    if pipeline_bus is None or not trace_id:
        return
    pipeline_bus.emit_execution_decided(
        trace_id=trace_id,
        symbol=event.symbol,
        timeframe=event.timeframe or "",
        scope=event.scope,
        strategy=event.strategy,
        direction=event.direction,
        order_kind=order_kind,
    )


def emit_execution_blocked(
    executor: "TradeExecutor",
    event: "SignalEvent",
    *,
    reason: str,
    category: str,
) -> None:
    pipeline_bus = executor.get_pipeline_event_bus()
    trace_id = trace_id_for_event(event)
    if pipeline_bus is None or not trace_id:
        return
    pipeline_bus.emit_execution_blocked(
        trace_id=trace_id,
        symbol=event.symbol,
        timeframe=event.timeframe or "",
        scope=event.scope,
        strategy=event.strategy,
        direction=event.direction,
        reason=reason,
        category=category,
    )


def emit_execution_submitted(
    executor: "TradeExecutor",
    event: "SignalEvent",
    *,
    order_kind: str,
    ticket: Any = None,
) -> None:
    pipeline_bus = executor.get_pipeline_event_bus()
    trace_id = trace_id_for_event(event)
    if pipeline_bus is None or not trace_id:
        return
    normalized_ticket: int | None = None
    try:
        normalized_ticket = int(ticket) if ticket is not None else None
    except (TypeError, ValueError):
        normalized_ticket = None
    pipeline_bus.emit_execution_submitted(
        trace_id=trace_id,
        symbol=event.symbol,
        timeframe=event.timeframe or "",
        scope=event.scope,
        strategy=event.strategy,
        direction=event.direction,
        order_kind=order_kind,
        request_id=event.signal_id,
        ticket=normalized_ticket,
    )


def emit_pending_order_submitted(
    executor: "TradeExecutor",
    event: "SignalEvent",
    *,
    order_kind: str,
    ticket: Any = None,
) -> None:
    pipeline_bus = executor.get_pipeline_event_bus()
    trace_id = trace_id_for_event(event)
    if pipeline_bus is None or not trace_id:
        return
    normalized_ticket: int | None = None
    try:
        normalized_ticket = int(ticket) if ticket is not None else None
    except (TypeError, ValueError):
        normalized_ticket = None
    pipeline_bus.emit_pending_order_submitted(
        trace_id=trace_id,
        symbol=event.symbol,
        timeframe=event.timeframe or "",
        scope=event.scope,
        strategy=event.strategy,
        direction=event.direction,
        order_kind=order_kind,
        request_id=event.signal_id,
        ticket=normalized_ticket,
    )


def emit_execution_failed(
    executor: "TradeExecutor",
    event: "SignalEvent",
    *,
    order_kind: str,
    reason: str,
    category: str,
) -> None:
    pipeline_bus = executor.get_pipeline_event_bus()
    trace_id = trace_id_for_event(event)
    if pipeline_bus is None or not trace_id:
        return
    pipeline_bus.emit_execution_failed(
        trace_id=trace_id,
        symbol=event.symbol,
        timeframe=event.timeframe or "",
        scope=event.scope,
        strategy=event.strategy,
        direction=event.direction,
        order_kind=order_kind,
        reason=reason,
        category=category,
    )


def build_trade_metadata(event: "SignalEvent") -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "entry_origin": "auto",
        "signal": {
            "signal_id": event.signal_id,
            "strategy": event.strategy,
            "timeframe": event.timeframe,
            "signal_state": event.signal_state,
            "confidence": round(float(event.confidence), 4),
        },
    }
    regime = event.metadata.get(MK.REGIME)
    if regime is not None:
        metadata[MK.REGIME] = regime
    raw_structure = event.metadata.get(MK.MARKET_STRUCTURE)
    if isinstance(raw_structure, dict):
        metadata[MK.MARKET_STRUCTURE] = dict(raw_structure)
    else:
        try:
            metadata[MK.MARKET_STRUCTURE] = raw_structure.to_dict()  # type: ignore[attr-defined]
        except Exception:
            pass
    return metadata


def record_slippage(
    executor: "TradeExecutor",
    *,
    requested_price: float | None,
    fill_price: float | None,
    symbol_point: float | None,
) -> dict[str, float | None]:
    try:
        requested = float(requested_price) if requested_price is not None else None
    except (TypeError, ValueError):
        requested = None
    try:
        filled = float(fill_price) if fill_price is not None else None
    except (TypeError, ValueError):
        filled = None
    if requested is None or filled is None:
        return {
            "requested_price": requested,
            "fill_price": filled,
            "slippage_price": None,
            "slippage_points": None,
        }

    slippage_price = round(filled - requested, 6)
    slippage_points = None
    if symbol_point is not None and symbol_point > 0:
        slippage_points = round(slippage_price / symbol_point, 2)
    executor.execution_quality["slippage_samples"] += 1
    executor.execution_quality["slippage_total_price"] += slippage_price
    if slippage_points is not None:
        executor.execution_quality["slippage_total_points"] += slippage_points
    return {
        "requested_price": requested,
        "fill_price": filled,
        "slippage_price": slippage_price,
        "slippage_points": slippage_points,
    }


def adjust_params_for_fill(
    params: Any,
    requested_price: float | None,
    fill_price: float | None,
    *,
    slippage_warn_ratio: float = 0.3,
    symbol: str = "",
    direction: str = "",
) -> Any:
    """根据实际成交价调整 TradeParameters，维持原始 SL/TP 距离。

    当 fill_price 与 requested_price 存在偏差时，按偏移量平移 SL/TP，
    确保 PositionManager 跟踪的 risk/reward 与策略设计一致。
    """
    if requested_price is None or fill_price is None:
        return params
    price_shift = fill_price - requested_price
    if abs(price_shift) < 1e-10:
        return params
    # 滑点显著时发出警告
    if (
        params.sl_distance > 0
        and abs(price_shift) > params.sl_distance * slippage_warn_ratio
    ):
        logger.warning(
            "Significant slippage for %s %s: requested=%.5f fill=%.5f "
            "shift=%.5f (%.0f%% of SL distance %.5f)",
            direction,
            symbol,
            requested_price,
            fill_price,
            price_shift,
            abs(price_shift) / params.sl_distance * 100,
            params.sl_distance,
        )
    adjusted_sl = params.stop_loss + price_shift
    adjusted_tp = params.take_profit + price_shift
    return _dc_replace(
        params,
        entry_price=fill_price,
        stop_loss=adjusted_sl,
        take_profit=adjusted_tp,
        sl_distance=abs(fill_price - adjusted_sl),
        tp_distance=abs(adjusted_tp - fill_price),
    )


def execute_market_order(
    executor: "TradeExecutor",
    event: "SignalEvent",
    params: Any,
    *,
    cost_metrics: dict[str, float | None] | None = None,
) -> dict[str, Any | None]:
    payload = {
        "symbol": event.symbol,
        "volume": params.position_size,
        "side": event.direction,
        "order_kind": "market",
        "sl": params.stop_loss,
        "tp": params.take_profit,
        "comment": build_trade_comment(
            request_id=event.signal_id,
            timeframe=event.timeframe,
            strategy=event.strategy,
            side=event.direction,
            order_kind="market",
        ),
        "request_id": event.signal_id,
        "metadata": build_trade_metadata(event),
    }

    try:
        result = executor.trading.dispatch_operation("trade", payload)
        executor.execution_count += 1
        executor.last_execution_at = datetime.now(timezone.utc)
        executor.last_error = None
        executor.last_risk_block = None
        executor.consecutive_failures = 0
        requested_price = None
        fill_price = None
        symbol_point = None
        if isinstance(result, dict):
            requested_price = result.get("requested_price") or result.get("price")
            fill_price = result.get("fill_price") or result.get("price")
            try:
                symbol_point = (
                    float(cost_metrics.get("symbol_point")) if cost_metrics else None
                )
            except (TypeError, ValueError):
                symbol_point = None
            if result.get("recovered_from_state"):
                executor.execution_quality["recovered_from_state"] += 1
            emit_execution_submitted(
                executor,
                event,
                order_kind="market",
                ticket=result.get("ticket") or result.get("order"),
            )
        execution_quality = record_slippage(
            executor,
            requested_price=requested_price,
            fill_price=fill_price,
            symbol_point=symbol_point,
        )
        tracking_params = adjust_params_for_fill(
            params,
            requested_price,
            fill_price,
            symbol=event.symbol,
            direction=event.direction,
        )
        account_context = _account_context(executor, event)
        log_entry = {
            "at": executor.last_execution_at.isoformat(),
            "signal_id": event.signal_id,
            "account_key": account_context["account_key"],
            "account_alias": account_context["account_alias"],
            "intent_id": account_context["intent_id"],
            "symbol": event.symbol,
            "direction": event.direction,
            "strategy": event.strategy,
            "confidence": event.confidence,
            "params": {
                "volume": tracking_params.position_size,
                "entry_price": (
                    fill_price
                    if fill_price is not None
                    else tracking_params.entry_price
                ),
                "sl": tracking_params.stop_loss,
                "tp": tracking_params.take_profit,
                "rr": tracking_params.risk_reward_ratio,
            },
            "cost": dict(cost_metrics or {}),
            "execution_quality": execution_quality,
            "success": True,
        }
        executor.execution_log.append(log_entry)
        bar_time = event.metadata.get(MK.BAR_TIME)
        if bar_time is not None:
            executor.last_entry_bar_time[
                (event.symbol, event.strategy, event.direction)
            ] = bar_time
        for fn in executor.on_trade_executed:
            try:
                fn(log_entry)
            except Exception:
                logger.debug("on_trade_executed callback failed", exc_info=True)
        logger.info(
            "TradeExecutor: executed %s %s vol=%.2f sl=%.2f tp=%.2f rr=%.2f (signal=%s)",
            event.direction,
            event.symbol,
            tracking_params.position_size,
            tracking_params.stop_loss,
            tracking_params.take_profit,
            tracking_params.risk_reward_ratio,
            event.signal_id,
        )
        if executor.persist_execution_fn is not None:
            try:
                executor.persist_execution_fn([log_entry])
            except Exception as persist_exc:
                logger.warning(
                    "TradeExecutor: persist execution failed: %s", persist_exc
                )
        if executor.position_manager is not None and isinstance(result, dict):
            ticket = result.get("ticket") or result.get("order")
            if ticket:
                try:
                    executor.position_manager.track_position(
                        ticket=int(ticket),
                        signal_id=event.signal_id,
                        symbol=event.symbol,
                        action=event.direction,
                        params=tracking_params,
                        timeframe=event.timeframe,
                        strategy=event.strategy,
                        confidence=event.confidence,
                        regime=event.metadata.get(MK.REGIME),
                        comment=str(result.get("comment") or payload["comment"]),
                        fill_price=(
                            float(result.get("fill_price"))
                            if result.get("fill_price") is not None
                            else None
                        ),
                        exit_spec=event.metadata.get(MK.EXIT_SPEC),
                        strategy_category=event.metadata.get(MK.STRATEGY_CATEGORY, ""),
                    )
                except Exception as pm_exc:
                    logger.warning(
                        "TradeExecutor: failed to register position ticket=%s: %s",
                        ticket,
                        pm_exc,
                    )
        if executor.trade_outcome_tracker is not None:
            try:
                executor.trade_outcome_tracker.on_trade_opened(
                    signal_id=event.signal_id,
                    symbol=event.symbol,
                    timeframe=event.timeframe,
                    strategy=event.strategy,
                    direction=event.direction,
                    fill_price=(
                        float(result.get("fill_price"))
                        if isinstance(result, dict)
                        and result.get("fill_price") is not None
                        else params.entry_price
                    ),
                    confidence=event.confidence,
                    regime=event.metadata.get(MK.REGIME),
                    account_key=account_context["account_key"],
                    account_alias=account_context["account_alias"],
                    intent_id=account_context["intent_id"],
                )
            except Exception as outcome_exc:
                logger.warning(
                    "TradeExecutor: failed to notify trade_outcome_tracker: %s",
                    outcome_exc,
                )
        if isinstance(result, dict):
            result.setdefault("execution_quality", execution_quality)
        return result
    except PreTradeRiskBlockedError as exc:
        executor.last_risk_block = str(exc)
        executor.execution_quality["risk_blocks"] += 1
        assessment = dict(exc.assessment or {})
        reason = str(assessment.get("reason") or exc)
        emit_execution_blocked(
            executor,
            event,
            reason=reason,
            category=SKIP_CATEGORY_RISK_SERVICE,
        )
        executor.execution_log.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
                "account_key": _account_context(executor, event)["account_key"],
                "account_alias": _account_context(executor, event)["account_alias"],
                "intent_id": _account_context(executor, event)["intent_id"],
                "symbol": event.symbol,
                "direction": event.direction,
                "strategy": event.strategy,
                "success": False,
                "skipped": True,
                "reason": reason,
                "assessment": assessment,
            }
        )
        notify_skip(executor, event.signal_id, reason, event.timeframe or "")
        if executor.persist_execution_fn is not None:
            try:
                executor.persist_execution_fn(
                    [
                        {
                            "at": datetime.now(timezone.utc).isoformat(),
                            "signal_id": event.signal_id,
                            "account_key": _account_context(executor, event)["account_key"],
                            "account_alias": _account_context(executor, event)["account_alias"],
                            "intent_id": _account_context(executor, event)["intent_id"],
                            "symbol": event.symbol,
                            "direction": event.direction,
                            "strategy": event.strategy,
                            "success": False,
                            "error": reason,
                            "metadata": {"blocked_by_risk": assessment},
                        }
                    ]
                )
            except Exception as persist_exc:
                logger.warning(
                    "TradeExecutor: persist blocked-entry failed: %s", persist_exc
                )
        return _build_terminal_result(
            status="skipped",
            reason=reason,
            category=SKIP_CATEGORY_RISK_SERVICE,
            details={"assessment": assessment},
        )
    except Exception as exc:
        executor.last_error = str(exc)
        executor.consecutive_failures += 1
        executor.execution_log.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
                "account_key": _account_context(executor, event)["account_key"],
                "account_alias": _account_context(executor, event)["account_alias"],
                "intent_id": _account_context(executor, event)["intent_id"],
                "symbol": event.symbol,
                "direction": event.direction,
                "strategy": event.strategy,
                "success": False,
                "error": str(exc),
            }
        )
        logger.exception(
            "TradeExecutor: failed to execute %s %s: %s",
            event.direction,
            event.symbol,
            exc,
        )
        if (
            not executor.circuit_open
            and executor.consecutive_failures
            >= executor.config.max_consecutive_failures
        ):
            executor.circuit_open = True
            executor.circuit_open_at = datetime.now(timezone.utc)
            account_context = _account_context(executor, event)
            executor.record_circuit_breaker_event(
                event="tripped",
                reason="max_consecutive_failures",
                consecutive_failures=executor.consecutive_failures,
                account_alias=account_context["account_alias"],
                account_key=account_context["account_key"],
                metadata={
                    "signal_id": event.signal_id,
                    "symbol": event.symbol,
                    "strategy": event.strategy,
                },
            )
            logger.error(
                "TradeExecutor: circuit breaker OPENED after %d consecutive failures. "
                "Auto-trading suspended. Will auto-reset in %d minutes or call reset_circuit().",
                executor.consecutive_failures,
                executor.config.circuit_auto_reset_minutes,
            )
        fail_entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "signal_id": event.signal_id,
            "account_key": _account_context(executor, event)["account_key"],
            "account_alias": _account_context(executor, event)["account_alias"],
            "intent_id": _account_context(executor, event)["intent_id"],
            "symbol": event.symbol,
            "direction": event.direction,
            "strategy": event.strategy,
            "success": False,
            "error": str(exc),
        }
        if executor.persist_execution_fn is not None:
            try:
                executor.persist_execution_fn([fail_entry])
            except Exception as persist_exc:
                logger.warning(
                    "TradeExecutor: persist fail-entry failed: %s", persist_exc
                )
        return _build_terminal_result(
            status="failed",
            reason=str(exc),
            category=SKIP_CATEGORY_DISPATCH,
            error_code=type(exc).__name__,
        )
