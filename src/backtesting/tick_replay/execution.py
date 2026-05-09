from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from src.clients.mt5_market import Tick


@dataclass(frozen=True)
class TickReplayOrder:
    side: str
    kind: str = "market"
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@dataclass(frozen=True)
class TickReplayFill:
    side: str
    reason: str
    fill_price: float
    fill_time: datetime
    fill_time_msc: Optional[int]
    tick: Tick


class BidAskTickExecutionModel:
    """Bid/ask-aware execution model for tick replay."""

    def fill_entry(
        self,
        order: TickReplayOrder,
        ticks: Iterable[Tick],
        *,
        signal_index: int,
    ) -> Optional[TickReplayFill]:
        side = self._normalize_side(order.side)
        kind = self._normalize_kind(order.kind)
        candidates = self._ticks_after_index(ticks, signal_index)
        if kind == "market":
            for tick in candidates:
                price = self._entry_price(side, tick)
                if price is not None:
                    return self._fill(side, "market", price, tick)
            return None
        if kind == "limit":
            if order.limit_price is None:
                raise ValueError("limit order requires limit_price")
            for tick in candidates:
                price = self._entry_price(side, tick)
                if price is None:
                    continue
                if side == "buy" and price <= order.limit_price:
                    return self._fill(side, "limit", price, tick)
                if side == "sell" and price >= order.limit_price:
                    return self._fill(side, "limit", price, tick)
            return None
        raise ValueError(f"unsupported tick replay order kind: {kind}")

    def resolve_exit(
        self,
        *,
        side: str,
        ticks: Iterable[Tick],
        signal_index: int,
        stop_loss: Optional[float],
        take_profit: Optional[float],
    ) -> Optional[TickReplayFill]:
        normalized_side = self._normalize_side(side)
        candidates = self._ticks_after_index(ticks, signal_index)
        for _time_msc, group in self._group_by_time_msc(candidates).items():
            stop_fill: Optional[TickReplayFill] = None
            take_profit_fill: Optional[TickReplayFill] = None
            for tick in group:
                exit_price = self._exit_price(normalized_side, tick)
                if exit_price is None:
                    continue
                if stop_loss is not None and self._touches_stop_loss(
                    normalized_side, exit_price, stop_loss
                ):
                    stop_fill = stop_fill or self._fill(
                        normalized_side,
                        "stop_loss",
                        exit_price,
                        tick,
                    )
                if take_profit is not None and self._touches_take_profit(
                    normalized_side, exit_price, take_profit
                ):
                    take_profit_fill = take_profit_fill or self._fill(
                        normalized_side,
                        "take_profit",
                        exit_price,
                        tick,
                    )
            if stop_fill is not None:
                return stop_fill
            if take_profit_fill is not None:
                return take_profit_fill
        return None

    @staticmethod
    def _ticks_after_index(ticks: Iterable[Tick], signal_index: int) -> list[Tick]:
        ordered = sorted(
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
        return ordered[signal_index + 1 :]

    @staticmethod
    def _group_by_time_msc(ticks: Iterable[Tick]) -> "OrderedDict[int, list[Tick]]":
        groups: "OrderedDict[int, list[Tick]]" = OrderedDict()
        for tick in ticks:
            time_msc = (
                int(tick.time_msc)
                if tick.time_msc is not None
                else int(tick.time.timestamp() * 1000)
            )
            groups.setdefault(time_msc, []).append(tick)
        return groups

    @staticmethod
    def _normalize_side(side: str) -> str:
        normalized = str(side).strip().lower()
        if normalized not in {"buy", "sell"}:
            raise ValueError(f"unsupported tick replay side: {side}")
        return normalized

    @staticmethod
    def _normalize_kind(kind: str) -> str:
        return str(kind).strip().lower()

    @staticmethod
    def _entry_price(side: str, tick: Tick) -> Optional[float]:
        return tick.ask if side == "buy" else tick.bid

    @staticmethod
    def _exit_price(side: str, tick: Tick) -> Optional[float]:
        return tick.bid if side == "buy" else tick.ask

    @staticmethod
    def _touches_stop_loss(side: str, exit_price: float, stop_loss: float) -> bool:
        if side == "buy":
            return exit_price <= stop_loss
        return exit_price >= stop_loss

    @staticmethod
    def _touches_take_profit(side: str, exit_price: float, take_profit: float) -> bool:
        if side == "buy":
            return exit_price >= take_profit
        return exit_price <= take_profit

    @staticmethod
    def _fill(side: str, reason: str, price: float, tick: Tick) -> TickReplayFill:
        return TickReplayFill(
            side=side,
            reason=reason,
            fill_price=price,
            fill_time=tick.time,
            fill_time_msc=tick.time_msc,
            tick=tick,
        )
