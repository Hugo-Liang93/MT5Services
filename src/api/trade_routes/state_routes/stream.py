from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src.api.deps import get_runtime_read_model, get_trading_query_service
from src.readmodels.runtime import RuntimeReadModel
from src.trading.application.services import TradingQueryService

from ..common import order_model_from_dataclass, position_model_from_dataclass
from .common import normalize_bool, normalize_int, normalize_optional_string

router = APIRouter(tags=["trade"])

_TRADE_STATE_STREAM_POLL_SECONDS = 1.0
_TRADE_STATE_STREAM_SNAPSHOT_LIMIT = 500

# ── P9 Phase 2.1: event metadata 表（tier / entity_scope / changed_blocks） ──
# Tier 与 docs/design/quantx-data-freshness-tiering.md §6.2 对齐：
#   T0 = critical realtime（< 1s，禁用按钮 if stale）
#   T1 = operational realtime（1-5s）
# changed_blocks 与 /v1/execution/workbench 9 块名称对齐，前端按此精确重拉。
_EVENT_METADATA: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "runtime_mode_changed": ("T0", "system", ("execution",)),
    "trade_control_changed": ("T0", "account", ("execution",)),
    "risk_state_changed": ("T0", "account", ("execution", "risk")),
    "closeout_started": ("T1", "account", ("execution", "positions", "orders")),
    "closeout_finished": (
        "T1",
        "account",
        ("execution", "positions", "orders", "exposure"),
    ),
    "position_changed": ("T1", "symbol", ("positions", "exposure")),
    "order_changed": ("T1", "symbol", ("orders",)),
    "pending_entry_changed": ("T1", "symbol", ("pending",)),
    "alert_raised": ("T1", "account", ("execution",)),
    "alert_resolved": ("T1", "account", ("execution",)),
    "unmanaged_position_detected": ("T1", "symbol", ("positions",)),
    "unmanaged_position_resolved": ("T1", "symbol", ("positions",)),
    "command_audit_appended": ("T1", "account", ("events", "relatedObjects")),
    "state_snapshot": (
        "T0",
        "account",
        (
            "execution",
            "risk",
            "positions",
            "orders",
            "pending",
            "exposure",
            "events",
            "relatedObjects",
            "marketContext",
            "stream",
        ),
    ),
    # resync_required: 前端必须全量重拉，changed_blocks 留空表示"无法精确指明"
    "resync_required": ("T0", "account", ()),
    "heartbeat": ("static", "system", ()),
}

# Pipeline 透传事件（command_* / intent_*）的默认元数据
_PIPELINE_DEFAULT_METADATA: tuple[str, str, tuple[str, ...]] = (
    "T3",
    "account",
    ("events",),
)


def _resolve_event_metadata(event_type: str) -> tuple[str, str, tuple[str, ...]]:
    """按 event_type 查 metadata；未知类型走 pipeline 透传默认。"""
    return _EVENT_METADATA.get(event_type, _PIPELINE_DEFAULT_METADATA)


def stream_account_alias(service: TradingQueryService) -> str:
    return str(getattr(service, "active_account_alias", "") or "primary")


def safe_live_positions(
    service: TradingQueryService,
    *,
    symbol: Optional[str],
) -> list[dict[str, Any]]:
    try:
        rows = service.get_positions(symbol, None)
    except Exception:
        return []
    return [position_model_from_dataclass(item).model_dump() for item in rows]


def safe_live_orders(
    service: TradingQueryService,
    *,
    symbol: Optional[str],
) -> list[dict[str, Any]]:
    try:
        rows = service.get_orders(symbol, None)
    except Exception:
        return []
    return [order_model_from_dataclass(item).model_dump() for item in rows]


def filter_pending_entries(
    runtime_views: RuntimeReadModel,
    *,
    symbol: Optional[str],
) -> list[dict[str, Any]]:
    payload = runtime_views.active_pending_order_payload(
        limit=_TRADE_STATE_STREAM_SNAPSHOT_LIMIT
    )
    items = list(payload.get("items") or [])
    if symbol:
        items = [item for item in items if str(item.get("symbol") or "") == symbol]
    return items


