from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PendingOrderStateRecord:
    account_alias: str
    order_ticket: int
    signal_id: Optional[str]
    request_id: Optional[str]
    symbol: str
    direction: str
    strategy: str = ""
    timeframe: str = ""
    category: str = ""
    order_kind: str = ""
    comment: str = ""
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    trigger_price: Optional[float] = None
    entry_price_requested: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    volume: Optional[float] = None
    atr_at_entry: Optional[float] = None
    confidence: Optional[float] = None
    regime: Optional[str] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    position_ticket: Optional[int] = None
    deal_id: Optional[int] = None
    fill_price: Optional[float] = None
    status: str = "placed"
    status_reason: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=_utcnow)

    def to_row(self) -> tuple:
        return (
            self.account_alias,
            self.order_ticket,
            self.signal_id,
            self.request_id,
            self.symbol,
            self.direction,
            self.strategy,
            self.timeframe,
            self.category,
            self.order_kind,
            self.comment,
            self.entry_low,
            self.entry_high,
            self.trigger_price,
            self.entry_price_requested,
            self.stop_loss,
            self.take_profit,
            self.volume,
            self.atr_at_entry,
            self.confidence,
            self.regime,
            self.created_at,
            self.expires_at,
            self.filled_at,
            self.cancelled_at,
            self.position_ticket,
            self.deal_id,
            self.fill_price,
            self.status,
            self.status_reason,
            self.last_seen_at,
            dict(self.metadata),
            self.updated_at,
        )


@dataclass(frozen=True)
class PositionRuntimeStateRecord:
    account_alias: str
    position_ticket: int
    signal_id: Optional[str]
    order_ticket: Optional[int]
    symbol: str
    direction: str
    timeframe: str = ""
    strategy: str = ""
    comment: str = ""
    entry_price: Optional[float] = None
    initial_stop_loss: Optional[float] = None
    initial_take_profit: Optional[float] = None
    current_stop_loss: Optional[float] = None
    current_take_profit: Optional[float] = None
    volume: Optional[float] = None
    atr_at_entry: Optional[float] = None
    confidence: Optional[float] = None
    regime: Optional[str] = None
    opened_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    last_managed_at: Optional[datetime] = None
    highest_price: Optional[float] = None
    lowest_price: Optional[float] = None
    current_price: Optional[float] = None
    breakeven_applied: bool = False
    trailing_active: bool = False
    status: str = "open"
    closed_at: Optional[datetime] = None
    close_source: Optional[str] = None
    close_price: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=_utcnow)

    def to_row(self) -> tuple:
        return (
            self.account_alias,
            self.position_ticket,
            self.signal_id,
            self.order_ticket,
            self.symbol,
            self.direction,
            self.timeframe,
            self.strategy,
            self.comment,
            self.entry_price,
            self.initial_stop_loss,
            self.initial_take_profit,
            self.current_stop_loss,
            self.current_take_profit,
            self.volume,
            self.atr_at_entry,
            self.confidence,
            self.regime,
            self.opened_at,
            self.last_seen_at,
            self.last_managed_at,
            self.highest_price,
            self.lowest_price,
            self.current_price,
            self.breakeven_applied,
            self.trailing_active,
            self.status,
            self.closed_at,
            self.close_source,
            self.close_price,
            dict(self.metadata),
            self.updated_at,
        )


@dataclass(frozen=True)
class TradeControlStateRecord:
    account_alias: str
    auto_entry_enabled: bool
    close_only_mode: bool
    updated_at: Optional[datetime]
    reason: Optional[str]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> tuple:
        return (
            self.account_alias,
            self.auto_entry_enabled,
            self.close_only_mode,
            self.updated_at,
            self.reason,
            dict(self.metadata),
        )
