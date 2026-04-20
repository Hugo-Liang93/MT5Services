from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_runtime_read_model, get_trading_query_service
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.readmodels.runtime import RuntimeReadModel
from src.trading.application.services import TradingQueryService

from ..view_models import TradeCommandAuditView
from .common import (
    iso_or_none,
    normalize_int,
    normalize_optional_datetime,
    normalize_optional_string,
)

router = APIRouter(tags=["trade"])


@router.get("/trade/command-audits", response_model=ApiResponse[list[TradeCommandAuditView]])
def trade_command_audits(
    command_type: Optional[str] = Query(default=None, description="command type"),
    status: Optional[str] = Query(default=None, description="operation status"),
    symbol: Optional[str] = Query(default=None, description="symbol filter"),
    signal_id: Optional[str] = Query(default=None, description="signal id filter"),
    trace_id: Optional[str] = Query(default=None, description="trace id filter"),
    actor: Optional[str] = Query(default=None, description="actor filter"),
    audit_id: Optional[str] = Query(default=None, description="audit/operation id (=trade_command_audits.operation_id)"),
    action_id: Optional[str] = Query(default=None, description="action id (operator_commands.action_id)"),
    idempotency_key: Optional[str] = Query(default=None, description="idempotency key for mutation receipts"),
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
) -> ApiResponse[list[dict]]:
    command_type = normalize_optional_string(command_type)
    status = normalize_optional_string(status)
    symbol = normalize_optional_string(symbol)
    signal_id = normalize_optional_string(signal_id)
    trace_id = normalize_optional_string(trace_id)
    actor = normalize_optional_string(actor)
    audit_id = normalize_optional_string(audit_id)
    action_id = normalize_optional_string(action_id)
    idempotency_key = normalize_optional_string(idempotency_key)
    page = normalize_int(page, default=1)
    sort = normalize_optional_string(sort) or "recorded_at_desc"
    effective_page_size = normalize_int(
        page_size,
        default=normalize_int(limit, default=100),
    )
    normalized_from_time = normalize_optional_datetime(from_time)
    normalized_to_time = normalize_optional_datetime(to_time)
    result = service.command_audit_page(
        command_type=command_type,
        status=status,
        symbol=symbol,
        signal_id=signal_id,
        trace_id=trace_id,
        actor=actor,
        audit_id=audit_id,
        action_id=action_id,
        idempotency_key=idempotency_key,
        from_time=normalized_from_time,
        to_time=normalized_to_time,
        page=page,
        page_size=effective_page_size,
        sort=sort,
    )
    items = list(result.get("items") or [])
    total = int(result.get("total") or 0)
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
            "audit_id": audit_id,
            "action_id": action_id,
            "idempotency_key": idempotency_key,
            "from": iso_or_none(normalized_from_time),
            "to": iso_or_none(normalized_to_time),
            "page": page,
            "page_size": effective_page_size,
            "total": total,
            "sort": sort,
            "count": len(items),
        },
    )


@router.get(
    "/trade/command-audits/{audit_id}",
    response_model=ApiResponse[TradeCommandAuditView],
    summary="单条 mutation 回执详情（含 linked operator_command）",
    description=(
        "按 `audit_id`（= `trade_command_audits.operation_id`）查询单条审计记录。"
        "额外返回 `linked_operator_command` —— 通过 `operator_commands.audit_id` 反查队列"
        "侧的 `action_id / actor / reason / idempotency_key / attempt_count / last_error_code`，"
        "闭环 mutation 回执链路。"
    ),
)
def trade_command_audit_detail(
    audit_id: str,
    service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[dict]:
    normalized = normalize_optional_string(audit_id) or ""
    if not normalized:
        return ApiResponse.error_response(
            error_code=AIErrorCode.VALIDATION_ERROR,
            error_message="audit_id required",
            suggested_action=AIErrorAction.VALIDATE_PARAMETERS,
        )
    record = service.command_audit_detail(audit_id=normalized)
    if record is None:
        return ApiResponse.error_response(
            error_code=AIErrorCode.NOT_FOUND,
            error_message=f"audit_id not found: {normalized}",
            suggested_action=AIErrorAction.VALIDATE_PARAMETERS,
        )
    return ApiResponse.success_response(
        data=record,
        metadata={
            "operation": "trade_command_audit_detail",
            "account_alias": service.active_account_alias,
            "audit_id": normalized,
            "has_linked_command": record.get("linked_operator_command") is not None,
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
        for row in rows:
            val = row.get("recorded_at")
            if val is not None and hasattr(val, "isoformat"):
                row["recorded_at"] = val.isoformat()
        return ApiResponse.success_response(
            data=rows,
            metadata={"position_ticket": ticket, "count": len(rows)},
        )
    except Exception as exc:
        return ApiResponse.error_response(str(exc), error_code="INTERNAL_ERROR")
