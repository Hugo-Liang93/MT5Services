from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace

import numpy as np

from src.research.entry_meta.dataset import EntryMetaDataset
from src.research.entry_meta.features import EntryMetaFeatureBuilder
from src.research.entry_meta.labels import EntryMetaLabelBuilder


class _Regime(Enum):
    TREND = "trend"
    RANGE = "range"


def _dataset(trades: list[dict[str, object]], bar_indices: list[int]) -> EntryMetaDataset:
    return EntryMetaDataset(
        trades=[dict(trade) for trade in trades],
        bar_indices=bar_indices,
        labels=EntryMetaLabelBuilder().build([dict(trade) for trade in trades]),
        train_indices=[0] if trades else [],
        test_indices=[1] if len(trades) > 1 else [],
        unmatched_trades=[],
    )


def _matrix() -> SimpleNamespace:
    return SimpleNamespace(
        bar_times=[
            datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
        ],
        indicator_series={
            ("ema", "value"): [1.0, 2.0, 3.0],
            ("rsi", "value"): [40.0, 50.0, 60.0],
            ("rsi", "future_value"): [900.0, 901.0, 902.0],
            ("trade", "pnl_estimate"): [7.0, 8.0, 9.0],
        },
        regimes=[_Regime.RANGE, _Regime.TREND, _Regime.RANGE],
        sessions=["asia", "london", "asia"],
    )


def test_builds_entry_context_visible_indicators_and_codes_in_stable_order() -> None:
    trades = [
        {
            "entry_time": "2026-01-01T00:05:00Z",
            "confidence": 0.75,
            "direction": "buy",
            "entry_price": 1.2345,
            "strategy": "breakout",
            "pnl": 10.0,
        },
        {
            "entry_time": "2026-01-01T00:10:00Z",
            "confidence": 0.25,
            "direction": "sell",
            "entry_price": 1.1111,
            "strategy": "mean_reversion",
            "pnl": -5.0,
        },
    ]

    features = EntryMetaFeatureBuilder().build(_matrix(), _dataset(trades, [1, 2]))

    assert features.feature_keys == [
        "entry.confidence",
        "entry.direction.buy",
        "entry.direction.sell",
        "entry.price",
        "entry.strategy_code",
        "indicator.ema.value",
        "indicator.rsi.value",
        "matrix.regime_code",
        "matrix.session_code",
    ]
    np.testing.assert_allclose(
        features.rows,
        np.array(
            [
                [0.75, 1.0, 0.0, 1.2345, 0.0, 2.0, 50.0, 1.0, 1.0],
                [0.25, 0.0, 1.0, 1.1111, 1.0, 3.0, 60.0, 0.0, 0.0],
            ],
            dtype=float,
        ),
    )
    assert features.bar_times == ["2026-01-01T00:05:00+00:00", "2026-01-01T00:10:00+00:00"]
    assert features.train_indices == [0]
    assert features.test_indices == [1]
    assert features.manifest["n_features"] == len(features.feature_keys)


def test_forbidden_indicator_fields_are_excluded_from_feature_keys() -> None:
    features = EntryMetaFeatureBuilder().build(_matrix(), _dataset([], []))

    assert "indicator.rsi.future_value" not in features.feature_keys
    assert "indicator.trade.pnl_estimate" not in features.feature_keys
    assert features.manifest["forbidden_tokens"] == [
        "forward",
        "future",
        "barrier",
        "outcome",
        "label",
        "pnl",
        "exit",
    ]


def test_empty_dataset_returns_zero_row_float_matrix_with_feature_columns() -> None:
    features = EntryMetaFeatureBuilder().build(_matrix(), _dataset([], []))

    assert features.rows.shape == (0, len(features.feature_keys))
    assert features.rows.dtype == float


def test_non_finite_and_non_numeric_visible_values_become_zero() -> None:
    matrix = SimpleNamespace(
        bar_times=[datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)],
        indicator_series={
            ("a", "value"): [float("nan")],
            ("b", "value"): [float("inf")],
            ("c", "value"): ["not-a-number"],
        },
        regimes=[None],
        sessions=[None],
    )
    trade = {
        "entry_time": "2026-01-01T00:00:00Z",
        "confidence": "bad",
        "direction": "buy",
        "entry_price": None,
        "strategy": "bad-inputs",
        "pnl": 1.0,
    }

    features = EntryMetaFeatureBuilder().build(matrix, _dataset([trade], [0]))

    assert features.rows.tolist() == [[0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]]
