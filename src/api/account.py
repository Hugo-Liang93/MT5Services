from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import get_account_service
from src.api.schemas import AccountInfoModel, ApiResponse, OrderModel, PositionModel
from src.core.account_service import AccountService

router = APIRouter(tags=["account"])


@router.get("/account/info", response_model=ApiResponse[AccountInfoModel])
def account_info(svc: AccountService = Depends(get_account_service)) -> ApiResponse[AccountInfoModel]:
    info = svc.account_info()
    return ApiResponse(success=True, data=AccountInfoModel(**info.__dict__))


@router.get("/account/positions", response_model=ApiResponse[List[PositionModel]])
def account_positions(
    symbol: Optional[str] = Query(default=None, description="过滤品种，可为空表示全部"),
    svc: AccountService = Depends(get_account_service),
) -> ApiResponse[List[PositionModel]]:
    try:
        positions = svc.positions(symbol)
        items = [PositionModel(**p.__dict__, time=p.time.isoformat()) for p in positions]
        return ApiResponse(success=True, data=items)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/account/orders", response_model=ApiResponse[List[OrderModel]])
def account_orders(
    symbol: Optional[str] = Query(default=None, description="过滤品种，可为空表示全部"),
    svc: AccountService = Depends(get_account_service),
) -> ApiResponse[List[OrderModel]]:
    try:
        orders = svc.orders(symbol)
        items = [OrderModel(**o.__dict__, time=o.time.isoformat()) for o in orders]
        return ApiResponse(success=True, data=items)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
