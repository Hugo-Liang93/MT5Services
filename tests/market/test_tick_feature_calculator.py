from __future__ import annotations

from datetime import datetime, timezone

from src.clients.mt5_market import Tick
from src.market.tick_features.calculator import TickFeatureCalculator
from src.market.tick_features.models import TickFeatureConfig


def _tick(time_msc: int, *, bid: float, ask: float, last: float) -> Tick:
    return Tick(
        symbol="EURUSD",
        bid=bid,
        ask=ask,
        last=last,
        volume=1.0,
        time=datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc),
        time_msc=time_msc,
        flags=6,
    )


def test_calculator_builds_healthy_snapshot_from_ordered_ticks() -> None:
    calculator = TickFeatureCalculator(
        TickFeatureConfig(
            window_seconds=5,
            min_ticks_per_window=3,
            point_size=0.0001,
            max_spread_points=3,
        )
    )
    now = datetime.fromtimestamp(10.500, tz=timezone.utc)

    snapshot = calculator.calculate(
        "EURUSD",
        [
            _tick(8_000, bid=1.1000, ask=1.1002, last=1.1001),
            _tick(9_000, bid=1.1002, ask=1.1004, last=1.1003),
            _tick(10_000, bid=1.1001, ask=1.1003, last=1.1002),
        ],
        now,
    )

    assert snapshot.status == "healthy"
    assert snapshot.reasons == ()
    assert snapshot.tick_count == 3
    assert snapshot.window_start_msc == 5_000
    assert snapshot.window_end_msc == 10_000
    assert snapshot.mid == 1.1002
    assert snapshot.spread_points == 2.0
    assert snapshot.quote_age_ms == 500
    assert snapshot.realized_range_points == 2.0
    assert snapshot.price_change_points == 1.0
    assert snapshot.buy_pressure == 0.5
    assert snapshot.sell_pressure == 0.5


def test_calculator_returns_stale_snapshot_for_empty_window() -> None:
    calculator = TickFeatureCalculator(TickFeatureConfig(point_size=0.0001))
    now = datetime.fromtimestamp(10, tz=timezone.utc)

    snapshot = calculator.calculate("EURUSD", [], now)

    assert snapshot.status == "stale"
    assert snapshot.reasons == ("no_ticks",)
    assert snapshot.tick_count == 0
    assert snapshot.window_end_msc == 10_000


def test_calculator_marks_sparse_window() -> None:
    calculator = TickFeatureCalculator(
        TickFeatureConfig(min_ticks_per_window=3, point_size=0.0001)
    )
    now = datetime.fromtimestamp(10, tz=timezone.utc)

    snapshot = calculator.calculate(
        "EURUSD",
        [_tick(10_000, bid=1.1000, ask=1.1002, last=1.1001)],
        now,
    )

    assert snapshot.status == "sparse"
    assert snapshot.reasons == ("sparse_ticks",)


def test_calculator_blocks_wide_spread() -> None:
    calculator = TickFeatureCalculator(
        TickFeatureConfig(
            min_ticks_per_window=1,
            point_size=0.0001,
            max_spread_points=1,
        )
    )
    now = datetime.fromtimestamp(10, tz=timezone.utc)

    snapshot = calculator.calculate(
        "EURUSD",
        [_tick(10_000, bid=1.1000, ask=1.1003, last=1.1001)],
        now,
    )

    assert snapshot.status == "blocked"
    assert snapshot.reasons == ("spread_wide",)


def test_calculator_blocks_when_tradeable_quote_side_is_missing() -> None:
    calculator = TickFeatureCalculator(
        TickFeatureConfig(
            min_ticks_per_window=3,
            point_size=0.0001,
        )
    )
    now = datetime.fromtimestamp(10, tz=timezone.utc)
    ticks = [
        Tick(
            symbol="EURUSD",
            bid=None,
            ask=None,
            last=1.1001 + index * 0.0001,
            volume=1.0,
            time=datetime.fromtimestamp((8_000 + index * 1_000) / 1000, tz=timezone.utc),
            time_msc=8_000 + index * 1_000,
            flags=6,
        )
        for index in range(3)
    ]

    snapshot = calculator.calculate("EURUSD", ticks, now)

    assert snapshot.status == "blocked"
    assert snapshot.reasons == ("quote_side_missing",)
    assert snapshot.spread_points is None


def test_calculator_sorts_out_of_order_input() -> None:
    calculator = TickFeatureCalculator(
        TickFeatureConfig(min_ticks_per_window=2, point_size=0.0001)
    )
    now = datetime.fromtimestamp(10, tz=timezone.utc)

    snapshot = calculator.calculate(
        "EURUSD",
        [
            _tick(10_000, bid=1.1002, ask=1.1004, last=1.1003),
            _tick(9_000, bid=1.1000, ask=1.1002, last=1.1001),
        ],
        now,
    )

    assert snapshot.window_end_msc == 10_000
    assert snapshot.price_change_points == 2.0
    assert snapshot.buy_pressure == 1.0
    assert snapshot.sell_pressure == 0.0
