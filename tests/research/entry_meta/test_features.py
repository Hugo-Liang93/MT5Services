from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace

import numpy as np
import pytest

from src.research.entry_meta.dataset import EntryMetaDataset
from src.research.entry_meta.features import (
    EntryMetaFeatureBuilder,
    EntryMetaFeatureBuildError,
    EntryMetaFeatureContext,
    EntryMetaFeatureRowBuilder,
)
from src.research.entry_meta.labels import EntryMetaLabelBuilder


class _Regime(Enum):
    TREND = "trend"
    RANGE = "range"


def _dataset(
    trades: list[dict[str, object]], bar_indices: list[int]
) -> EntryMetaDataset:
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


def _context() -> EntryMetaFeatureContext:
    return EntryMetaFeatureContext(
        bar_time=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
        bar_index=1,
        strategy="breakout",
        direction="buy",
        confidence=0.75,
        entry_price=1.2345,
        indicators={"ema": {"value": 2.0}, "rsi": {"value": 50.0}},
        regime="trend",
        session="london",
    )


def test_feature_row_builder_matches_training_feature_order() -> None:
    builder = EntryMetaFeatureRowBuilder(
        feature_keys=[
            "entry.confidence",
            "entry.direction.buy",
            "entry.direction.sell",
            "entry.price",
            "entry.strategy_code",
            "indicator.ema.value",
            "indicator.rsi.value",
            "matrix.regime_code",
            "matrix.session_code",
        ],
        category_mappings={
            "strategy": {"breakout": 0.0, "mean_reversion": 1.0},
            "regime": {"range": 0.0, "trend": 1.0},
            "session": {"asia": 0.0, "london": 1.0},
        },
    )

    row = builder.build(_context())

    assert row.bar_time == "2026-01-01T00:05:00+00:00"
    assert row.strategy == "breakout"
    assert row.direction == "buy"
    assert row.values.shape == (9,)
    np.testing.assert_allclose(
        row.values,
        np.asarray([0.75, 1.0, 0.0, 1.2345, 0.0, 2.0, 50.0, 1.0, 1.0]),
    )


def test_feature_row_builder_rejects_unknown_category_without_implicit_code() -> None:
    builder = EntryMetaFeatureRowBuilder(
        feature_keys=["entry.strategy_code"],
        category_mappings={
            "strategy": {"breakout": 0.0},
            "regime": {"trend": 0.0},
            "session": {"london": 0.0},
        },
    )
    context = EntryMetaFeatureContext(
        bar_time="2026-01-01T00:05:00Z",
        bar_index=1,
        strategy="unknown",
        direction="buy",
        confidence=0.75,
        entry_price=1.2345,
        indicators={},
        regime="trend",
        session="london",
    )

    with pytest.raises(EntryMetaFeatureBuildError, match="unknown strategy category"):
        builder.build(context)


def test_feature_row_builder_rejects_unknown_strategy_with_feature_subset() -> None:
    builder = EntryMetaFeatureRowBuilder(
        feature_keys=["entry.confidence"],
        category_mappings={
            "strategy": {"breakout": 0.0},
            "regime": {"trend": 0.0},
            "session": {"london": 0.0},
        },
    )
    context = EntryMetaFeatureContext(
        bar_time="2026-01-01T00:05:00Z",
        bar_index=1,
        strategy="unknown",
        direction="buy",
        confidence=0.75,
        entry_price=1.2345,
        indicators={},
        regime="trend",
        session="london",
    )

    with pytest.raises(EntryMetaFeatureBuildError, match="unknown strategy category"):
        builder.build(context)


def test_feature_row_builder_rejects_unknown_regime_with_feature_subset() -> None:
    builder = EntryMetaFeatureRowBuilder(
        feature_keys=["entry.confidence"],
        category_mappings={
            "strategy": {"breakout": 0.0},
            "regime": {"trend": 0.0},
            "session": {"london": 0.0},
        },
    )
    context = EntryMetaFeatureContext(
        bar_time="2026-01-01T00:05:00Z",
        bar_index=1,
        strategy="breakout",
        direction="buy",
        confidence=0.75,
        entry_price=1.2345,
        indicators={},
        regime="range",
        session="london",
    )

    with pytest.raises(EntryMetaFeatureBuildError, match="unknown regime category"):
        builder.build(context)


def test_feature_row_builder_rejects_unknown_session_with_feature_subset() -> None:
    builder = EntryMetaFeatureRowBuilder(
        feature_keys=["entry.confidence"],
        category_mappings={
            "strategy": {"breakout": 0.0},
            "regime": {"trend": 0.0},
            "session": {"london": 0.0},
        },
    )
    context = EntryMetaFeatureContext(
        bar_time="2026-01-01T00:05:00Z",
        bar_index=1,
        strategy="breakout",
        direction="buy",
        confidence=0.75,
        entry_price=1.2345,
        indicators={},
        regime="trend",
        session="asia",
    )

    with pytest.raises(EntryMetaFeatureBuildError, match="unknown session category"):
        builder.build(context)


