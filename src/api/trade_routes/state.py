from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.params import Param
from fastapi.responses import StreamingResponse

from src.config import build_account_key, list_mt5_accounts, resolve_current_environment
from src.api.deps import get_runtime_read_model, get_trading_query_service
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse, OrderModel, PositionModel, TradingAccountModel
from src.clients.base import MT5TradeError
from src.readmodels.runtime import RuntimeReadModel
from src.trading.application import TradingQueryService

from .common import (
    normalize_optional_status,
    order_model_from_dataclass,
    position_model_from_dataclass,
)
from .view_models import (
    ExecutionContextListView,
    ExposureCloseoutSummaryView,
    PendingOrderStateListView,
    PositionRuntimeStateListView,
    TradeCommandAuditView,
    TradeControlStatusView,
    TradeDailySummaryView,
    TradeEntryStatusView,
    TradeStateAlertsView,
    TradeStateSummaryView,
)

router = APIRouter(tags=["trade"])

_TRADE_STATE_STREAM_POLL_SECONDS = 1.0
_TRADE_STATE_STREAM_SNAPSHOT_LIMIT = 500


def _resolve_param(value: Any) -> Any:
    if isinstance(value, Param):
        default = value.default
        return None if default is ... else default
    return value


def _normalize_optional_datetime(value: Optional[datetime]) -> Optional[datetime]:
    value = _resolve_param(value)
    if not isinstance(value, datetime):
        return None
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _iso_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _normalize_optional_string(value: Any) -> Optional[str]:
    value = _resolve_param(value)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_int(value: Any, *, default: int) -> int:
    value = _resolve_param(value)
    if value is None:
        return default
    return int(value)


def _normalize_bool(value: Any, *, default: bool) -> bool:
    value = _resolve_param(value)
    if value is None:
        return default
    return bool(value)


def _stream_account_alias(service: TradingQueryService) -> str:
    return str(getattr(service, "active_account_alias", "") or "primary")


def _safe_live_positions(
    service: TradingQueryService,
    *,
    symbol: Optional[str],
) -> list[dict[str, Any]]:
    try:
        rows = service.get_positions(symbol, None)
    except Exception:
        return []
    return [position_model_from_dataclass(item).model_dump() for item in rows]


def _safe_live_orders(
    service: TradingQueryService,
    *,
    symbol: Optional[str],
) -> list[dict[str, Any]]:
    try:
        rows = service.get_orders(symbol, None)
    except Exception:
        return []
    return [order_model_from_dataclass(item).model_dump() for item in rows]


def _filter_pending_entries(
    runtime_views: RuntimeReadModel,
    *,
    symbol: Optional[str],
) -> list[dict[str, Any]]:
    payload = runtime_views.active_pending_order_payload(limit=_TRADE_STATE_STREAM_SNAPSHOT_LIMIT)
    items = list(payload.get("items") or [])
    if symbol:
        items = [item for item in items if str(item.get("symbol") or "") == symbol]
    return items


def _filter_command_audits(
    service: TradingQueryService,
    *,
    symbol: Optional[str],
) -> list[dict[str, Any]]:
    pager = getattr(service, "command_audit_page", None)
    if callable(pager):
        result = pager(
            symbol=symbol,
            page=1,
            page_size=50,
            sort="recorded_at_desc",
        )
        return list(result.get("items") or [])
    rows = list(service.recent_command_audits(limit=50) or [])
    if symbol:
        rows = [row for row in rows if str(row.get("symbol") or "") == symbol]
    return rows


def _build_trade_state_stream_snapshot(
    runtime_views: RuntimeReadModel,
    service: TradingQueryService,
    *,
    symbol: Optional[str],
    include_alerts: bool,
    include_positions: bool,
    include_orders: bool,
    include_pending: bool,
) -> dict[str, Any]:
    alerts_payload = runtime_views.trading_state_alerts_summary() if include_alerts else {}
    return {
        "account": _stream_account_alias(service),
        "runtime_mode": runtime_views.runtime_mode_summary(),
        "trade_control": service.trade_control_status()
        or runtime_views.persisted_trade_control_payload(),
        "tradability": runtime_views.tradability_state_summary(),
        "closeout": runtime_views.exposure_closeout_summary(),
        "positions": _safe_live_positions(service, symbol=symbol) if include_positions else [],
        "orders": _safe_live_orders(service, symbol=symbol) if include_orders else [],
        "pending_entries": _filter_pending_entries(runtime_views, symbol=symbol) if include_pending else [],
        "unmanaged_live_positions": runtime_views.unmanaged_live_positions_payload(
            limit=_TRADE_STATE_STREAM_SNAPSHOT_LIMIT
        ),
        "pipeline_events": runtime_views.recent_trade_pipeline_events_payload(
            limit=_TRADE_STATE_STREAM_SNAPSHOT_LIMIT
        ),
        "alerts": list(alerts_payload.get("alerts") or []) if include_alerts else [],
        "recent_command_audits": _filter_command_audits(service, symbol=symbol),
    }


