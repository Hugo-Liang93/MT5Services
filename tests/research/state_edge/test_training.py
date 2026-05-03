from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.research.core.barrier import BarrierOutcome
from src.research.core.data_matrix import DataMatrix
from src.research.state_edge.lab import train_state_edge_artifact


def _outcomes(values: list[float | None]) -> list[BarrierOutcome | None]:
    return [
        None if value is None else BarrierOutcome("time", float(value), 3)
        for value in values
    ]


def test_train_predict_artifact_smoke() -> None:
    n = 9
    times = [datetime(2026, 1, 1, i, tzinfo=timezone.utc) for i in range(n)]
    matrix = DataMatrix(
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
        regimes=["trending", "ranging", "unknown"] * 3,
        soft_regimes=[None] * n,
        forward_returns={},
        indicator_series={
            ("rsi14", "rsi"): [30.0, 70.0, 50.0, 32.0, 72.0, 51.0, 34.0, 74.0, 52.0],
            ("adx14", "adx"): [25.0, 28.0, 10.0, 26.0, 29.0, 11.0, 27.0, 30.0, 12.0],
        },
        train_end_idx=6,
        split_idx=6,
        sessions=["london"] * n,
        barrier_returns_long={
            (1.0, 1.5, 20): _outcomes(
                [0.010, -0.006, -0.001, 0.011, -0.004, -0.002, 0.012, -0.003, -0.001]
            )
        },
        barrier_returns_short={
            (1.0, 1.5, 20): _outcomes(
                [-0.004, 0.012, -0.002, -0.003, 0.010, -0.001, -0.005, 0.013, -0.002]
            )
        },
    )

    artifact = train_state_edge_artifact(
        matrix,
        backend_name="cpu",
        model_id="state-edge-H1-smoke",
    )

    assert artifact.model_id == "state-edge-H1-smoke"
    assert artifact.backend == "cpu"
    assert artifact.label_summary == {"long": 3, "short": 3, "no_trade": 3}
    assert len(artifact.predictions) == n
    assert artifact.metrics["oos_samples"] == 3
    assert "top_probability_buckets" in artifact.metrics
    for prediction in artifact.predictions:
        total = (
            prediction.long_edge_prob
            + prediction.short_edge_prob
            + prediction.no_trade_prob
        )
        assert total == pytest.approx(1.0)
