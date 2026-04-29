"""Admin routes for EntryPolicy registry & decisions (ADR-013).

- ``GET /v1/entry-policies/registry``    — list registered policies + mappings
- ``GET /v1/entry-policies/decisions``    — query entry_policy_decisions table
- ``GET /v1/entry-policies/groups/active``— list currently active OCO groups

All endpoints are read-only. Mapping changes go through entry_policy.ini
hot-reload (planned in P5).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api import deps
from src.api.schemas import ApiResponse

router = APIRouter(prefix="/v1/entry-policies", tags=["entry-policies"])


@router.get("/registry", response_model=ApiResponse[Dict[str, Any]])
def get_registry(
    registry: Optional[Any] = Depends(deps.get_entry_policy_registry),
) -> ApiResponse[Dict[str, Any]]:
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail="EntryPolicyRegistry not assembled (signal phase not initialized)",
        )
    return ApiResponse.success_response(registry.describe())


@router.get("/decisions", response_model=ApiResponse[List[Dict[str, Any]]])
def get_decisions(
    storage: Optional[Any] = Depends(deps.get_storage_writer),
    strategy: Optional[str] = Query(None),
    timeframe: Optional[str] = Query(None),
    policy_name: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> ApiResponse[List[Dict[str, Any]]]:
    if storage is None or storage.db is None:
        raise HTTPException(status_code=503, detail="storage unavailable")

    repo = storage.db.entry_policy_repo
    rows = repo.fetch_decisions(
        strategy=strategy,
        timeframe=timeframe,
        policy_name=policy_name,
        since=since,
        limit=limit,
    )
    return ApiResponse.success_response(rows)


@router.get("/groups/active", response_model=ApiResponse[List[Dict[str, Any]]])
def get_active_groups(
    storage: Optional[Any] = Depends(deps.get_storage_writer),
    account_key: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> ApiResponse[List[Dict[str, Any]]]:
    """列当前活跃 OCO group（按 group_id 聚合 status='placed' 挂单）。"""
    if storage is None or storage.db is None:
        raise HTTPException(status_code=503, detail="storage unavailable")

    repo = storage.db.trading_state_repo
    rows = repo.fetch_pending_order_states(
        account_key=account_key,
        statuses=["placed"],
        limit=limit * 4,  # 留余量按 group_id 聚合
    )

    # 按 group_id 聚合
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        gid = row.get("order_group_id")
        if not gid:
            continue
        bucket = groups.setdefault(
            gid,
            {
                "group_id": gid,
                "account_key": row.get("account_key"),
                "symbol": row.get("symbol"),
                "strategy": row.get("strategy"),
                "timeframe": row.get("timeframe"),
                "members": [],
            },
        )
        bucket["members"].append(
            {
                "order_ticket": row.get("order_ticket"),
                "member_id": row.get("group_member_id"),
                "group_role": row.get("group_role"),
                "trigger_price": row.get("trigger_price"),
                "entry_low": row.get("entry_low"),
                "entry_high": row.get("entry_high"),
                "status": row.get("status"),
                "expires_at": row.get("expires_at"),
            }
        )

    aggregated = list(groups.values())[:limit]
    return ApiResponse.success_response(aggregated)
