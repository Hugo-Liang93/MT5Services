from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.risk.service import PreTradeRiskBlockedError

if TYPE_CHECKING:
    from src.signals.models import SignalEvent
    from .executor import TradeExecutor

logger = logging.getLogger(__name__)


def notify_skip(
    executor: "TradeExecutor",
    signal_id: str,
    reason: str,
    timeframe: str = "",
    *,
    event: "SignalEvent | None" = None,
) -> None:
    with executor._skip_lock:
        executor._skip_reasons[reason] = executor._skip_reasons.get(reason, 0) + 1
        if timeframe:
            tf_entry = executor._tf_stats.setdefault(
                timeframe, {"received": 0, "passed": 0, "skip_reasons": {}}
            )
            tf_entry["skip_reasons"][reason] = (
                tf_entry["skip_reasons"].get(reason, 0) + 1
            )
    if executor._on_execution_skip is not None and signal_id:
        try:
            executor._on_execution_skip(signal_id, reason)
        except Exception:
            logger.debug("on_execution_skip callback failed", exc_info=True)
    # 发射 EXECUTION_SKIPPED pipeline 事件
    pipeline_bus = getattr(executor, "_pipeline_bus", None)
    if pipeline_bus is not None and hasattr(pipeline_bus, "emit_execution_skipped") and event is not None:
        tid = str(event.metadata.get("signal_trace_id") or "")
        pipeline_bus.emit_execution_skipped(
            trace_id=tid,
            symbol=event.symbol,
            timeframe=timeframe or event.timeframe or "",
            scope=event.metadata.get("scope", "confirmed"),
            strategy=event.strategy,
            direction=event.direction,
            skip_reason=reason,
            skip_category=_skip_reason_category(reason),
            confidence=event.confidence,
        )


def _skip_reason_category(reason: str) -> str:
    """将 skip reason 映射到类别。"""
    categories = {
        "min_confidence": "confidence",
        "position_limit": "position",
        "pnl_circuit_paused": "circuit",
        "margin_guard_block": "risk_guard",
        "htf_conflict_block": "htf_alignment",
        "after_eod_block": "eod_guard",
        "reentry_cooldown": "cooldown",
        "spread_to_stop_ratio_too_high": "cost_guard",
        "trade_params_unavailable": "trade_params",
    }
    for key, cat in categories.items():
        if key in reason:
            return cat
    if "duplicate" in reason or "same_strategy" in reason:
        return "duplicate_guard"
    if "gate" in reason or "voting_group" in reason or "armed" in reason:
        return "execution_gate"
    return "other"


def trace_id_for_event(event: "SignalEvent") -> str:
    return str(event.metadata.get("signal_trace_id") or "").strip()