def filter_command_audits(
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


def build_trade_state_stream_snapshot(
    runtime_views: RuntimeReadModel,
    service: TradingQueryService,
    *,
    symbol: Optional[str],
    include_alerts: bool,
    include_positions: bool,
    include_orders: bool,
    include_pending: bool,
) -> dict[str, Any]:
    alerts_payload = (
        runtime_views.trading_state_alerts_summary() if include_alerts else {}
    )
    return {
        "account": stream_account_alias(service),
        "runtime_mode": runtime_views.runtime_mode_summary(),
        "trade_control": service.trade_control_status()
        or runtime_views.persisted_trade_control_payload(),
        "tradability": runtime_views.tradability_state_summary(),
        "closeout": runtime_views.exposure_closeout_summary(),
        "positions": (
            safe_live_positions(service, symbol=symbol) if include_positions else []
        ),
        "orders": safe_live_orders(service, symbol=symbol) if include_orders else [],
        "pending_entries": (
            filter_pending_entries(runtime_views, symbol=symbol)
            if include_pending
            else []
        ),
        "unmanaged_live_positions": runtime_views.unmanaged_live_positions_payload(
            limit=_TRADE_STATE_STREAM_SNAPSHOT_LIMIT
        ),
        "pipeline_events": runtime_views.recent_trade_pipeline_events_payload(
            limit=_TRADE_STATE_STREAM_SNAPSHOT_LIMIT
        ),
        "alerts": list(alerts_payload.get("alerts") or []) if include_alerts else [],
        "recent_command_audits": filter_command_audits(service, symbol=symbol),
    }


def index_rows(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = str(row.get(key) or "").strip()
        if value:
            indexed[value] = row
    return indexed


def snapshot_items(snapshot: dict[str, Any], key: str) -> list[dict[str, Any]]:
    payload = snapshot.get(key)
    if isinstance(payload, dict):
        return list(payload.get("items") or [])
    if isinstance(payload, list):
        return list(payload)
    return []


def position_change_type(
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


def generic_change_type(
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


def next_stream_envelope(
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
    tier: Optional[str] = None,
    entity_scope: Optional[str] = None,
    changed_blocks: Optional[list[str]] = None,
) -> dict[str, Any]:
    """构造 SSE envelope（schema 1.1）。

    P9 Phase 2.1: 新增 ``tier`` / ``entity_scope`` / ``changed_blocks`` 字段，
    前端按 tier 决定刷新优先级、按 changed_blocks 精确局部重拉、按
    snapshot_version 与 ``/v1/execution/workbench`` 对账。

    显式传入的 tier/entity_scope/changed_blocks 优先于 ``_EVENT_METADATA``
    查表默认值，用于 pipeline 透传等需要覆盖默认的特殊场景。
    """
    counters["sequence"] += 1
    if state_change:
        counters["state_version"] += 1
    event_id = f"trade_state_{counters['sequence']:08d}"

    default_tier, default_scope, default_blocks = _resolve_event_metadata(event_type)
    resolved_tier = tier if tier is not None else default_tier
    resolved_scope = entity_scope if entity_scope is not None else default_scope
    resolved_blocks = (
        list(changed_blocks) if changed_blocks is not None else list(default_blocks)
    )

    return {
        "event_id": event_id,
        "stream": "trade_state",
        "schema_version": "1.1",
        "event_type": event_type,
        # P9 Phase 2.1 新增字段：
        "tier": resolved_tier,
        "entity_scope": resolved_scope,
        "changed_blocks": resolved_blocks,
        # snapshot_version 与 /v1/execution/workbench 的 state_version 同名同值；
        # state_version 保留作向后兼容字段。
        "snapshot_version": counters["state_version"],
        "state_version": counters["state_version"],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "sequence": counters["sequence"],
        "account": account,
        "symbol": symbol,
        "trace_id": trace_id,
        "signal_id": signal_id,
        "action_id": action_id,
        "audit_id": audit_id,
        "payload": payload,
    }


def format_trade_state_sse(envelope: dict[str, Any]) -> str:
    payload = json.dumps(envelope, ensure_ascii=False, default=str)
    return f"id: {envelope['event_id']}\nevent: {envelope['event_type']}\ndata: {payload}\n\n"


def append_change_events(
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
            next_stream_envelope(
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
            next_stream_envelope(
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
            next_stream_envelope(
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
    if previous_closeout_status in {
        "idle",
        "unavailable",
    } and current_closeout_status not in {"idle", "unavailable"}:
        events.append(
            next_stream_envelope(
                counters,
                account=account,
                event_type="closeout_started",
                payload=current_closeout,
                action_id=current_closeout.get("action_id"),
                audit_id=current_closeout.get("audit_id")
                or current_closeout.get("action_id"),
            )
        )
    if (
        previous_closeout_status != current_closeout_status
        and current_closeout_status
        in {
            "completed",
            "failed",
            "partial_failure",
        }
    ):
        events.append(
            next_stream_envelope(
                counters,
                account=account,
                event_type="closeout_finished",
                payload=current_closeout,
                action_id=current_closeout.get("action_id"),
                audit_id=current_closeout.get("audit_id")
                or current_closeout.get("action_id"),
            )
        )

    previous_positions = index_rows(
        list(previous_snapshot.get("positions") or []), "ticket"
    )
    current_positions = index_rows(
        list(current_snapshot.get("positions") or []), "ticket"
    )
    for ticket in sorted(set(previous_positions) | set(current_positions)):
        previous_item = previous_positions.get(ticket)
        current_item = current_positions.get(ticket)
        if previous_item == current_item:
            continue
        payload_position = current_item or previous_item or {}
        events.append(
            next_stream_envelope(
                counters,
                account=account,
                event_type="position_changed",
                payload={
                    "change_type": position_change_type(previous_item, current_item),
                    "ticket": payload_position.get("ticket"),
                    "position": payload_position,
                },
                symbol=payload_position.get("symbol"),
            )
        )

    previous_orders = index_rows(list(previous_snapshot.get("orders") or []), "ticket")
    current_orders = index_rows(list(current_snapshot.get("orders") or []), "ticket")
    for ticket in sorted(set(previous_orders) | set(current_orders)):
        previous_item = previous_orders.get(ticket)
        current_item = current_orders.get(ticket)
        if previous_item == current_item:
            continue
        payload_order = current_item or previous_item or {}
        events.append(
            next_stream_envelope(
                counters,
                account=account,
                event_type="order_changed",
                payload={
                    "change_type": generic_change_type(
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

    previous_pending = index_rows(
        list(previous_snapshot.get("pending_entries") or []), "order_ticket"
    )
    current_pending = index_rows(
        list(current_snapshot.get("pending_entries") or []), "order_ticket"
    )
    for order_ticket in sorted(set(previous_pending) | set(current_pending)):
        previous_item = previous_pending.get(order_ticket)
        current_item = current_pending.get(order_ticket)
        if previous_item == current_item:
            continue
        payload_pending = current_item or previous_item or {}
        change_type = generic_change_type(
            previous_item,
            current_item,
            created=str((current_item or {}).get("status") or "created"),
            removed="removed",
        )
        if current_item is not None and previous_item is not None:
            change_type = str(current_item.get("status") or "updated")
        events.append(
            next_stream_envelope(
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

    previous_alerts = index_rows(list(previous_snapshot.get("alerts") or []), "code")
    current_alerts = index_rows(list(current_snapshot.get("alerts") or []), "code")
    for code in sorted(set(previous_alerts) | set(current_alerts)):
        previous_item = previous_alerts.get(code)
        current_item = current_alerts.get(code)
        if previous_item == current_item:
            continue
        if current_item is None:
            events.append(
                next_stream_envelope(
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
            next_stream_envelope(
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

    previous_unmanaged = index_rows(
        snapshot_items(previous_snapshot, "unmanaged_live_positions"), "ticket"
    )
    current_unmanaged = index_rows(
        snapshot_items(current_snapshot, "unmanaged_live_positions"), "ticket"
    )
    for ticket in sorted(set(previous_unmanaged) | set(current_unmanaged)):
        previous_item = previous_unmanaged.get(ticket)
        current_item = current_unmanaged.get(ticket)
        if previous_item == current_item:
            continue
        payload_item = dict(current_item or previous_item or {})
        event_type = (
            "unmanaged_position_detected"
            if current_item is not None
            else "unmanaged_position_resolved"
        )
        events.append(
            next_stream_envelope(
                counters,
                account=account,
                event_type=event_type,
                payload=payload_item,
                symbol=payload_item.get("symbol"),
            )
        )

    previous_pipeline = index_rows(
        snapshot_items(previous_snapshot, "pipeline_events"), "id"
    )
    current_pipeline = index_rows(
        snapshot_items(current_snapshot, "pipeline_events"), "id"
    )
    new_pipeline_ids = [
        key for key in current_pipeline.keys() if key not in previous_pipeline
    ]
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
            next_stream_envelope(
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

    previous_audits = index_rows(
        list(previous_snapshot.get("recent_command_audits") or []), "operation_id"
    )
    current_audits = index_rows(
        list(current_snapshot.get("recent_command_audits") or []), "operation_id"
    )
    new_audit_ids = [key for key in current_audits.keys() if key not in previous_audits]
    new_audit_ids.sort(
        key=lambda item: str((current_audits.get(item) or {}).get("recorded_at") or "")
    )
    for audit_id in new_audit_ids:
        audit = current_audits[audit_id]
        events.append(
            next_stream_envelope(
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
    symbol = normalize_optional_string(symbol)
    account = normalize_optional_string(account)
    include_snapshot = normalize_bool(include_snapshot, default=True)
    include_alerts = normalize_bool(include_alerts, default=True)
    include_positions = normalize_bool(include_positions, default=True)
    include_orders = normalize_bool(include_orders, default=True)
    include_pending = normalize_bool(include_pending, default=True)
    heartbeat_seconds = normalize_int(heartbeat_seconds, default=15)
    active_account = stream_account_alias(service)
    if account and account != active_account:
        raise HTTPException(
            status_code=404, detail=f"unknown trading account: {account}"
        )

    async def event_generator():  # type: ignore[no-untyped-def]
        counters = {"sequence": 0, "state_version": 0}
        last_heartbeat = time.monotonic()
        previous_snapshot = await asyncio.to_thread(
            build_trade_state_stream_snapshot,
            runtime_views,
            service,
            symbol=symbol,
            include_alerts=include_alerts,
            include_positions=include_positions,
            include_orders=include_orders,
            include_pending=include_pending,
        )

        last_event_id = request.headers.get("last-event-id") or request.headers.get(
            "Last-Event-ID"
        )
        if last_event_id:
            yield format_trade_state_sse(
                next_stream_envelope(
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
            yield format_trade_state_sse(
                next_stream_envelope(
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
                        "pending_entries": previous_snapshot.get("pending_entries")
                        or [],
                        "unmanaged_live_positions": (
                            previous_snapshot.get("unmanaged_live_positions") or {}
                        ),
                        "pipeline_events": previous_snapshot.get("pipeline_events")
                        or {},
                        "alerts": previous_snapshot.get("alerts") or [],
                    },
                    symbol=symbol,
                )
            )

        while True:
            await asyncio.sleep(_TRADE_STATE_STREAM_POLL_SECONDS)
            current_snapshot = await asyncio.to_thread(
                build_trade_state_stream_snapshot,
                runtime_views,
                service,
                symbol=symbol,
                include_alerts=include_alerts,
                include_positions=include_positions,
                include_orders=include_orders,
                include_pending=include_pending,
            )
            events: list[dict[str, Any]] = []
            append_change_events(
                events,
                counters,
                previous_snapshot=previous_snapshot,
                current_snapshot=current_snapshot,
            )
            if events:
                previous_snapshot = current_snapshot
                for event in events:
                    yield format_trade_state_sse(event)
                last_heartbeat = time.monotonic()
                continue

            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_seconds:
                yield format_trade_state_sse(
                    next_stream_envelope(
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
