from __future__ import annotations

from datetime import datetime, timezone

from src.research.core.barrier import BarrierOutcome
from src.research.core.data_matrix import DataMatrix
from src.research.state_edge.labels import StateEdgeClass, StateEdgeLabelBuilder


def _matrix(
    long_returns: list[float | None],
    short_returns: list[float | None],
    *,
    second_long: list[float | None] | None = None,
    second_short: list[float | None] | None = None,
) -> DataMatrix:
    n = len(long_returns)
    times = [datetime(2026, 1, 1, i, tzinfo=timezone.utc) for i in range(n)]

    def outcomes(values: list[float | None]) -> list[BarrierOutcome | None]:
        return [
            None if value is None else BarrierOutcome("time", float(value), 3)
            for value in values
        ]

    long = {(1.0, 1.5, 20): outcomes(long_returns)}
    short = {(1.0, 1.5, 20): outcomes(short_returns)}
    if second_long is not None:
        long[(1.5, 2.5, 40)] = outcomes(second_long)
    if second_short is not None:
        short[(1.5, 2.5, 40)] = outcomes(second_short)
    return DataMatrix(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=n,
        bar_times=times,
        opens=[1.0] * n,
        highs=[1.0] * n,
        lows=[1.0] * n,
        closes=[1.0] * n,
        volumes=[1.0] * n,
        indicators=[{} for _ in range(n)],
        regimes=["unknown"] * n,
        soft_regimes=[None] * n,
        forward_returns={},
        indicator_series={},
        train_end_idx=max(1, n // 2),
        split_idx=max(1, n // 2),
        barrier_returns_long=long,
        barrier_returns_short=short,
    )


def test_labels_choose_best_positive_direction_and_no_trade() -> None:
    matrix = _matrix(
        long_returns=[0.012, -0.004, 0.004, -0.001],
        short_returns=[0.002, 0.009, 0.004, -0.002],
    )

    labels = StateEdgeLabelBuilder().build(matrix)

    assert labels.labels == [
        StateEdgeClass.LONG,
        StateEdgeClass.SHORT,
        StateEdgeClass.LONG,
        StateEdgeClass.NO_TRADE,
    ]
    assert labels.long_best_return == [0.012, -0.004, 0.004, -0.001]
    assert labels.short_best_return == [0.002, 0.009, 0.004, -0.002]
    assert labels.summary["long"] == 2
    assert labels.summary["short"] == 1
    assert labels.summary["no_trade"] == 1


def test_labels_scan_all_barrier_configs_and_ignore_missing_outcomes() -> None:
    matrix = _matrix(
        long_returns=[None, 0.001, None],
        short_returns=[None, 0.004, None],
        second_long=[0.006, 0.003, None],
        second_short=[None, 0.002, None],
    )

    labels = StateEdgeLabelBuilder().build(matrix)

    assert labels.labels == [
        StateEdgeClass.LONG,
        StateEdgeClass.SHORT,
        StateEdgeClass.NO_TRADE,
    ]
    assert labels.best_long_barrier[0] == (1.5, 2.5, 40)
    assert labels.best_short_barrier[1] == (1.0, 1.5, 20)
    assert labels.best_long_barrier[2] is None
    assert labels.best_short_barrier[2] is None