def _index_rows(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = str(row.get(key) or "").strip()
        if value:
            indexed[value] = row
    return indexed


def _snapshot_items(snapshot: dict[str, Any], key: str) -> list[dict[str, Any]]:
    payload = snapshot.get(key)
    if isinstance(payload, dict):
        items = payload.get("items")
        return list(items or [])
    if isinstance(payload, list):
        return list(payload)
    return []


def _position_change_type(
    previous: Optional[dict[str, Any]],
    current: Optional[dict[str, Any]],
) -> str:
    if previous is None and current is not None:
        return "opened"
    if previous is not None and current is None:
        return "closed"
    if previous is None or current is None:
        return "updated"
    try:
        previous_volume = float(previous.get("volume") or 0.0)
        current_volume = float(current.get("volume") or 0.0)
    except (TypeError, ValueError):
        previous_volume = 0.0
        current_volume = 0.0
    if current_volume and previous_volume and current_volume < previous_volume:
        return "partially_closed"
    return "updated"


def _generic_change_type(
    previous: Optional[dict[str, Any]],
    current: Optional[dict[str, Any]],
    *,
    created: str,
    removed: str,
) -> str:
    if previous is None and current is not None:
        return created
    if previous is not None and current is None:
        return removed
    return "updated"


def _next_stream_envelope(
    counters: dict[str, int],
    *,
    account: str,
    event_type: str,
    payload: dict[str, Any],
    symbol: Optional[str] = None,
    trace_id: Optional[str] = None,
    signal_id: Optional[str] = None,
    action_id: Optional[str] = None,
    audit_id: Optional[str] = None,
    state_change: bool = True,
) -> dict[str, Any]:
    counters["sequence"] += 1
    if state_change:
        counters["state_version"] += 1
    event_id = f"trade_state_{counters['sequence']:08d}"
    return {
        "event_id": event_id,
        "stream": "trade_state",
        "schema_version": "1.0",
        "event_type": event_type,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "sequence": counters["sequence"],
        "state_version": counters["state_version"],
        "account": account,
        "symbol": symbol,
        "trace_id": trace_id,
        "signal_id": signal_id,
        "action_id": action_id,
        "audit_id": audit_id,
        "payload": payload,
    }


def _format_trade_state_sse(envelope: dict[str, Any]) -> str:
    payload = json.dumps(envelope, ensure_ascii=False, default=str)
    return f"id: {envelope['event_id']}\nevent: {envelope['event_type']}\ndata: {payload}\n\n"


def _append_change_events(
    events: list[dict[str, Any]],
    counters: dict[str, int],
    *,
    previous_snapshot: dict[str, Any],
    current_snapshot: dict[str, Any],
) -> None:
    account = str(current_snapshot.get("account") or "primary")

    previous_runtime_mode = dict(previous_snapshot.get("runtime_mode") or {})
    current_runtime_mode = dict(current_snapshot.get("runtime_mode") or {})
    if previous_runtime_mode != current_runtime_mode:
        events.append(
            _next_stream_envelope(
                counters,
                account=account,
                event_type="runtime_mode_changed",
                payload={
                    "previous_mode": previous_runtime_mode.get("current_mode"),
                    "current_mode": current_runtime_mode.get("current_mode"),
                    "configured_mode": current_runtime_mode.get("configured_mode"),
                    "reason": current_runtime_mode.get("last_transition_reason"),
                    "actor": current_runtime_mode.get("last_actor"),
                    "idempotency_key": current_runtime_mode.get("last_idempotency_key"),
                },
                action_id=current_runtime_mode.get("last_action_id"),
                audit_id=current_runtime_mode.get("last_audit_id")
                or current_runtime_mode.get("last_action_id"),
            )
        )

    previous_trade_control = dict(previous_snapshot.get("trade_control") or {})
    current_trade_control = dict(current_snapshot.get("trade_control") or {})
    if previous_trade_control != current_trade_control:
        events.append(
            _next_stream_envelope(
                counters,
                account=account,
                event_type="trade_control_changed",
                payload={
                    "previous": previous_trade_control,
                    "current": current_trade_control,
                    "reason": current_trade_control.get("reason"),
                    "actor": current_trade_control.get("actor"),
                    "idempotency_key": current_trade_control.get("idempotency_key"),
                },
                action_id=current_trade_control.get("action_id"),
                audit_id=current_trade_control.get("audit_id")
                or current_trade_control.get("action_id"),
            )
        )

    previous_tradability = dict(previous_snapshot.get("tradability") or {})
    current_tradability = dict(current_snapshot.get("tradability") or {})
    if previous_tradability != current_tradability:
        events.append(
            _next_stream_envelope(
                counters,
                account=account,
                event_type="risk_state_changed",
                payload={
                    "previous": previous_tradability,
                    "current": current_tradability,
                },
            )
        )

    previous_closeout = dict(previous_snapshot.get("closeout") or {})
    current_closeout = dict(current_snapshot.get("closeout") or {})
    previous_closeout_status = str(previous_closeout.get("status") or "idle")
    current_closeout_status = str(current_closeout.get("status") or "idle")
    if previous_closeout_status in {"idle", "unavailable"} and current_closeout_status not in {"idle", "unavailable"}:
        events.append(
            _next_stream_envelope(
                counters,
                account=account,
                event_type="closeout_started",
                payload=current_closeout,
                action_id=current_closeout.get("action_id"),
                audit_id=current_closeout.get("audit_id")
                or current_closeout.get("action_id"),
            )
        )
    if previous_closeout_status != current_closeout_status and current_closeout_status in {
        "completed",
        "failed",
        "partial_failure",
    }:
        events.append(
            _next_stream_envelope(
                counters,
                account=account,
                event_type="closeout_finished",
                payload=current_closeout,
                action_id=current_closeout.get("action_id"),
                audit_id=current_closeout.get("audit_id")
                or current_closeout.get("action_id"),
            )
        )

    previous_positions = _index_rows(list(previous_snapshot.get("positions") or []), "ticket")
    current_positions = _index_rows(list(current_snapshot.get("positions") or []), "ticket")
    for ticket in sorted(set(previous_positions) | set(current_positions)):
        previous_item = previous_positions.get(ticket)
        current_item = current_positions.get(ticket)
        if previous_item == current_item:
            continue
        payload_position = current_item or previous_item or {}
        events.append(
            _next_stream_envelope(
                counters,
                account=account,
                event_type="position_changed",
                payload={
                    "change_type": _position_change_type(previous_item, current_item),
                    "ticket": payload_position.get("ticket"),
                    "position": payload_position,
                },
                symbol=payload_position.get("symbol"),
            )
        )

    previous_orders = _index_rows(list(previous_snapshot.get("orders") or []), "ticket")
    current_orders = _index_rows(list(current_snapshot.get("orders") or []), "ticket")
    for ticket in sorted(set(previous_orders) | set(current_orders)):
        previous_item = previous_orders.get(ticket)
        current_item = current_orders.get(ticket)
        if previous_item == current_item:
            continue
        payload_order = current_item or previous_item or {}
        events.append(
            _next_stream_envelope(
                counters,
                account=account,
                event_type="order_changed",
                payload={
                    "change_type": _generic_change_type(
                        previous_item,
                        current_item,
                        created="submitted",
                        removed="removed",
                    ),
                    "ticket": payload_order.get("ticket"),
                    "order": payload_order,
                },
                symbol=payload_order.get("symbol"),
            )
        )

    previous_pending = _index_rows(list(previous_snapshot.get("pending_entries") or []), "order_ticket")
    current_pending = _index_rows(list(current_snapshot.get("pending_entries") or []), "order_ticket")
    for order_ticket in sorted(set(previous_pending) | set(current_pending)):
        previous_item = previous_pending.get(order_ticket)
        current_item = current_pending.get(order_ticket)
        if previous_item == current_item:
            continue
        payload_pending = current_item or previous_item or {}
        change_type = _generic_change_type(
            previous_item,
            current_item,
            created=str((current_item or {}).get("status") or "created"),
            removed="removed",
        )
        if current_item is not None and previous_item is not None:
            change_type = str(current_item.get("status") or "updated")
        events.append(
            _next_stream_envelope(
                counters,
                account=account,
                event_type="pending_entry_changed",
                payload={
                    "change_type": change_type,
                    "pending_entry": payload_pending,
                },
                symbol=payload_pending.get("symbol"),
                signal_id=payload_pending.get("signal_id"),
            )
        )

    previous_alerts = _index_rows(list(previous_snapshot.get("alerts") or []), "code")
    current_alerts = _index_rows(list(current_snapshot.get("alerts") or []), "code")
    for code in sorted(set(previous_alerts) | set(current_alerts)):
        previous_item = previous_alerts.get(code)
        current_item = current_alerts.get(code)
        if previous_item == current_item:
            continue
        if current_item is None:
            events.append(
                _next_stream_envelope(
                    counters,
                    account=account,
                    event_type="alert_resolved",
                    payload={
                        "alert_id": code,
                        "resolved_by": "system",
                        "resolution": "no_longer_active",
                    },
                )
            )
            continue
        events.append(
            _next_stream_envelope(
                counters,
                account=account,
                event_type="alert_raised",
                payload={
                    "alert_id": code,
                    "severity": current_item.get("severity"),
                    "code": current_item.get("code"),
                    "message": current_item.get("message"),
                    "details": current_item.get("details") or {},
                    "status": "active",
                },
            )
        )

    previous_unmanaged = _index_rows(_snapshot_items(previous_snapshot, "unmanaged_live_positions"), "ticket")
    current_unmanaged = _index_rows(_snapshot_items(current_snapshot, "unmanaged_live_positions"), "ticket")
    for ticket in sorted(set(previous_unmanaged) | set(current_unmanaged)):
        previous_item = previous_unmanaged.get(ticket)
        current_item = current_unmanaged.get(ticket)
        if previous_item == current_item:
            continue
        if current_item is not None:
            payload_item = dict(current_item)
            events.append(
                _next_stream_envelope(
                    counters,
                    account=account,
                    event_type="unmanaged_position_detected",
                    payload=payload_item,
                    symbol=payload_item.get("symbol"),
                )
            )
        else:
            payload_item = dict(previous_item or {})
            events.append(
                _next_stream_envelope(
                    counters,
                    account=account,
                    event_type="unmanaged_position_resolved",
                    payload=payload_item,
                    symbol=payload_item.get("symbol"),
                )
            )

    previous_pipeline = _index_rows(_snapshot_items(previous_snapshot, "pipeline_events"), "id")
    current_pipeline = _index_rows(_snapshot_items(current_snapshot, "pipeline_events"), "id")
    new_pipeline_ids = [key for key in current_pipeline.keys() if key not in previous_pipeline]
    new_pipeline_ids.sort(
        key=lambda item: (
            str((current_pipeline.get(item) or {}).get("recorded_at") or ""),
            int(str(item) or "0"),
        )
    )
    for pipeline_id in new_pipeline_ids:
        event = dict(current_pipeline[pipeline_id])
        payload = dict(event.get("payload") or {})
        payload.setdefault("pipeline_event_id", event.get("id"))
        events.append(
            _next_stream_envelope(
                counters,
                account=account,
                event_type=str(event.get("event_type") or "pipeline_event"),
                payload=payload,
                symbol=event.get("symbol"),
                trace_id=event.get("trace_id"),
                signal_id=event.get("signal_id"),
                action_id=event.get("action_id"),
                audit_id=event.get("command_id"),
            )
        )

    previous_audits = _index_rows(list(previous_snapshot.get("recent_command_audits") or []), "operation_id")
    current_audits = _index_rows(list(current_snapshot.get("recent_command_audits") or []), "operation_id")
    new_audit_ids = [key for key in current_audits.keys() if key not in previous_audits]
    new_audit_ids.sort(
        key=lambda item: str((current_audits.get(item) or {}).get("recorded_at") or "")
    )
    for audit_id in new_audit_ids:
        audit = current_audits[audit_id]
        events.append(
            _next_stream_envelope(
                counters,
                account=account,
                event_type="command_audit_appended",
                payload=audit,
                symbol=audit.get("symbol"),
                trace_id=audit.get("trace_id"),
                signal_id=audit.get("signal_id"),
                action_id=audit.get("operation_id"),
                audit_id=audit.get("audit_id") or audit.get("operation_id"),
            )
        )


@router.get("/trade/daily_summary", response_model=ApiResponse[TradeDailySummaryView])
def trade_daily_summary(
    service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(
        data=service.daily_trade_summary(),
        metadata={
            "operation": "daily_summary",
            "account_alias": service.active_account_alias,
        },
    )


@router.get("/trade/control", response_model=ApiResponse[TradeControlStatusView])
def trade_control_status(
    service: TradingQueryService = Depends(get_trading_query_service),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[TradeControlStatusView]:
    payload = {
        "trade_control": service.trade_control_status(),
        "persisted_trade_control": runtime_views.persisted_trade_control_payload(),
        "trading_state": runtime_views.trading_state_summary(
            pending_limit=10,
            position_limit=10,
        ),
        "executor": runtime_views.trade_executor_summary(),
    }
    return ApiResponse.success_response(
        data=payload,
        metadata={"operation": "trade_control_status"},
    )


@router.get("/trade/state", response_model=ApiResponse[TradeStateSummaryView])
def trade_state_summary(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[TradeStateSummaryView]:
    return ApiResponse.success_response(
        data=runtime_views.trading_state_summary(
            pending_limit=20,
            position_limit=20,
        ),
        metadata={"operation": "trade_state_summary"},
    )


@router.get("/trade/state/alerts", response_model=ApiResponse[TradeStateAlertsView])
def trade_state_alerts_summary(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[TradeStateAlertsView]:
    return ApiResponse.success_response(
        data=runtime_views.trading_state_alerts_summary(),
        metadata={"operation": "trade_state_alerts_summary"},
    )


@router.get(
    "/trade/state/closeout",
    response_model=ApiResponse[ExposureCloseoutSummaryView],
)
def trade_state_closeout_summary(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[ExposureCloseoutSummaryView]:
    return ApiResponse.success_response(
        data=runtime_views.exposure_closeout_summary(),
        metadata={"operation": "trade_state_closeout_summary"},
    )


@router.get(
    "/trade/state/pending/active",
    response_model=ApiResponse[PendingOrderStateListView],
)
def trade_active_pending_state_list(
    limit: int = Query(default=50, ge=1, le=500),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[PendingOrderStateListView]:
    return ApiResponse.success_response(
        data=runtime_views.active_pending_order_payload(limit=limit),
        metadata={
            "operation": "trade_active_pending_state_list",
            "limit": limit,
        },
    )


@router.get(
    "/trade/state/execution-contexts",
    response_model=ApiResponse[ExecutionContextListView],
)
def trade_pending_execution_context_list(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[ExecutionContextListView]:
    return ApiResponse.success_response(
        data=runtime_views.pending_execution_context_payload(),
        metadata={"operation": "trade_pending_execution_context_list"},
    )


@router.get(
    "/trade/state/pending/lifecycle",
    response_model=ApiResponse[PendingOrderStateListView],
)
def trade_pending_lifecycle_state_list(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[PendingOrderStateListView]:
    normalized_status = normalize_optional_status(status)
    statuses = [normalized_status] if normalized_status else None
    return ApiResponse.success_response(
        data=runtime_views.pending_order_state_payload(
            statuses=statuses,
            limit=limit,
        ),
        metadata={
            "operation": "trade_pending_lifecycle_state_list",
            "status": normalized_status,
            "limit": limit,
        },
    )


@router.get(
    "/trade/state/positions",
    response_model=ApiResponse[PositionRuntimeStateListView],
)
def trade_position_state_list(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[PositionRuntimeStateListView]:
    normalized_status = normalize_optional_status(status)
    statuses = [normalized_status] if normalized_status else None
    return ApiResponse.success_response(
        data=runtime_views.position_runtime_state_payload(
            statuses=statuses,
            limit=limit,
        ),
        metadata={
            "operation": "trade_position_state_list",
            "status": normalized_status,
            "limit": limit,
        },
    )


@router.get("/trade/entry_status", response_model=ApiResponse[TradeEntryStatusView])
def trade_entry_status(
    symbol: Optional[str] = Query(default=None),
    volume: float = Query(default=0.1, gt=0),
    side: str = Query(default="buy"),
    order_kind: str = Query(default="market"),
    service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(
        data=service.entry_to_order_status(
            symbol=symbol,
            volume=volume,
            side=side,
            order_kind=order_kind,
        ),
        metadata={
            "operation": "entry_status",
            "account_alias": service.active_account_alias,
        },
    )


@router.get("/positions", response_model=ApiResponse[List[PositionModel]])
def positions(
    symbol: Optional[str] = Query(default=None, description="trading symbol"),
    magic: Optional[int] = Query(default=None, description="magic id"),
    service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[List[PositionModel]]:
    active_alias = service.active_account_alias
    try:
        rows = service.get_positions(symbol, magic)
        items = [position_model_from_dataclass(p) for p in rows]
        return ApiResponse.success_response(
            data=items,
            metadata={
                "operation": "get_positions",
                "account_alias": active_alias,
                "symbol": symbol,
                "magic": magic,
                "count": len(items),
                "total_volume": sum(p.volume for p in rows),
                "total_profit": (
                    sum(p.profit for p in rows)
                    if rows and hasattr(rows[0], "profit")
                    else None
                ),
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.POSITION_NOT_FOUND,
            error_message=f"Get positions error: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                "symbol": symbol,
                "magic": magic,
            },
        )


@router.get("/orders", response_model=ApiResponse[List[OrderModel]])
def orders(
    symbol: Optional[str] = Query(default=None, description="trading symbol"),
    magic: Optional[int] = Query(default=None, description="magic id"),
    service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[List[OrderModel]]:
    active_alias = service.active_account_alias
    try:
        rows = service.get_orders(symbol, magic)
        items = [order_model_from_dataclass(o) for o in rows]
        return ApiResponse.success_response(
            data=items,
            metadata={
                "operation": "get_orders",
                "account_alias": active_alias,
                "symbol": symbol,
                "magic": magic,
                "count": len(items),
                "total_volume": sum(o.volume for o in rows),
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.ORDER_NOT_FOUND,
            error_message=f"Get orders error: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                "symbol": symbol,
                "magic": magic,
            },
        )


@router.get("/trade/accounts", response_model=ApiResponse[List[TradingAccountModel]])
def trading_accounts(
    service: TradingQueryService = Depends(get_trading_query_service),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[List[TradingAccountModel]]:
    risk_payload = runtime_views.account_risk_states_payload(limit=500)
    risk_by_account_key = {
        str(item.get("account_key") or ""): item
        for item in list(risk_payload.get("items") or [])
        if str(item.get("account_key") or "").strip()
    }
    configured_accounts = []
    active_account_alias = service.active_account_alias
    environment = (
        runtime_views.runtime_identity.environment
        if runtime_views.runtime_identity is not None
        else resolve_current_environment()
    )
    if environment is None:
        raise HTTPException(status_code=500, detail="runtime environment is not configured")
    for settings in list_mt5_accounts():
        account_key = build_account_key(
            environment,
            settings.mt5_server,
            settings.mt5_login,
        )
        risk_state = risk_by_account_key.get(account_key)
        tradability = None
        unmanaged = None
        if settings.account_alias == active_account_alias:
            tradability = runtime_views.tradability_state_summary()
            unmanaged = runtime_views.unmanaged_live_positions_payload(limit=20)
        elif isinstance(risk_state, dict):
            auto_entry_enabled = bool(risk_state.get("auto_entry_enabled", True))
            close_only_mode = bool(risk_state.get("close_only_mode", False))
            circuit_open = bool(risk_state.get("circuit_open", False))
            should_block_new_trades = bool(risk_state.get("should_block_new_trades", False))
            quote_stale = bool(risk_state.get("quote_stale", False))
            tradability = {
                "runtime_present": True,
                "admission_enabled": auto_entry_enabled and not close_only_mode,
                "market_data_fresh": not quote_stale,
                "quote_health": {
                    "stale": quote_stale,
                    "age_seconds": (
                        risk_state.get("metadata", {}) or {}
                    ).get("quote_health", {}).get("age_seconds"),
                    "stale_threshold_seconds": (
                        risk_state.get("metadata", {}) or {}
                    ).get("quote_health", {}).get("stale_threshold_seconds"),
                },
                "session_allowed": {"status": "unknown", "reason": None},
                "economic_guard": {"status": "warn_only", "degraded": False},
                "auto_entry_enabled": auto_entry_enabled,
                "close_only_mode": close_only_mode,
                "margin_guard": (risk_state.get("metadata", {}) or {}).get("margin_guard", {}),
                "circuit_open": circuit_open,
                "tradable": bool(
                    auto_entry_enabled
                    and not close_only_mode
                    and not circuit_open
                    and not should_block_new_trades
                    and not quote_stale
                ),
            }
            unmanaged = {"count": 0, "items": [], "reason_counts": {}}
        configured_accounts.append(
            {
                "alias": settings.account_alias,
                "label": settings.account_label or settings.account_alias,
                "account_key": account_key,
                "login": settings.mt5_login,
                "server": settings.mt5_server,
                "environment": environment,
                "timezone": settings.timezone,
                "enabled": settings.enabled,
                "default": settings.account_alias == active_account_alias,
                "active": settings.account_alias == active_account_alias,
                "risk_state": risk_state,
                "tradability": tradability,
                "unmanaged_live_positions": unmanaged,
            }
        )
    accounts = [
        TradingAccountModel(**item)
        for item in configured_accounts
    ]
    return ApiResponse.success_response(
        data=accounts,
        metadata={
            "operation": "trading_accounts",
            "mode": "account_risk_projection",
            "active_account_alias": active_account_alias,
            "count": len(accounts),
        },
    )


@router.get("/trade/command-audits", response_model=ApiResponse[List[TradeCommandAuditView]])
def trade_command_audits(
    command_type: Optional[str] = Query(default=None, description="command type"),
    status: Optional[str] = Query(default=None, description="operation status"),
    symbol: Optional[str] = Query(default=None, description="symbol filter"),
    signal_id: Optional[str] = Query(default=None, description="signal id filter"),
    trace_id: Optional[str] = Query(default=None, description="trace id filter"),
    actor: Optional[str] = Query(default=None, description="actor filter"),
    from_time: Optional[datetime] = Query(default=None, alias="from"),
    to_time: Optional[datetime] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: Optional[int] = Query(default=None, ge=1, le=500),
    sort: str = Query(
        default="recorded_at_desc",
        pattern="^(recorded_at_desc|recorded_at_asc|asc|desc)$",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[List[dict]]:
    command_type = _normalize_optional_string(command_type)
    status = _normalize_optional_string(status)
    symbol = _normalize_optional_string(symbol)
    signal_id = _normalize_optional_string(signal_id)
    trace_id = _normalize_optional_string(trace_id)
    actor = _normalize_optional_string(actor)
    page = _normalize_int(page, default=1)
    sort = _normalize_optional_string(sort) or "recorded_at_desc"
    effective_page_size = _normalize_int(
        page_size,
        default=_normalize_int(limit, default=100),
    )
    normalized_from_time = _normalize_optional_datetime(from_time)
    normalized_to_time = _normalize_optional_datetime(to_time)
    page_fn = getattr(service, "command_audit_page", None)
    if callable(page_fn):
        result = page_fn(
            command_type=command_type,
            status=status,
            symbol=symbol,
            signal_id=signal_id,
            trace_id=trace_id,
            actor=actor,
            from_time=normalized_from_time,
            to_time=normalized_to_time,
            page=page,
            page_size=effective_page_size,
            sort=sort,
        )
        items = list(result.get("items") or [])
        total = int(result.get("total") or 0)
    else:
        items = service.recent_command_audits(
            command_type=command_type,
            status=status,
            limit=effective_page_size,
        )
        if symbol is not None:
            items = [item for item in items if str(item.get("symbol") or "") == symbol]
        total = len(items)
    return ApiResponse.success_response(
        data=items,
        metadata={
            "operation": "trade_command_audits",
            "account_alias": service.active_account_alias,
            "command_type": command_type,
            "status": status,
            "symbol": symbol,
            "signal_id": signal_id,
            "trace_id": trace_id,
            "actor": actor,
            "from": _iso_or_none(normalized_from_time),
            "to": _iso_or_none(normalized_to_time),
            "page": page,
            "page_size": effective_page_size,
            "total": total,
            "sort": sort,
            "count": len(items),
        },
    )


@router.get(
    "/trade/positions/{ticket}/sl-tp-history",
    response_model=ApiResponse[list],
    summary="查询持仓的 SL/TP 修改历史",
)
async def get_sl_tp_history(
    ticket: int,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    read_model: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[list]:
    try:
        db = read_model.db_writer
        rows = db.fetch_position_sl_tp_history(
            position_ticket=ticket, limit=limit, offset=offset,
        )
        # datetime 转字符串
        for row in rows:
            for key in ("recorded_at",):
                val = row.get(key)
                if val is not None and hasattr(val, "isoformat"):
                    row[key] = val.isoformat()
        return ApiResponse.success_response(
            data=rows,
            metadata={"position_ticket": ticket, "count": len(rows)},
        )
    except Exception as exc:
        return ApiResponse.error_response(str(exc), error_code="INTERNAL_ERROR")


@router.get("/trade/state/stream")
async def trade_state_stream(
    request: Request,
    symbol: Optional[str] = Query(default=None),
    account: Optional[str] = Query(default=None),
    include_snapshot: bool = Query(default=True),
    include_alerts: bool = Query(default=True),
    include_positions: bool = Query(default=True),
    include_orders: bool = Query(default=True),
    include_pending: bool = Query(default=True),
    heartbeat_seconds: int = Query(default=15, ge=5, le=120),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
    service: TradingQueryService = Depends(get_trading_query_service),
) -> StreamingResponse:
    symbol = _normalize_optional_string(symbol)
    account = _normalize_optional_string(account)
    include_snapshot = _normalize_bool(include_snapshot, default=True)
    include_alerts = _normalize_bool(include_alerts, default=True)
    include_positions = _normalize_bool(include_positions, default=True)
    include_orders = _normalize_bool(include_orders, default=True)
    include_pending = _normalize_bool(include_pending, default=True)
    heartbeat_seconds = _normalize_int(heartbeat_seconds, default=15)
    active_account = _stream_account_alias(service)
    if account and account != active_account:
        raise HTTPException(status_code=404, detail=f"unknown trading account: {account}")

    async def event_generator():  # type: ignore[no-untyped-def]
        counters = {"sequence": 0, "state_version": 0}
        last_heartbeat = time.monotonic()
        previous_snapshot = await asyncio.to_thread(
            _build_trade_state_stream_snapshot,
            runtime_views,
            service,
            symbol=symbol,
            include_alerts=include_alerts,
            include_positions=include_positions,
            include_orders=include_orders,
            include_pending=include_pending,
        )

        last_event_id = request.headers.get("last-event-id") or request.headers.get("Last-Event-ID")
        if last_event_id:
            yield _format_trade_state_sse(
                _next_stream_envelope(
                    counters,
                    account=active_account,
                    event_type="resync_required",
                    payload={
                        "reason": "event_buffer_missed",
                        "expected_last_event_id": last_event_id,
                    },
                    state_change=False,
                )
            )

        if include_snapshot:
            yield _format_trade_state_sse(
                _next_stream_envelope(
                    counters,
                    account=active_account,
                    event_type="state_snapshot",
                    payload={
                        "runtime_mode": previous_snapshot.get("runtime_mode") or {},
                        "trade_control": previous_snapshot.get("trade_control") or {},
                        "tradability": previous_snapshot.get("tradability") or {},
                        "closeout": previous_snapshot.get("closeout") or {},
                        "positions": previous_snapshot.get("positions") or [],
                        "orders": previous_snapshot.get("orders") or [],
                        "pending_entries": previous_snapshot.get("pending_entries") or [],
                        "unmanaged_live_positions": (
                            previous_snapshot.get("unmanaged_live_positions") or {}
                        ),
                        "pipeline_events": (
                            previous_snapshot.get("pipeline_events") or {}
                        ),
                        "alerts": previous_snapshot.get("alerts") or [],
                    },
                    symbol=symbol,
                )
            )

        while True:
            await asyncio.sleep(_TRADE_STATE_STREAM_POLL_SECONDS)
            current_snapshot = await asyncio.to_thread(
                _build_trade_state_stream_snapshot,
                runtime_views,
                service,
                symbol=symbol,
                include_alerts=include_alerts,
                include_positions=include_positions,
                include_orders=include_orders,
                include_pending=include_pending,
            )
            events: list[dict[str, Any]] = []
            _append_change_events(
                events,
                counters,
                previous_snapshot=previous_snapshot,
                current_snapshot=current_snapshot,
            )
            if events:
                previous_snapshot = current_snapshot
                for event in events:
                    yield _format_trade_state_sse(event)
                last_heartbeat = time.monotonic()
                continue

            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_seconds:
                yield _format_trade_state_sse(
                    _next_stream_envelope(
                        counters,
                        account=active_account,
                        event_type="heartbeat",
                        payload={"healthy": True, "lag_ms": 0},
                        symbol=symbol,
                        state_change=False,
                    )
                )
                last_heartbeat = now

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
