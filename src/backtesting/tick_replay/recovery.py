from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from src.clients.mt5_market import Tick
from src.trading.recovery.controller import BoundedRecoveryController
from src.trading.recovery.models import (
    RecoveryCycleState,
    RecoveryDecision,
    RecoveryMarketSnapshot,
    RecoveryPolicy,
)


@dataclass(frozen=True)
class RecoveryReplayFill:
    role: str
    step_index: int
    side: str
    volume: float
    fill_price: float
    fill_time: datetime
    fill_time_msc: int | None
    tick: Tick


@dataclass(frozen=True)
class RecoveryReplayReport:
    symbol: str
    direction: str
    tick_count: int
    tradable_tick_count: int
    tick_coverage: float
    fills: tuple[RecoveryReplayFill, ...]
    events: tuple[RecoveryDecision, ...]
    closed: bool
    close_reason: str | None
    close_price: float | None
    estimated_pnl: float
    blocked_count: int
    hold_count: int
    max_step_count: int
    max_total_volume: float


class TickRecoveryReplayRunner:
    """Replay bounded recovery cycles on bid/ask ticks.

    This runner is deliberately offline-only. It validates path-dependent
    recovery cadence and exposure caps before any live/demo execution wiring.
    """

    def __init__(self, controller: BoundedRecoveryController | None = None) -> None:
        self._controller = controller or BoundedRecoveryController()

    def run(
        self,
        *,
        symbol: str,
        direction: str,
        ticks: Iterable[Tick],
        policy: RecoveryPolicy,
        account_key: str,
        cycle_id: str,
    ) -> RecoveryReplayReport:
        ordered = self._ordered_ticks(ticks)
        tradable_tick_count = sum(
            1 for tick in ordered if tick.bid is not None and tick.ask is not None
        )
        events: list[RecoveryDecision] = []
        fills: list[RecoveryReplayFill] = []
        blocked_count = 0
        hold_count = 0
        if not ordered:
            return self._report(
                symbol=symbol,
                direction=direction,
                tick_count=0,
                tradable_tick_count=0,
                fills=fills,
                events=events,
                closed=False,
                close_reason=None,
                close_price=None,
                estimated_pnl=0.0,
                blocked_count=0,
                hold_count=0,
                max_step_count=0,
                max_total_volume=0.0,
            )

        first = ordered[0]
        first_snapshot = self._snapshot(symbol, first)
        initial_price = (
            first_snapshot.entry_price(direction)
            if first_snapshot.has_bid_ask()
            else None
        )
        if initial_price is None:
            events.append(RecoveryDecision(action="block", reason="quote_side_missing"))
            return self._report(
                symbol=symbol,
                direction=direction,
                tick_count=len(ordered),
                tradable_tick_count=tradable_tick_count,
                fills=fills,
                events=events,
                closed=False,
                close_reason=None,
                close_price=None,
                estimated_pnl=0.0,
                blocked_count=1,
                hold_count=0,
                max_step_count=0,
                max_total_volume=0.0,
            )

        cycle = RecoveryCycleState(
            cycle_id=cycle_id,
            account_key=account_key,
            symbol=symbol,
            direction=str(direction).strip().lower(),
            status="open",
            base_volume=policy.base_volume,
            total_volume=policy.base_volume,
            step_count=0,
            average_entry_price=initial_price,
            last_entry_price=initial_price,
            started_at=first.time,
            updated_at=first.time,
            last_step_at=first.time,
        )
        fills.append(
            RecoveryReplayFill(
                role="initial",
                step_index=0,
                side=cycle.direction,
                volume=policy.base_volume,
                fill_price=initial_price,
                fill_time=first.time,
                fill_time_msc=first.time_msc,
                tick=first,
            )
        )
        max_total_volume = cycle.total_volume
        close_reason: str | None = None
        close_price: float | None = None
        estimated_pnl = 0.0
        closed = False

        for tick in ordered[1:]:
            snapshot = self._snapshot(symbol, tick)
            exit_decision = self._controller.evaluate_cycle_exit(
                policy, cycle, snapshot
            )
            if exit_decision.action == "block":
                events.append(exit_decision)
                blocked_count += 1
                continue
            if exit_decision.action == "close_cycle":
                events.append(exit_decision)
                assert exit_decision.exit_price is not None
                close_price = exit_decision.exit_price
                close_reason = exit_decision.reason
                estimated_pnl = self._controller.estimate_pnl(
                    policy,
                    cycle,
                    exit_price=close_price,
                )
                closed = True
                break

            decision = self._controller.evaluate_next_step(
                policy, cycle, snapshot, now=tick.time
            )
            events.append(decision)
            if decision.action == "block":
                blocked_count += 1
                continue
            if decision.action == "hold":
                hold_count += 1
                continue

            assert decision.volume is not None
            assert decision.entry_price is not None
            assert decision.step_index is not None
            fills.append(
                RecoveryReplayFill(
                    role="recovery",
                    step_index=decision.step_index,
                    side=cycle.direction,
                    volume=decision.volume,
                    fill_price=decision.entry_price,
                    fill_time=tick.time,
                    fill_time_msc=tick.time_msc,
                    tick=tick,
                )
            )
            cycle = self._controller.apply_open_step(cycle, decision, now=tick.time)
            max_total_volume = max(max_total_volume, cycle.total_volume)

        return self._report(
            symbol=symbol,
            direction=cycle.direction,
            tick_count=len(ordered),
            tradable_tick_count=tradable_tick_count,
            fills=fills,
            events=events,
            closed=closed,
            close_reason=close_reason,
            close_price=close_price,
            estimated_pnl=estimated_pnl,
            blocked_count=blocked_count,
            hold_count=hold_count,
            max_step_count=cycle.step_count,
            max_total_volume=max_total_volume,
        )

    @staticmethod
    def _ordered_ticks(ticks: Iterable[Tick]) -> list[Tick]:
        return sorted(
            list(ticks),
            key=lambda tick: (
                (
                    int(tick.time_msc)
                    if tick.time_msc is not None
                    else int(tick.time.timestamp() * 1000)
                ),
                tick.time,
            ),
        )

    @staticmethod
    def _snapshot(symbol: str, tick: Tick) -> RecoveryMarketSnapshot:
        return RecoveryMarketSnapshot(
            symbol=symbol,
            bid=tick.bid,
            ask=tick.ask,
            time=tick.time,
            time_msc=tick.time_msc,
        )

    @staticmethod
    def _report(
        *,
        symbol: str,
        direction: str,
        tick_count: int,
        tradable_tick_count: int,
        fills: list[RecoveryReplayFill],
        events: list[RecoveryDecision],
        closed: bool,
        close_reason: str | None,
        close_price: float | None,
        estimated_pnl: float,
        blocked_count: int,
        hold_count: int,
        max_step_count: int,
        max_total_volume: float,
    ) -> RecoveryReplayReport:
        return RecoveryReplayReport(
            symbol=symbol,
            direction=direction,
            tick_count=tick_count,
            tradable_tick_count=tradable_tick_count,
            tick_coverage=(
                round(tradable_tick_count / tick_count, 8) if tick_count > 0 else 0.0
            ),
            fills=tuple(fills),
            events=tuple(events),
            closed=closed,
            close_reason=close_reason,
            close_price=close_price,
            estimated_pnl=estimated_pnl,
            blocked_count=blocked_count,
            hold_count=hold_count,
            max_step_count=max_step_count,
            max_total_volume=round(max_total_volume, 8),
        )
