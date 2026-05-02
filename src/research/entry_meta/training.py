from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import numpy as np

from src.research.entry_meta.artifacts import EntryMetaArtifact, EntryMetaPrediction
from src.research.entry_meta.dataset import EntryMetaDataset
from src.research.entry_meta.features import EntryMetaFeatureBuilder, EntryMetaFeatureMatrix
from src.research.state_edge.backends import resolve_backend


_GPU_PHASE1_NOTE = (
    "phase1 validates CUDA readiness, while this tabular estimator remains CPU-bound by design"
)


@dataclass(frozen=True)
class EntryMetaTrainingBundle:
    artifact: EntryMetaArtifact
    features: EntryMetaFeatureMatrix
    dataset: EntryMetaDataset


def train_entry_meta_bundle(
    matrix: Any,
    dataset: EntryMetaDataset,
    backend_name: str,
    model_id: str | None = None,
) -> EntryMetaTrainingBundle:
    backend = resolve_backend(backend_name)
    backend.assert_available()

    features = EntryMetaFeatureBuilder().build(matrix=matrix, dataset=dataset)
    labels = np.asarray(dataset.labels.labels, dtype=int)
    sample_weights = np.asarray(dataset.labels.sample_weights, dtype=float)
    train_indices = np.asarray(features.train_indices, dtype=int)

    probabilities, model_payload, status = _fit_predict_probabilities(
        rows=features.rows,
        labels=labels,
        sample_weights=sample_weights,
        train_indices=train_indices,
    )
    if backend.name == "gpu":
        model_payload["backend_note"] = _GPU_PHASE1_NOTE

    predictions = [
        EntryMetaPrediction(
            bar_time=features.bar_times[index],
            strategy=str(dataset.trades[index].get("strategy", "")),
            direction=str(dataset.trades[index].get("direction", "")),
            take_entry_prob=float(probabilities[index, 1]),
            block_entry_prob=float(probabilities[index, 0]),
            threshold_context=None,
        )
        for index in range(len(dataset.trades))
    ]
    metrics = _metrics(
        labels=labels,
        probabilities=probabilities,
        test_indices=list(features.test_indices),
    )
    artifact = EntryMetaArtifact(
        model_id=model_id or f"entry-meta-{uuid4().hex}",
        symbol=str(getattr(matrix, "symbol", "")),
        timeframe=str(getattr(matrix, "timeframe", "")),
        backend=backend.name,
        model_kind="tabular",
        feature_keys=list(features.feature_keys),
        label_summary=dict(dataset.labels.summary),
        sample_weight_summary=dict(dataset.labels.weight_summary),
        metrics=metrics,
        predictions=predictions,
        model_payload=model_payload,
        feature_manifest=dict(features.manifest),
        status=status,
    )
    return EntryMetaTrainingBundle(artifact=artifact, features=features, dataset=dataset)


def _fit_predict_probabilities(
    *,
    rows: np.ndarray,
    labels: np.ndarray,
    sample_weights: np.ndarray,
    train_indices: np.ndarray,
) -> tuple[np.ndarray, dict[str, Any], str]:
    train_labels = labels[train_indices] if len(train_indices) else np.asarray([], dtype=int)
    has_train_rows = len(train_indices) > 0
    has_feature_columns = rows.shape[1] > 0
    has_both_classes = set(int(label) for label in train_labels.tolist()) == {0, 1}

    if not has_train_rows or not has_feature_columns or not has_both_classes:
        probabilities = _constant_prior_probabilities(labels, train_indices)
        return (
            probabilities,
            {
                "estimator": "constant_prior",
                "reason": "insufficient_train_classes_or_features",
                "train_samples": int(len(train_indices)),
            },
            "refit",
        )

    from sklearn.ensemble import HistGradientBoostingClassifier

    estimator = HistGradientBoostingClassifier(random_state=0)
    estimator.fit(
        rows[train_indices],
        train_labels,
        sample_weight=sample_weights[train_indices],
    )
    raw_probabilities = estimator.predict_proba(rows)
    probabilities = _align_probabilities(raw_probabilities, estimator.classes_, len(labels))
    return (
        probabilities,
        {
            "estimator": "HistGradientBoostingClassifier",
            "train_samples": int(len(train_indices)),
            "classes": [int(item) for item in estimator.classes_],
        },
        "trained",
    )


def _constant_prior_probabilities(labels: np.ndarray, train_indices: np.ndarray) -> np.ndarray:
    if len(labels) == 0:
        return np.empty((0, 2), dtype=float)
    if len(train_indices) == 0:
        take_probability = 0.5
    else:
        take_probability = float(np.mean(labels[train_indices] == 1))
    block_probability = 1.0 - take_probability
    return np.tile(
        np.asarray([[block_probability, take_probability]], dtype=float),
        (len(labels), 1),
    )


def _align_probabilities(
    raw_probabilities: np.ndarray,
    classes: np.ndarray,
    n_samples: int,
) -> np.ndarray:
    probabilities = np.zeros((n_samples, 2), dtype=float)
    for class_index, label in enumerate(classes):
        probabilities[:, int(label)] = raw_probabilities[:, class_index]
    row_sums = probabilities.sum(axis=1)
    missing_rows = row_sums == 0.0
    probabilities[missing_rows, :] = 0.5
    row_sums[missing_rows] = 1.0
    return probabilities / row_sums[:, None]


def _metrics(
    *,
    labels: np.ndarray,
    probabilities: np.ndarray,
    test_indices: list[int],
) -> dict[str, Any]:
    oos_samples = len(test_indices)
    if oos_samples:
        oos_labels = labels[test_indices]
        oos_predicted = (probabilities[test_indices, 1] >= 0.5).astype(int)
        oos_accuracy = float(np.mean(oos_predicted == oos_labels))
    else:
        oos_accuracy = 0.0

    if len(probabilities):
        mean_take = float(np.mean(probabilities[:, 1]))
        mean_block = float(np.mean(probabilities[:, 0]))
    else:
        mean_take = 0.0
        mean_block = 0.0

    return {
        "oos_samples": int(oos_samples),
        "oos_accuracy": oos_accuracy,
        "probability_distribution": {
            "mean_take_entry_prob": mean_take,
            "mean_block_entry_prob": mean_block,
        },
    }
