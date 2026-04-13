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
    account_key: str = ""
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
            self.account_key,
        )


@dataclass(frozen=True)
class PositionRuntimeStateRecord:
    account_alias: str
    position_ticket: int
    signal_id: Optional[str]
    order_ticket: Optional[int]
    symbol: str
    direction: str
    account_key: str = ""
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
            self.account_key,
        )


@dataclass(frozen=True)
class TradeControlStateRecord:
    account_alias: str
    auto_entry_enabled: bool
    close_only_mode: bool
    updated_at: Optional[datetime]
    reason: Optional[str]
    account_key: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> tuple:
        return (
            self.account_alias,
            self.auto_entry_enabled,
            self.close_only_mode,
            self.updated_at,
            self.reason,
            dict(self.metadata),
            self.account_key,
        )


@dataclass(frozen=True)
class AccountRiskStateRecord:
    account_key: str
    account_alias: str
    instance_id: str
    instance_role: str
    runtime_mode: str | None
    auto_entry_enabled: bool
    close_only_mode: bool
    circuit_open: bool
    consecutive_failures: int
    last_risk_block: Optional[str]
    margin_level: Optional[float]
    margin_guard_state: Optional[str]
    should_block_new_trades: bool
    should_tighten_stops: bool
    should_emergency_close: bool
    open_positions_count: int
    pending_orders_count: int
    quote_stale: bool
    indicator_degraded: bool
    db_degraded: bool
    active_risk_flags: list[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=_utcnow)

    def to_row(self) -> tuple:
        return (
            self.account_key,
            self.account_alias,
            self.instance_id,
            self.instance_role,
            self.runtime_mode,
            self.auto_entry_enabled,
            self.close_only_mode,
            self.circuit_open,
            self.consecutive_failures,
            self.last_risk_block,
            self.margin_level,
            self.margin_guard_state,
            self.should_block_new_trades,
            self.should_tighten_stops,
            self.should_emergency_close,
            self.open_positions_count,
            self.pending_orders_count,
            self.quote_stale,
            self.indicator_degraded,
            self.db_degraded,
            list(self.active_risk_flags),
            dict(self.metadata),
            self.updated_at,
        )
