from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from src.clients.mt5_market import Tick
from src.market.tick_features import (
    TickFeatureCalculator,
    TickFeatureConfig,
    TickFeatureSnapshot,
)
from src.signals.contracts.capability import StrategyCapability, normalize_signal_scopes


@dataclass(frozen=True)
class TickReplayReport:
    symbol: str
    tick_count: int
    valid_tick_count: int
    feature_snapshot_count: int
    tick_coverage: float
    feature_coverage: float
    average_spread_points: Optional[float]
    max_spread_points: Optional[float]
    rejected_signal_count: int
    snapshots: tuple[TickFeatureSnapshot, ...]


class TickReplayRunner:
    """Replay ticks into live-equivalent tick feature snapshots."""

    def __init__(
        self,
        *,
        calculator: Optional[TickFeatureCalculator] = None,
        config: Optional[TickFeatureConfig] = None,
    ) -> None:
        self._calculator = calculator or TickFeatureCalculator(config)

    @property
    def calculator(self) -> TickFeatureCalculator:
        return self._calculator

    def build_feature_snapshots(
        self,
        symbol: str,
        ticks: Iterable[Tick],
    ) -> list[TickFeatureSnapshot]:
        ordered = self._ordered_ticks(ticks)
        snapshots: list[TickFeatureSnapshot] = []
        window_msc = int(self._calculator.config.window_seconds * 1000)
        for index, current in enumerate(ordered):
            current_msc = self._tick_order_msc(current)
            window_start = current_msc - window_msc
            window_ticks = [
                tick
                for tick in ordered[: index + 1]
                if self._tick_order_msc(tick) >= window_start
            ]
            snapshots.append(
                self._calculator.calculate(
                    symbol,
                    window_ticks,
                    now=current.time,
                )
            )
        return snapshots

    def run(
        self,
        *,
        symbol: str,
        ticks: Iterable[Tick],
        strategy: Any = None,
    ) -> TickReplayReport:
        ordered = self._ordered_ticks(ticks)
        snapshots = self.build_feature_snapshots(symbol, ordered)
        valid_tick_count = sum(
            1 for tick in ordered if self._is_valid_replay_tick(tick)
        )
        spreads = [
            float(snapshot.spread_points)
            for snapshot in snapshots
            if snapshot.spread_points is not None
        ]
        feature_windows_with_data = sum(
            1 for snapshot in snapshots if snapshot.spread_points is not None
        )
        rejected = 0
        if strategy is not None and not self._strategy_supports_tick_derived(strategy):
            rejected = len(snapshots)
        return TickReplayReport(
            symbol=symbol,
            tick_count=len(ordered),
            valid_tick_count=valid_tick_count,
            feature_snapshot_count=len(snapshots),
            tick_coverage=self._ratio(valid_tick_count, len(ordered)),
            feature_coverage=self._ratio(feature_windows_with_data, len(ordered)),
            average_spread_points=(
                round(sum(spreads) / len(spreads), 10) if spreads else None
            ),
            max_spread_points=max(spreads) if spreads else None,
            rejected_signal_count=rejected,
            snapshots=tuple(snapshots),
        )

    def _strategy_supports_tick_derived(self, strategy: Any) -> bool:
        capability = getattr(strategy, "capability", None)
        if isinstance(capability, StrategyCapability):
            return "tick_derived" in capability.valid_scopes
        if isinstance(capability, dict):
            return (
                "tick_derived"
                in StrategyCapability.from_contract(capability).valid_scopes
            )
        capability_fn = getattr(strategy, "capability_contract", None)
        if callable(capability_fn):
            return (
                "tick_derived"
                in StrategyCapability.from_contract(capability_fn()).valid_scopes
            )
        preferred_scopes = getattr(strategy, "preferred_scopes", None)
        if preferred_scopes is not None:
            return "tick_derived" in normalize_signal_scopes(preferred_scopes)
        return False

    @staticmethod
    def _ordered_ticks(ticks: Iterable[Tick]) -> list[Tick]:
        return sorted(
            list(ticks),
            key=lambda tick: (
                TickReplayRunner._tick_order_msc(tick),
                tick.time,
            ),
        )

    @staticmethod
    def _tick_order_msc(tick: Tick) -> int:
        if tick.time_msc is not None:
            return int(tick.time_msc)
        return int(tick.time.timestamp() * 1000)

    @staticmethod
    def _is_valid_replay_tick(tick: Tick) -> bool:
        return (
            tick.time_msc is not None and tick.bid is not None and tick.ask is not None
        )

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return round(numerator / denominator, 10)
