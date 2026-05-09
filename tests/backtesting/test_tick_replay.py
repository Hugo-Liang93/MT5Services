from __future__ import annotations

from datetime import datetime, timezone

from src.backtesting.tick_replay import (
    BidAskTickExecutionModel,
    TickReplayDataLoader,
    TickReplayOrder,
    TickReplayRunner,
)
from src.clients.mt5_market import Tick
from src.market.tick_features import TickFeatureCalculator, TickFeatureConfig
from src.signals.contracts.capability import StrategyCapability


def _dt(msc: int) -> datetime:
    return datetime.fromtimestamp(msc / 1000, tz=timezone.utc)


def _tick(
    time_msc: int,
    *,
    bid: float,
    ask: float,
    last: float,
    volume: float = 1.0,
) -> Tick:
    return Tick(
        symbol="EURUSD",
        bid=bid,
        ask=ask,
        last=last,
        volume=volume,
        time=_dt(time_msc),
        time_msc=time_msc,
        flags=6,
    )


def test_loader_orders_persisted_ticks_by_time_msc() -> None:
    rows = [
        ("EURUSD", 1.1003, 1.1002, 1.1004, 1.1003, 1.0, _dt(3_000), 3_000, 6),
        ("EURUSD", 1.1001, 1.1000, 1.1002, 1.1001, 1.0, _dt(1_000), 1_000, 6),
        ("EURUSD", 1.1002, 1.1001, 1.1003, 1.1002, 1.0, _dt(2_000), 2_000, 6),
    ]

    class Repo:
        def fetch_ticks(self, symbol, start_time, end_time, limit):
            return rows

    ticks = TickReplayDataLoader(Repo()).load_ticks(
        "EURUSD",
        start=_dt(1_000),
        end=_dt(3_000),
        limit=10,
    )

    assert [tick.time_msc for tick in ticks] == [1_000, 2_000, 3_000]


def test_replay_uses_same_tick_feature_calculator_as_live_path() -> None:
    ticks = [
        _tick(1_000, bid=1.1000, ask=1.1002, last=1.1001),
        _tick(2_000, bid=1.1002, ask=1.1004, last=1.1003),
        _tick(3_000, bid=1.1001, ask=1.1003, last=1.1002),
    ]
    config = TickFeatureConfig(window_seconds=5.0, min_ticks_per_window=3)
    calculator = TickFeatureCalculator(config)

    snapshots = TickReplayRunner(calculator=calculator).build_feature_snapshots("EURUSD", ticks)
    live_snapshot = calculator.calculate("EURUSD", ticks, now=ticks[-1].time)

    assert snapshots[-1].spread_points == live_snapshot.spread_points
    assert snapshots[-1].price_change_points == live_snapshot.price_change_points
    assert snapshots[-1].buy_pressure == live_snapshot.buy_pressure
    assert snapshots[-1].sell_pressure == live_snapshot.sell_pressure


def test_market_entry_fills_on_next_tick_side_price() -> None:
    ticks = [
        _tick(1_000, bid=1.1000, ask=1.1002, last=1.1001),
        _tick(2_000, bid=1.1005, ask=1.1008, last=1.1006),
    ]

    fill = BidAskTickExecutionModel().fill_entry(
        TickReplayOrder(side="buy", kind="market"),
        ticks,
        signal_index=0,
    )

    assert fill is not None
    assert fill.fill_time_msc == 2_000
    assert fill.fill_price == 1.1008


def test_same_timestamp_stop_loss_take_profit_conflict_uses_adverse_side() -> None:
    ticks = [
        _tick(1_000, bid=1.1000, ask=1.1002, last=1.1001),
        _tick(2_000, bid=1.0990, ask=1.0992, last=1.0991),
        _tick(2_000, bid=1.1010, ask=1.1012, last=1.1011),
    ]

    fill = BidAskTickExecutionModel().resolve_exit(
        side="buy",
        ticks=ticks,
        signal_index=0,
        stop_loss=1.0990,
        take_profit=1.1010,
    )

    assert fill is not None
    assert fill.reason == "stop_loss"
    assert fill.fill_price == 1.0990


def test_replay_report_exposes_coverage_spread_and_rejection_counts() -> None:
    ticks = [
        _tick(1_000, bid=1.1000, ask=1.1002, last=1.1001),
        _tick(2_000, bid=1.1002, ask=1.1005, last=1.1003),
        _tick(3_000, bid=1.1001, ask=1.1004, last=1.1002),
    ]

    class ConfirmedOnlyStrategy:
        capability = StrategyCapability.from_contract(
            {
                "name": "confirmed_only",
                "valid_scopes": ("confirmed",),
                "needed_indicators": (),
                "needs_intrabar": False,
                "needs_htf": False,
                "regime_affinity": {},
                "htf_requirements": {},
                "market_data_requirements": ("ohlc",),
            }
        )

    report = TickReplayRunner().run(
        symbol="EURUSD",
        ticks=ticks,
        strategy=ConfirmedOnlyStrategy(),
    )

    assert report.tick_coverage == 1.0
    assert report.feature_coverage == 1.0
    assert report.average_spread_points > 0
    assert report.max_spread_points >= report.average_spread_points
    assert report.rejected_signal_count == report.feature_snapshot_count


def test_replay_feature_coverage_is_bounded_when_ticks_are_not_feature_eligible() -> None:
    ticks = [
        Tick(
            symbol="EURUSD",
            bid=1.1000,
            ask=1.1002,
            last=1.1001,
            volume=1.0,
            time=_dt(1_000),
            time_msc=None,
            flags=6,
        ),
        _tick(2_000, bid=1.1002, ask=1.1004, last=1.1003),
    ]

    report = TickReplayRunner().run(symbol="EURUSD", ticks=ticks)

    assert report.tick_coverage == 0.5
    assert report.feature_coverage == 0.5


def test_replay_coverage_excludes_last_only_ticks_without_tradeable_sides() -> None:
    ticks = [
        Tick(
            symbol="EURUSD",
            bid=None,
            ask=None,
            last=1.1001,
            volume=1.0,
            time=_dt(1_000),
            time_msc=1_000,
            flags=6,
        ),
        Tick(
            symbol="EURUSD",
            bid=None,
            ask=None,
            last=1.1002,
            volume=1.0,
            time=_dt(2_000),
            time_msc=2_000,
            flags=6,
        ),
    ]

    report = TickReplayRunner().run(symbol="EURUSD", ticks=ticks)

    assert report.tick_coverage == 0.0
    assert report.feature_coverage == 0.0
    assert report.average_spread_points is None