def test_feature_row_builder_rejects_missing_indicator_field() -> None:
    builder = EntryMetaFeatureRowBuilder(
        feature_keys=["indicator.atr14.atr"],
        category_mappings={
            "strategy": {"breakout": 0.0},
            "regime": {"trend": 0.0},
            "session": {"london": 0.0},
        },
    )

    with pytest.raises(EntryMetaFeatureBuildError, match="missing indicator"):
        builder.build(_context())


def test_feature_row_builder_rejects_non_mapping_indicators_context() -> None:
    builder = EntryMetaFeatureRowBuilder(
        feature_keys=["indicator.atr14.atr"],
        category_mappings={
            "strategy": {"breakout": 0.0},
            "regime": {"trend": 0.0},
            "session": {"london": 0.0},
        },
    )
    context = EntryMetaFeatureContext(
        bar_time="2026-01-01T00:05:00Z",
        bar_index=1,
        strategy="breakout",
        direction="buy",
        confidence=0.75,
        entry_price=1.2345,
        indicators=None,
        regime="trend",
        session="london",
    )

    with pytest.raises(EntryMetaFeatureBuildError, match="indicators"):
        builder.build(context)


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
    assert features.bar_times == [
        "2026-01-01T00:05:00+00:00",
        "2026-01-01T00:10:00+00:00",
    ]
    assert features.train_indices == [0]
    assert features.test_indices == [1]
    assert features.manifest["n_features"] == len(features.feature_keys)
    assert features.manifest["category_mappings"] == {
        "strategy": {"breakout": 0.0, "mean_reversion": 1.0},
        "regime": {"range": 0.0, "trend": 1.0},
        "session": {"asia": 0.0, "london": 1.0},
    }


def test_runtime_safe_feature_scope_keeps_only_allowed_runtime_indicators() -> None:
    matrix = SimpleNamespace(
        bar_times=[datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)],
        indicator_series={
            ("ema", "value"): [1.5],
            ("rsi", "value"): [55.0],
            ("temporal", "rsi_cross_up"): [1.0],
            ("microstructure", "body_ratio"): [0.25],
            ("session_event", "is_london"): [1.0],
            ("trade", "pnl_estimate"): [99.0],
        },
        regimes=["trend"],
        sessions=["london"],
    )
    trade = {
        "entry_time": "2026-01-01T00:00:00Z",
        "confidence": 0.80,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "pnl": 10.0,
    }

    features = EntryMetaFeatureBuilder(
        feature_scope="runtime_safe",
        runtime_indicator_names=["ema", "rsi"],
    ).build(matrix, _dataset([trade], [0]))

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
    assert features.manifest["feature_scope"] == "runtime_safe"
    assert features.manifest["dynamic_scoring_supported"] is True
    assert features.manifest["runtime_indicator_names"] == ["ema", "rsi"]
    assert "indicator.temporal.rsi_cross_up" not in features.feature_keys
    assert "indicator.microstructure.body_ratio" not in features.feature_keys
    assert "indicator.session_event.is_london" not in features.feature_keys
    assert "indicator.trade.pnl_estimate" not in features.feature_keys


def test_research_full_feature_scope_keeps_visible_provider_features() -> None:
    matrix = SimpleNamespace(
        bar_times=[datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)],
        indicator_series={
            ("ema", "value"): [1.5],
            ("temporal", "rsi_cross_up"): [1.0],
            ("trade", "pnl_estimate"): [99.0],
        },
        regimes=["trend"],
        sessions=["london"],
    )
    trade = {
        "entry_time": "2026-01-01T00:00:00Z",
        "confidence": 0.80,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "pnl": 10.0,
    }

    features = EntryMetaFeatureBuilder(feature_scope="research_full").build(
        matrix,
        _dataset([trade], [0]),
    )

    assert "indicator.ema.value" in features.feature_keys
    assert "indicator.temporal.rsi_cross_up" in features.feature_keys
    assert "indicator.trade.pnl_estimate" not in features.feature_keys
    assert features.manifest["feature_scope"] == "research_full"
    assert features.manifest["dynamic_scoring_supported"] is False
    assert features.manifest["runtime_indicator_names"] == []


def test_feature_builder_rejects_unknown_feature_scope() -> None:
    with pytest.raises(ValueError, match="unsupported entry meta feature_scope"):
        EntryMetaFeatureBuilder(feature_scope="unknown")


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
        "confidence": 0.9,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "bad-inputs",
        "pnl": 1.0,
    }

    features = EntryMetaFeatureBuilder().build(matrix, _dataset([trade], [0]))

    assert features.rows.tolist() == [
        [0.9, 1.0, 0.0, 1.2345, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    ]


def test_nested_entry_values_do_not_override_top_level_trade_contract() -> None:
    trade = {
        "entry_time": "2026-01-01T00:05:00Z",
        "confidence": 0.75,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "entry": {
            "confidence": 0.01,
            "direction": "sell",
            "entry_price": 9.9999,
            "strategy": "nested_should_not_apply",
        },
        "pnl": 10.0,
    }

    features = EntryMetaFeatureBuilder().build(_matrix(), _dataset([trade], [1]))

    assert features.rows[0, :5].tolist() == [0.75, 1.0, 0.0, 1.2345, 0.0]


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("confidence", None),
        ("confidence", float("nan")),
        ("confidence", "not-a-number"),
        ("entry_price", None),
        ("entry_price", 0.0),
        ("entry_price", float("inf")),
        ("direction", ""),
        ("direction", "hold"),
        ("strategy", ""),
        ("strategy", None),
    ],
)
def test_raw_trade_core_fields_fail_fast(field: str, bad_value: object) -> None:
    trade = {
        "entry_time": "2026-01-01T00:05:00Z",
        "confidence": 0.75,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "pnl": 10.0,
    }
    trade[field] = bad_value

    with pytest.raises(ValueError, match=rf"sample 0.*{field}"):
        EntryMetaFeatureBuilder().build(_matrix(), _dataset([trade], [1]))


