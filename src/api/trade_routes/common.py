from __future__ import annotations

from typing import Optional

from src.api.schemas import OrderModel, PositionModel, TradeRequest
from src.api.error_codes import get_trade_error_details


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
