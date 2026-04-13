from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4


@dataclass(frozen=True)
class TradeExecutionDetails:
    ticket: int
    symbol: str
    volume: float
    requested_price: Optional[float]
    fill_price: float
    order_id: int = 0
    deal_id: int = 0
    retcode: Optional[int] = None
    broker_comment: Optional[str] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    deviation: int = 20
    magic: int = 0
    pending: bool = False
    recovered_from_state: bool = False
    state_source: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ticket": int(self.ticket),
            "order": int(self.order_id),
            "deal": int(self.deal_id),
            "retcode": int(self.retcode) if self.retcode is not None else None,
            "broker_comment": self.broker_comment,
            "symbol": self.symbol,
            "volume": float(self.volume),
            "price": self.requested_price,
            "requested_price": self.requested_price,
            "fill_price": float(self.fill_price),
            "sl": self.sl,
            "tp": self.tp,
            "deviation": int(self.deviation),
            "magic": int(self.magic),
            "pending": bool(self.pending),
        }
        if self.recovered_from_state:
            payload["recovered_from_state"] = True
        if self.state_source:
            payload["state_source"] = self.state_source
        return payload


@dataclass(frozen=True)
class TradeCommandAuditRecord:
    account_alias: str
    command_type: str
    status: str
    account_key: str = ""
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
            self.command_type,
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
            self.account_key,
        )