@pytest.mark.parametrize("missing_field", ["indicator_series", "regimes", "sessions"])
def test_matrix_required_fields_fail_fast(missing_field: str) -> None:
    matrix = _matrix()
    delattr(matrix, missing_field)
    trade = {
        "entry_time": "2026-01-01T00:05:00Z",
        "confidence": 0.75,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "pnl": 10.0,
    }

    with pytest.raises(ValueError, match=missing_field):
        EntryMetaFeatureBuilder().build(matrix, _dataset([trade], [1]))


def test_matrix_indicator_series_length_must_match_bar_times() -> None:
    matrix = _matrix()
    matrix.indicator_series[("ema", "value")] = [1.0, 2.0]
    trade = {
        "entry_time": "2026-01-01T00:05:00Z",
        "confidence": 0.75,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "pnl": 10.0,
    }

    with pytest.raises(ValueError, match=r"indicator_series.*ema.*value"):
        EntryMetaFeatureBuilder().build(matrix, _dataset([trade], [1]))


def test_matrix_n_bars_must_match_bar_times_length() -> None:
    matrix = _matrix()
    matrix.n_bars = 99
    trade = {
        "entry_time": "2026-01-01T00:05:00Z",
        "confidence": 0.75,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "pnl": 10.0,
    }

    with pytest.raises(ValueError, match="n_bars"):
        EntryMetaFeatureBuilder().build(matrix, _dataset([trade], [1]))


def test_frozen_category_mappings_are_reused() -> None:
    trade = {
        "entry_time": "2026-01-01T00:05:00Z",
        "confidence": 0.75,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "pnl": 10.0,
    }
    builder = EntryMetaFeatureBuilder(
        category_mappings={
            "strategy": {"breakout": 7.0},
            "regime": {"range": 11.0, "trend": 12.0},
            "session": {"asia": 21.0, "london": 22.0},
        }
    )

    features = builder.build(_matrix(), _dataset([trade], [1]))

    assert features.rows[0, 4] == 7.0
    assert features.rows[0, -2:].tolist() == [12.0, 22.0]
    assert features.manifest["category_mappings"]["strategy"] == {"breakout": 7.0}


def test_frozen_category_mapping_unknown_category_fails_fast() -> None:
    trade = {
        "entry_time": "2026-01-01T00:05:00Z",
        "confidence": 0.75,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "unknown",
        "pnl": 10.0,
    }
    builder = EntryMetaFeatureBuilder(
        category_mappings={
            "strategy": {"breakout": 0.0},
            "regime": {"range": 0.0, "trend": 1.0},
            "session": {"asia": 0.0, "london": 1.0},
        }
    )

    with pytest.raises(ValueError, match=r"sample 0.*strategy.*unknown"):
        builder.build(_matrix(), _dataset([trade], [1]))


def test_dataset_trades_more_than_bar_indices_fails_fast() -> None:
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

    with pytest.raises(ValueError, match=r"trades.*bar_indices"):
        EntryMetaFeatureBuilder().build(_matrix(), _dataset(trades, [1]))


def test_dataset_bar_indices_more_than_trades_fails_fast() -> None:
    trade = {
        "entry_time": "2026-01-01T00:05:00Z",
        "confidence": 0.75,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "pnl": 10.0,
    }

    with pytest.raises(ValueError, match=r"trades.*bar_indices"):
        EntryMetaFeatureBuilder().build(_matrix(), _dataset([trade], [1, 2]))


def test_negative_bar_index_fails_fast() -> None:
    trade = {
        "entry_time": "2026-01-01T00:05:00Z",
        "confidence": 0.75,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "pnl": 10.0,
    }

    with pytest.raises(ValueError, match="bar_index"):
        EntryMetaFeatureBuilder().build(_matrix(), _dataset([trade], [-1]))


def test_out_of_range_bar_index_fails_fast() -> None:
    trade = {
        "entry_time": "2026-01-01T00:05:00Z",
        "confidence": 0.75,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "pnl": 10.0,
    }

    with pytest.raises(ValueError, match="bar_index"):
        EntryMetaFeatureBuilder().build(_matrix(), _dataset([trade], [3]))
