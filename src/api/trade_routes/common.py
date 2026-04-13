from __future__ import annotations

from typing import Optional

from src.api.action_contracts import (
    build_action_error_details as build_control_action_error_details,
    build_action_error_payload as build_control_action_error_payload,
    build_action_result as build_control_action_result,
    build_idempotency_conflict_response,
    build_replayed_action_response,
    next_action_id,
    normalize_action_actor as normalize_control_actor,
    normalize_idempotency_key,
    normalize_request_context,
)
from src.api.error_codes import get_trade_error_details
from src.api.schemas import ApiResponse, OrderModel, PositionModel, TradeRequest


def trade_request_details(request: TradeRequest) -> dict:
    return get_trade_error_details(
        symbol=request.symbol,
        volume=request.volume,
        side=request.side,
        price=request.price,
    )


def position_model_from_dataclass(position) -> PositionModel:
    payload = dict(position.__dict__)
    payload["time"] = position.time.isoformat()
    return PositionModel(**payload)


def order_model_from_dataclass(order) -> OrderModel:
    payload = dict(order.__dict__)
    payload["time"] = order.time.isoformat()
    return OrderModel(**payload)


def normalize_optional_status(status: Optional[str]) -> Optional[str]:
    if not isinstance(status, str):
        return None
    normalized = status.strip()
    return normalized or None
