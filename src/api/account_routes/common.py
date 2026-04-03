from __future__ import annotations

from src.api.schemas import OrderModel, PositionModel


def position_model_from_dataclass(position) -> PositionModel:
    payload = dict(position.__dict__)
    payload["time"] = position.time.isoformat()
    return PositionModel(**payload)


def order_model_from_dataclass(order) -> OrderModel:
    payload = dict(order.__dict__)
    payload["time"] = order.time.isoformat()
    return OrderModel(**payload)
