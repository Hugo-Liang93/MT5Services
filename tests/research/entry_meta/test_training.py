from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.research.entry_meta.dataset import EntryMetaDatasetBuilder
from src.research.entry_meta.features import EntryMetaFeatureMatrix
from src.research.entry_meta.scoring import EntryMetaScorer
from src.research.entry_meta.training import train_entry_meta_bundle
from src.research.core.backends import BackendUnavailableError


@dataclass
class _UnavailableBackend:
    name: str = "gpu"

    def assert_available(self) -> None:
        raise BackendUnavailableError("test backend unavailable")


def _matrix(n_bars: int = 8):
    bar_times = [
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=5 * index)
        for index in range(n_bars)
    ]

    class _Matrix:
        n_bars = len(bar_times)
        indicator_series = {
            ("ema", "value"): [float(index) for index in range(n_bars)],
            ("rsi", "value"): [40.0 + float(index) for index in range(n_bars)],
        }
        regimes = ["range", "trend"] * (n_bars // 2)
        sessions = ["asia", "london"] * (n_bars // 2)

        def __init__(self) -> None:
            self.bar_times = list(bar_times)

        def train_slice(self) -> range:
            return range(0, n_bars // 2)

        def test_slice(self) -> range:
            return range(n_bars // 2, n_bars)

    return _Matrix()


def _trade(bar_time: datetime, pnl: float, index: int) -> dict[str, object]:
    return {
        "entry_time": bar_time,
        "confidence": 0.50 + index * 0.01,
        "direction": "buy" if index % 2 == 0 else "sell",
        "entry_price": 1.10 + index * 0.001,
        "strategy": "breakout" if index % 2 == 0 else "mean_reversion",
        "pnl": pnl,
    }


def _dataset(matrix, pnls: list[float]):
    trades = [_trade(matrix.bar_times[index], pnl, index) for index, pnl in enumerate(pnls)]
    return EntryMetaDatasetBuilder().build(matrix, trades)


def test_cpu_training_outputs_artifact_probabilities_aligned_to_take_and_block() -> None:
    matrix = _matrix()
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0, 9.0, -4.0, 6.0, -2.0])

    bundle = train_entry_meta_bundle(matrix, dataset, "cpu", model_id="entry-meta-test")

    assert bundle.artifact.model_id == "entry-meta-test"
    assert bundle.artifact.backend == "cpu"
    assert bundle.artifact.model_kind == "tabular"
    assert bundle.artifact.status == "refit"
    assert bundle.artifact.metrics["fit_status"] == "trained"
    assert bundle.artifact.metrics["quality"] == {
        "status": "refit",
        "reason": "insufficient_samples",
    }
    assert bundle.features.train_indices == [0, 1, 2, 3]
    assert bundle.dataset is dataset
    assert len(bundle.artifact.predictions) == len(dataset.trades)
    for prediction in bundle.artifact.predictions:
        assert prediction.take_entry_prob == pytest.approx(1.0 - prediction.block_entry_prob)
        assert 0.0 <= prediction.take_entry_prob <= 1.0
        assert 0.0 <= prediction.block_entry_prob <= 1.0
    assert bundle.artifact.metrics["oos_samples"] == 4
    assert "oos_accuracy" in bundle.artifact.metrics
    assert "probability_distribution" in bundle.artifact.metrics
    assert bundle.artifact.model_payload["estimator"] == "logistic_regression_v1"
    assert bundle.artifact.model_payload["feature_order"] == bundle.features.feature_keys
    assert bundle.artifact.model_payload["classes"] == [0, 1]
    assert bundle.artifact.model_payload["prediction_reuse"] == "dynamic_scorer"


def test_cpu_training_accepts_artifact_when_quality_gate_thresholds_are_met() -> None:
    matrix = _matrix()
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0, 9.0, -4.0, 6.0, -2.0])

    bundle = train_entry_meta_bundle(
        matrix,
        dataset,
        "cpu",
        min_samples=8,
        min_oos_samples=4,
        min_class_samples=4,
    )

    assert bundle.artifact.status == "accepted"
    assert bundle.artifact.metrics["fit_status"] == "trained"
    assert bundle.artifact.metrics["quality"] == {
        "status": "accepted",
        "reason": "accepted",
    }


def test_training_dynamic_scorer_reproduces_saved_predictions() -> None:
    matrix = _matrix()
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0, 9.0, -4.0, 6.0, -2.0])

    bundle = train_entry_meta_bundle(matrix, dataset, "cpu")
    scorer = EntryMetaScorer.from_payload(
        bundle.artifact.model_payload,
        feature_keys=bundle.artifact.feature_keys,
    )

    for row, prediction in zip(bundle.features.rows, bundle.artifact.predictions, strict=True):
        score = scorer.score(row)

        assert score.score_source == "dynamic_scorer"
        assert prediction.take_entry_prob == pytest.approx(score.take_entry_prob)
        assert prediction.block_entry_prob == pytest.approx(score.block_entry_prob)


def test_training_uses_constant_refit_when_train_slice_has_one_class() -> None:
    matrix = _matrix()
    dataset = _dataset(matrix, [10.0, 8.0, 7.0, 3.0, -9.0, -4.0, 6.0, -2.0])

    bundle = train_entry_meta_bundle(matrix, dataset, "cpu")

    assert bundle.artifact.status == "refit"
    assert bundle.artifact.metrics["fit_status"] == "refit"
    assert bundle.artifact.metrics["quality"]["reason"] == "insufficient_samples"
    assert bundle.artifact.model_payload["estimator"] == "constant_prior"
    assert bundle.artifact.model_payload["prediction_reuse"] == "constant_prior"
    assert bundle.artifact.model_payload["class_probs"] == {
        "block_entry": 0.0,
        "take_entry": 1.0,
    }
    assert {prediction.take_entry_prob for prediction in bundle.artifact.predictions} == {1.0}
    assert {prediction.block_entry_prob for prediction in bundle.artifact.predictions} == {0.0}


