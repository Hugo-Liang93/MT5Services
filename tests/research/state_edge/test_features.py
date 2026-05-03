from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from src.research.core.data_matrix import DataMatrix
from src.research.state_edge.features import StateEdgeFeatureBuilder


def _matrix() -> DataMatrix:
    times = [datetime(2026, 1, 1, h, tzinfo=timezone.utc) for h in range(3)]
    return DataMatrix(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=3,
        bar_times=times,
        opens=[1.0, 2.0, 3.0],
        highs=[1.0, 2.0, 3.0],
        lows=[1.0, 2.0, 3.0],
        closes=[1.0, 2.0, 3.0],
        volumes=[10.0, 11.0, 12.0],
        indicators=[{} for _ in range(3)],
        regimes=["trending", "ranging", "trending"],
        soft_regimes=[
            {"trending": 0.8, "ranging": 0.2},
            {"trending": 0.4, "ranging": 0.6},
            {"trending": 0.7, "ranging": 0.3},
        ],
        forward_returns={3: [0.01, -0.01, 0.0]},
        indicator_series={
            ("rsi14", "rsi"): [31.0, 52.0, 68.0],
            ("regime_transition", "regime_entropy"): [0.2, 0.8, 0.4],
            ("future_probe", "value"): [9.0, 9.0, 9.0],
            ("barrier_returns_long", "tp_20"): [1.0, 1.0, 1.0],
            ("forward_return", "h3"): [0.1, 0.2, 0.3],
            ("label", "target"): [1.0, 2.0, 0.0],
        },
        train_end_idx=2,
        split_idx=2,
        sessions=["asia", "london", "new_york"],
    )


def test_feature_manifest_excludes_future_and_barrier_fields() -> None:
    features = StateEdgeFeatureBuilder().build(_matrix())

    assert "indicator.rsi14.rsi" in features.feature_keys
    assert "indicator.regime_transition.regime_entropy" in features.feature_keys
    assert "regime.hard_code" in features.feature_keys
    assert "soft_regime.trending" in features.feature_keys
    assert "session.asia" in features.feature_keys
    assert not any(
        token in key.lower()
        for key in features.feature_keys
        for token in ("forward", "future", "barrier", "outcome", "label")
    )
    assert features.rows.shape == (3, len(features.feature_keys))
    assert np.isfinite(features.rows).all()
    assert features.train_indices == [0, 1]
    assert features.test_indices == [2]
