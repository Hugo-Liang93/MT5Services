from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4


@dataclass(frozen=True)
class TradeOperationRecord:
    account_alias: str
    operation_type: str
    status: str
    symbol: Optional[str] = None
    side: Optional[str] = None
    order_kind: Optional[str] = None
    volume: Optional[float] = None
    ticket: Optional[int] = None
    order_id: Optional[int] = None
    deal_id: Optional[int] = None
    magic: Optional[int] = None
    duration_ms: Optional[int] = None
    error_message: Optional[str] = None
    request_payload: Dict[str, Any] = field(default_factory=dict)
    response_payload: Dict[str, Any] = field(default_factory=dict)
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    operation_id: str = field(default_factory=lambda: uuid4().hex)

    def to_row(self) -> tuple:
        return (
            self.recorded_at,
            self.operation_id,
            self.account_alias,
            self.operation_type,
            self.status,
            self.symbol,
            self.side,
            self.order_kind,
            self.volume,
            self.ticket,
            self.order_id,
            self.deal_id,
            self.magic,
            self.duration_ms,
            self.error_message,
            dict(self.request_payload),
            dict(self.response_payload),
        )