def emit_execution_decided(
    executor: "TradeExecutor", event: "SignalEvent", *, order_kind: str
) -> None:
    pipeline_bus = executor._pipeline_event_bus
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
    pipeline_bus = executor._pipeline_event_bus
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
    pipeline_bus = executor._pipeline_event_bus
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
    pipeline_bus = executor._pipeline_event_bus
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
    pipeline_bus = executor._pipeline_event_bus
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
    regime = event.metadata.get("regime")
    if regime is not None:
        metadata["regime"] = regime
    raw_structure = event.metadata.get("market_structure")
    if isinstance(raw_structure, dict):
        metadata["market_structure"] = dict(raw_structure)
    elif hasattr(raw_structure, "to_dict"):
        try:
            metadata["market_structure"] = raw_structure.to_dict()
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
    executor._execution_quality["slippage_samples"] += 1
    executor._execution_quality["slippage_total_price"] += slippage_price
    if slippage_points is not None:
        executor._execution_quality["slippage_total_points"] += slippage_points
    return {
        "requested_price": requested,
        "fill_price": filled,
        "slippage_price": slippage_price,
        "slippage_points": slippage_points,
    }


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
        "comment": f"{event.timeframe}:{event.strategy}:{event.direction}"[:31],
        "request_id": event.signal_id,
        "metadata": build_trade_metadata(event),
    }

    try:
        result = executor._trading.dispatch_operation("trade", payload)
        executor._execution_count += 1
        executor._last_execution_at = datetime.now(timezone.utc)
        executor._last_error = None
        executor._last_risk_block = None
        executor._consecutive_failures = 0
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
                executor._execution_quality["recovered_from_state"] += 1
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
        log_entry = {
            "at": executor._last_execution_at.isoformat(),
            "signal_id": event.signal_id,
            "symbol": event.symbol,
            "direction": event.direction,
            "strategy": event.strategy,
            "confidence": event.confidence,
            "params": {
                "volume": params.position_size,
                "entry_price": fill_price if fill_price is not None else params.entry_price,
                "sl": params.stop_loss,
                "tp": params.take_profit,
                "rr": params.risk_reward_ratio,
            },
            "cost": dict(cost_metrics or {}),
            "execution_quality": execution_quality,
            "success": True,
        }
        executor._execution_log.append(log_entry)
        bar_time = event.metadata.get("bar_time")
        if bar_time is not None:
            executor._last_entry_bar_time[
                (event.symbol, event.strategy, event.direction)
            ] = bar_time
        for fn in executor._on_trade_executed:
            try:
                fn(log_entry)
            except Exception:
                logger.debug("on_trade_executed callback failed", exc_info=True)
        logger.info(
            "TradeExecutor: executed %s %s vol=%.2f sl=%.2f tp=%.2f rr=%.2f (signal=%s)",
            event.direction,
            event.symbol,
            params.position_size,
            params.stop_loss,
            params.take_profit,
            params.risk_reward_ratio,
            event.signal_id,
        )
        if executor._persist_execution_fn is not None:
            try:
                executor._persist_execution_fn([log_entry])
            except Exception as persist_exc:
                logger.warning(
                    "TradeExecutor: persist execution failed: %s", persist_exc
                )
        if executor._position_manager is not None and isinstance(result, dict):
            ticket = result.get("ticket") or result.get("order")
            if ticket:
                try:
                    executor._position_manager.track_position(
                        ticket=int(ticket),
                        signal_id=event.signal_id,
                        symbol=event.symbol,
                        action=event.direction,
                        params=params,
                        timeframe=event.timeframe,
                        strategy=event.strategy,
                        confidence=event.confidence,
                        regime=event.metadata.get("regime"),
                        comment=str(result.get("comment") or payload["comment"]),
                        fill_price=(
                            float(result.get("fill_price"))
                            if result.get("fill_price") is not None
                            else None
                        ),
                    )
                except Exception as pm_exc:
                    logger.warning(
                        "TradeExecutor: failed to register position ticket=%s: %s",
                        ticket,
                        pm_exc,
                    )
        if executor._trade_outcome_tracker is not None:
            try:
                executor._trade_outcome_tracker.on_trade_opened(
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
                    regime=event.metadata.get("regime"),
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
        executor._last_risk_block = str(exc)
        executor._execution_quality["risk_blocks"] += 1
        assessment = dict(exc.assessment or {})
        reason = str(assessment.get("reason") or exc)
        emit_execution_blocked(
            executor,
            event,
            reason=reason,
            category="risk_service",
        )
        executor._execution_log.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
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
        if executor._persist_execution_fn is not None:
            try:
                executor._persist_execution_fn(
                    [
                        {
                            "at": datetime.now(timezone.utc).isoformat(),
                            "signal_id": event.signal_id,
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
        return None
    except Exception as exc:
        executor._last_error = str(exc)
        executor._consecutive_failures += 1
        emit_execution_failed(
            executor,
            event,
            order_kind="market",
            reason=str(exc),
            category="dispatch",
        )
        executor._execution_log.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
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
            not executor._circuit_open
            and executor._consecutive_failures >= executor.config.max_consecutive_failures
        ):
            executor._circuit_open = True
            executor._circuit_open_at = datetime.now(timezone.utc)
            logger.error(
                "TradeExecutor: circuit breaker OPENED after %d consecutive failures. "
                "Auto-trading suspended. Will auto-reset in %d minutes or call reset_circuit().",
                executor._consecutive_failures,
                executor.config.circuit_auto_reset_minutes,
            )
        fail_entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "signal_id": event.signal_id,
            "symbol": event.symbol,
            "direction": event.direction,
            "strategy": event.strategy,
            "success": False,
            "error": str(exc),
        }
        if executor._persist_execution_fn is not None:
            try:
                executor._persist_execution_fn([fail_entry])
            except Exception as persist_exc:
                logger.warning(
                    "TradeExecutor: persist fail-entry failed: %s", persist_exc
                )
        return None
