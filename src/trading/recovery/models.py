from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RecoveryPolicy:
    """Bounded recovery / martingale policy.

    `max_steps`, `max_next_volume`, and `max_total_volume` are hard caps. The
    controller never emits an escalation decision that violates them.
    """

    enabled: bool
    base_volume: float
    multiplier: float
    max_steps: int
    max_total_volume: float
    step_distance_points: float
    recovery_target_points: float
    point: float
    max_next_volume: float | None = None
    min_step_interval_ms: int = 0
    max_step_adverse_move_points: float = 0.0
    volume_step: float = 0.01
    contract_size: float = 100.0
    direction_mode: str = "fixed"
    min_directional_move_points: float = 0.0
    max_directional_move_points: float = 0.0
    min_pressure_delta: float = 0.0
    max_entry_spread_points: float | None = None
    slippage_budget_points: float = 0.0
    commission_points: float = 0.0
    min_net_profit_points: float = 0.0
    max_cycle_loss_points: float = 0.0
    max_cycle_duration_seconds: float = 0.0
    max_steps_exit_mode: str = "hold"
    max_steps_hold_seconds: float = 0.0

    def __post_init__(self) -> None:
        direction_mode = str(self.direction_mode or "fixed").strip().lower()
        if direction_mode not in {"fixed", "auto"}:
            raise ValueError("direction_mode must be fixed or auto")
        object.__setattr__(self, "direction_mode", direction_mode)
        if self.base_volume <= 0:
            raise ValueError("base_volume must be > 0")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")
        if self.max_steps < 0:
            raise ValueError("max_steps must be >= 0")
        if self.max_total_volume < self.base_volume:
            raise ValueError("max_total_volume must be >= base_volume")
        if self.max_next_volume is not None and self.max_next_volume <= 0:
            raise ValueError("max_next_volume must be None or > 0")
        if self.step_distance_points <= 0:
            raise ValueError("step_distance_points must be > 0")
        if self.recovery_target_points < 0:
            raise ValueError("recovery_target_points must be >= 0")
        if self.point <= 0:
            raise ValueError("point must be > 0")
        if self.min_step_interval_ms < 0:
            raise ValueError("min_step_interval_ms must be >= 0")
        if self.max_step_adverse_move_points < 0:
            raise ValueError("max_step_adverse_move_points must be >= 0")
        if self.volume_step < 0:
            raise ValueError("volume_step must be >= 0")
        if self.contract_size <= 0:
            raise ValueError("contract_size must be > 0")
        if self.min_directional_move_points < 0:
            raise ValueError("min_directional_move_points must be >= 0")
        if self.max_directional_move_points < 0:
            raise ValueError("max_directional_move_points must be >= 0")
        if self.min_pressure_delta < 0:
            raise ValueError("min_pressure_delta must be >= 0")
        if self.max_entry_spread_points is not None and self.max_entry_spread_points <= 0:
            raise ValueError("max_entry_spread_points must be None or > 0")
        if self.slippage_budget_points < 0:
            raise ValueError("slippage_budget_points must be >= 0")
        if self.commission_points < 0:
            raise ValueError("commission_points must be >= 0")
        if self.min_net_profit_points < 0:
            raise ValueError("min_net_profit_points must be >= 0")
        if self.max_cycle_loss_points < 0:
            raise ValueError("max_cycle_loss_points must be >= 0")
        if self.max_cycle_duration_seconds < 0:
            raise ValueError("max_cycle_duration_seconds must be >= 0")
        if self.max_steps_hold_seconds < 0:
            raise ValueError("max_steps_hold_seconds must be >= 0")
        max_steps_exit_mode = str(self.max_steps_exit_mode or "hold").strip().lower()
        if max_steps_exit_mode not in {"hold", "close_cycle"}:
            raise ValueError("max_steps_exit_mode must be hold or close_cycle")
        object.__setattr__(self, "max_steps_exit_mode", max_steps_exit_mode)


@dataclass(frozen=True)
class RecoveryCycleState:
    cycle_id: str
    account_key: str
    symbol: str
    direction: str
    status: str
    base_volume: float
    total_volume: float
    step_count: int
    average_entry_price: float
    last_entry_price: float
    started_at: datetime
    updated_at: datetime
    last_step_at: datetime | None = None
    strategy: str = ""
    timeframe: str = ""
    source_signal_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        direction = str(self.direction).strip().lower()
        if direction not in {"buy", "sell"}:
            raise ValueError("direction must be buy or sell")
        status = str(self.status).strip().lower()
        if status not in {"open", "closed", "blocked"}:
            raise ValueError("status must be open, closed, or blocked")
        if self.base_volume <= 0:
            raise ValueError("base_volume must be > 0")
        if self.total_volume <= 0:
            raise ValueError("total_volume must be > 0")
        if self.step_count < 0:
            raise ValueError("step_count must be >= 0")
        if self.average_entry_price <= 0 or self.last_entry_price <= 0:
            raise ValueError("entry prices must be > 0")