def test_training_uses_constant_refit_when_train_slice_is_empty() -> None:
    matrix = _matrix()
    matrix.train_slice = lambda: range(0, 0)
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0, 9.0, -4.0, 6.0, -2.0])

    bundle = train_entry_meta_bundle(matrix, dataset, "cpu")

    assert bundle.artifact.status == "refit"
    assert bundle.artifact.model_payload["reason"] == "insufficient_train_classes_or_features"
    assert bundle.artifact.model_payload["class_probs"] == {
        "block_entry": 0.5,
        "take_entry": 0.5,
    }
    assert all(
        prediction.take_entry_prob == pytest.approx(0.5)
        and prediction.block_entry_prob == pytest.approx(0.5)
        for prediction in bundle.artifact.predictions
    )


def test_gpu_backend_unavailable_fails_fast(monkeypatch) -> None:
    from src.research.entry_meta import training

    monkeypatch.setattr(training, "resolve_backend", lambda name: _UnavailableBackend())
    matrix = _matrix()
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0, 9.0, -4.0, 6.0, -2.0])

    with pytest.raises(BackendUnavailableError, match="test backend unavailable"):
        train_entry_meta_bundle(matrix, dataset, "gpu")


def test_training_rejects_dataset_label_and_weight_length_mismatch() -> None:
    matrix = _matrix()
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0])
    mismatched = replace(
        dataset,
        labels=replace(
            dataset.labels,
            labels=list(dataset.labels.labels[:-1]),
            sample_weights=list(dataset.labels.sample_weights[:-1]),
        ),
    )

    with pytest.raises(ValueError, match="labels.*sample_weights"):
        train_entry_meta_bundle(matrix, mismatched, "cpu")


def test_training_rejects_feature_row_and_key_contract_mismatch(monkeypatch) -> None:
    from src.research.entry_meta import training

    matrix = _matrix()
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0])

    class _BadFeatureBuilder:
        def build(self, matrix, dataset):  # noqa: ANN001
            return EntryMetaFeatureMatrix(
                rows=np.asarray([[1.0], [2.0], [3.0], [4.0]], dtype=float),
                feature_keys=["one", "two"],
                bar_times=["t0", "t1", "t2", "t3"],
                train_indices=[0],
                test_indices=[1],
                manifest={},
            )

    monkeypatch.setattr(training, "EntryMetaFeatureBuilder", _BadFeatureBuilder)

    with pytest.raises(ValueError, match="rows.*feature_keys"):
        train_entry_meta_bundle(matrix, dataset, "cpu")


def test_training_rejects_feature_index_out_of_range(monkeypatch) -> None:
    from src.research.entry_meta import training

    matrix = _matrix()
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0])

    class _BadFeatureBuilder:
        def build(self, matrix, dataset):  # noqa: ANN001
            return EntryMetaFeatureMatrix(
                rows=np.asarray([[1.0], [2.0], [3.0], [4.0]], dtype=float),
                feature_keys=["one"],
                bar_times=["t0", "t1", "t2", "t3"],
                train_indices=[0, 4],
                test_indices=[1],
                manifest={},
            )

    monkeypatch.setattr(training, "EntryMetaFeatureBuilder", _BadFeatureBuilder)

    with pytest.raises(ValueError, match="train_indices.*\\[0, 4\\)"):
        train_entry_meta_bundle(matrix, dataset, "cpu")


def test_training_rejects_invalid_probability_output(monkeypatch) -> None:
    from src.research.entry_meta import training

    matrix = _matrix()
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0])

    monkeypatch.setattr(
        training,
        "_fit_predict_probabilities",
        lambda **kwargs: (np.asarray([[0.5, 0.5]], dtype=float), {}, "trained"),
    )

    with pytest.raises(ValueError, match="probabilities.*shape"):
        train_entry_meta_bundle(matrix, dataset, "cpu")


@pytest.mark.parametrize(
    ("probabilities", "message"),
    [
        (np.asarray([[0.5, 0.5], [np.nan, 0.5], [0.5, 0.5], [0.5, 0.5]]), "finite"),
        (np.asarray([[0.5, 0.5], [0.0, 0.0], [0.5, 0.5], [0.5, 0.5]]), "row sums"),
        (np.asarray([[0.5, 0.5], [-1.0, 2.0], [0.5, 0.5], [0.5, 0.5]]), "range"),
        (np.asarray([[0.5, 0.5], [0.7, 0.7], [0.5, 0.5], [0.5, 0.5]]), "row sums"),
    ],
)
def test_training_rejects_non_finite_or_zero_sum_probabilities(
    monkeypatch,
    probabilities: np.ndarray,
    message: str,
) -> None:
    from src.research.entry_meta import training

    matrix = _matrix()
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0])

    monkeypatch.setattr(
        training,
        "_fit_predict_probabilities",
        lambda **kwargs: (probabilities, {}, "trained"),
    )

    with pytest.raises(ValueError, match=f"probabilities.*{message}"):
        train_entry_meta_bundle(matrix, dataset, "cpu")
