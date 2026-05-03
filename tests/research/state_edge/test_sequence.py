from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.research.core.barrier import BarrierOutcome
from src.research.core.data_matrix import DataMatrix
from src.research.state_edge.features import StateEdgeFeatureBuilder
from src.research.state_edge.labels import StateEdgeLabelBuilder
from src.research.state_edge.neighbors import ShapeNeighborIndex
from src.research.state_edge.sequence import SequenceWindowBuilder
from src.research.state_edge.sequence_training import train_sequence_tcn_artifact
from src.research.state_edge.sequence_training import train_sequence_mlp_artifact


def _outcomes(values: list[float]) -> list[BarrierOutcome]:
    return [BarrierOutcome("time", float(value), 3) for value in values]


def _matrix(n: int = 96) -> DataMatrix:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    times = [start + timedelta(hours=i) for i in range(n)]
    opens = [100.0 + np.sin(i / 5) for i in range(n)]
    closes = [opens[i] + (0.4 if i % 4 in (0, 1) else -0.35) for i in range(n)]
    highs = [max(opens[i], closes[i]) + 0.2 for i in range(n)]
    lows = [min(opens[i], closes[i]) - 0.2 for i in range(n)]
    indicator_series = {
        ("rsi14", "rsi"): [45.0 + np.sin(i / 3) * 20.0 for i in range(n)],
        ("adx14", "adx"): [18.0 + (i % 10) for i in range(n)],
        ("future_probe", "value"): [999.0] * n,
    }
    long_ret = [0.01 if i % 4 in (0, 1) else -0.004 for i in range(n)]
    short_ret = [0.01 if i % 4 in (2, 3) else -0.004 for i in range(n)]
    return DataMatrix(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=n,
        bar_times=times,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        volumes=[1.0] * n,
        indicators=[{} for _ in range(n)],
        regimes=["trending" if i % 2 == 0 else "ranging" for i in range(n)],
        soft_regimes=[None] * n,
        forward_returns={},
        indicator_series=indicator_series,
        train_end_idx=int(n * 0.7),
        split_idx=int(n * 0.7),
        sessions=["london"] * n,
        barrier_returns_long={(1.0, 1.5, 20): _outcomes(long_ret)},
        barrier_returns_short={(1.0, 1.5, 20): _outcomes(short_ret)},
    )


def test_sequence_window_builder_adds_candle_shape_without_future_fields() -> None:
    matrix = _matrix(8)
    features = StateEdgeFeatureBuilder().build(matrix)
    labels = StateEdgeLabelBuilder().build(matrix)

    sequence = SequenceWindowBuilder(window=4).build(matrix, features, labels)

    assert sequence.windows.shape == (5, 4, len(sequence.feature_keys))
    assert sequence.target_indices == [3, 4, 5, 6, 7]
    assert "candle.body_ratio" in sequence.feature_keys
    assert "candle.upper_wick_ratio" in sequence.feature_keys
    assert "indicator.rsi14.rsi" in sequence.feature_keys
    assert not any("future" in key.lower() for key in sequence.feature_keys)
    first_target = sequence.target_indices[0]
    assert sequence.windows[0, -1, sequence.feature_keys.index("candle.close_location")] == pytest.approx(
        (matrix.closes[first_target] - matrix.lows[first_target])
        / (matrix.highs[first_target] - matrix.lows[first_target])
    )


def test_sequence_tcn_training_outputs_state_edge_artifact() -> None:
    pytest.importorskip("torch")
    matrix = _matrix(96)

    artifact = train_sequence_tcn_artifact(
        matrix,
        backend_name="cpu",
        model_id="sequence-tcn-smoke",
        sequence_window=12,
        epochs=1,
        batch_size=16,
    )

    assert artifact.model_id == "sequence-tcn-smoke"
    assert artifact.backend == "cpu"
    assert artifact.model_payload["kind"] == "sequence_tcn"
    assert artifact.model_payload["sequence_window"] == 12
    assert len(artifact.predictions) == matrix.n_bars
    assert artifact.metrics["oos_samples"] > 0
    assert "shape_neighbors" in artifact.metrics
    for prediction in artifact.predictions:
        total = (
            prediction.long_edge_prob
            + prediction.short_edge_prob
            + prediction.no_trade_prob
        )
        assert total == pytest.approx(1.0)


def test_sequence_mlp_training_outputs_state_edge_artifact() -> None:
    pytest.importorskip("torch")
    matrix = _matrix(96)

    artifact = train_sequence_mlp_artifact(
        matrix,
        backend_name="cpu",
        model_id="sequence-mlp-smoke",
        sequence_window=12,
        epochs=1,
        batch_size=16,
    )

    assert artifact.model_id == "sequence-mlp-smoke"
    assert artifact.model_payload["kind"] == "sequence_mlp"
    assert artifact.model_payload["sequence_window"] == 12
    assert len(artifact.predictions) == matrix.n_bars
    assert artifact.metrics["shape_neighbors"]["available"] is True


def test_shape_neighbors_return_past_analogs_only() -> None:
    matrix = _matrix(16)
    features = StateEdgeFeatureBuilder().build(matrix)
    labels = StateEdgeLabelBuilder().build(matrix)
    sequence = SequenceWindowBuilder(window=4).build(matrix, features, labels)
    index = ShapeNeighborIndex.build(sequence, labels)

    result = index.query(target_index=12, top_k=3)

    assert result.target_index == 12
    assert len(result.neighbors) == 3
    assert all(item.target_index < 12 for item in result.neighbors)
    assert sum(result.label_counts.values()) == 3
    assert result.mean_best_return != 0.0