@dataclass(frozen=True)
class RecoveryCycleStateRecord:
    account_alias: str
    account_key: str
    cycle_id: str
    symbol: str
    direction: str
    strategy: str
    timeframe: str
    source_signal_id: str | None
    status: str
    status_reason: str | None
    base_volume: float
    total_volume: float
    step_count: int
    average_entry_price: float
    last_entry_price: float
    started_at: datetime
    last_step_at: datetime | None
    closed_at: datetime | None = None
    close_price: float | None = None
    realized_pnl: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None

    @classmethod
    def from_cycle(
        cls,
        cycle: RecoveryCycleState,
        *,
        account_alias: str,
        account_key: str,
        status_reason: str | None = None,
        closed_at: datetime | None = None,
        close_price: float | None = None,
        realized_pnl: float | None = None,
        updated_at: datetime | None = None,
    ) -> "RecoveryCycleStateRecord":
        return cls(
            account_alias=account_alias,
            account_key=account_key,
            cycle_id=cycle.cycle_id,
            symbol=cycle.symbol,
            direction=cycle.direction,
            strategy=cycle.strategy,
            timeframe=cycle.timeframe,
            source_signal_id=cycle.source_signal_id,
            status=cycle.status,
            status_reason=status_reason,
            base_volume=cycle.base_volume,
            total_volume=cycle.total_volume,
            step_count=cycle.step_count,
            average_entry_price=cycle.average_entry_price,
            last_entry_price=cycle.last_entry_price,
            started_at=cycle.started_at,
            last_step_at=cycle.last_step_at,
            closed_at=closed_at,
            close_price=close_price,
            realized_pnl=realized_pnl,
            metadata=dict(cycle.metadata),
            updated_at=updated_at or cycle.updated_at,
        )

    def to_row(self) -> tuple:
        return (
            self.account_alias,
            self.account_key,
            self.cycle_id,
            self.symbol,
            self.direction,
            self.strategy,
            self.timeframe,
            self.source_signal_id,
            self.status,
            self.status_reason,
            self.base_volume,
            self.total_volume,
            self.step_count,
            self.average_entry_price,
            self.last_entry_price,
            self.started_at,
            self.last_step_at,
            self.closed_at,
            self.close_price,
            self.realized_pnl,
            dict(self.metadata),
            self.updated_at,
        )


@dataclass(frozen=True)
class RecoveryMarketSnapshot:
    symbol: str
    bid: float | None
    ask: float | None
    time: datetime
    time_msc: int | None = None

    def has_bid_ask(self) -> bool:
        return self.bid is not None and self.ask is not None

    def entry_price(self, direction: str) -> float | None:
        normalized = str(direction).strip().lower()
        if normalized == "buy":
            return self.ask
        if normalized == "sell":
            return self.bid
        raise ValueError("direction must be buy or sell")

    def exit_price(self, direction: str) -> float | None:
        normalized = str(direction).strip().lower()
        if normalized == "buy":
            return self.bid
        if normalized == "sell":
            return self.ask
        raise ValueError("direction must be buy or sell")


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    reason: str
    step_index: int | None = None
    volume: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PositionScalingIntent:
    cycle_id: str
    account_key: str
    symbol: str
    direction: str
    strategy: str
    timeframe: str
    step_index: int
    volume: float
    entry_price: float | None
    reason: str
    created_at: datetime
    source_signal_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.cycle_id).strip():
            raise ValueError("cycle_id is required")
        if not str(self.account_key).strip():
            raise ValueError("account_key is required")
        if not str(self.symbol).strip():
            raise ValueError("symbol is required")
        direction = str(self.direction).strip().lower()
        if direction not in {"buy", "sell"}:
            raise ValueError("direction must be buy or sell")
        object.__setattr__(self, "direction", direction)
        if self.step_index <= 0:
            raise ValueError("step_index must be > 0")
        if self.volume <= 0:
            raise ValueError("volume must be > 0")
        if self.entry_price is not None and self.entry_price <= 0:
            raise ValueError("entry_price must be None or > 0")
        if not str(self.reason).strip():
            raise ValueError("reason is required")


@dataclass(frozen=True)
class RecoveryExecutionPlan:
    action: str
    reason: str
    cycle_id: str
    account_key: str
    created_at: datetime
    position_scaling_intent: PositionScalingIntent | None = None
    exit_price: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_decision(
        cls,
        *,
        cycle: RecoveryCycleState,
        decision: RecoveryDecision,
        created_at: datetime,
    ) -> "RecoveryExecutionPlan":
        action = str(decision.action).strip().lower()
        if action not in {"open_step", "close_cycle", "hold", "block"}:
            raise ValueError("unsupported recovery action")
        intent: PositionScalingIntent | None = None
        if action == "open_step":
            if decision.step_index is None:
                raise ValueError("open_step decision requires step_index")
            if decision.volume is None:
                raise ValueError("open_step decision requires volume")
            if decision.entry_price is None:
                raise ValueError("open_step decision requires entry_price")
            intent = PositionScalingIntent(
                cycle_id=cycle.cycle_id,
                account_key=cycle.account_key,
                symbol=cycle.symbol,
                direction=cycle.direction,
                strategy=cycle.strategy,
                timeframe=cycle.timeframe,
                source_signal_id=cycle.source_signal_id,
                step_index=decision.step_index,
                volume=decision.volume,
                entry_price=decision.entry_price,
                reason=decision.reason,
                metadata=dict(decision.metadata),
                created_at=created_at,
            )
        return cls(
            action=action,
            reason=decision.reason,
            cycle_id=cycle.cycle_id,
            account_key=cycle.account_key,
            created_at=created_at,
            position_scaling_intent=intent,
            exit_price=decision.exit_price,
            metadata=dict(decision.metadata),
        )
