from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import numpy as np

from src.research.entry_meta.artifacts import EntryMetaArtifact, EntryMetaPrediction
from src.research.entry_meta.dataset import EntryMetaDataset
from src.research.entry_meta.features import EntryMetaFeatureBuilder, EntryMetaFeatureMatrix
from src.research.entry_meta.quality import evaluate_entry_meta_quality
from src.research.core.backends import resolve_backend


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
    *,
    min_samples: int = 40,
    min_oos_samples: int = 10,
    min_class_samples: int = 10,
) -> EntryMetaTrainingBundle:
    backend = resolve_backend(backend_name)
    backend.assert_available()

    features = EntryMetaFeatureBuilder().build(matrix=matrix, dataset=dataset)
    labels = np.asarray(dataset.labels.labels, dtype=int)
    sample_weights = np.asarray(dataset.labels.sample_weights, dtype=float)
    _validate_training_contract(features, dataset, labels, sample_weights)
    train_indices = np.asarray(features.train_indices, dtype=int)

    probabilities, model_payload, fit_status = _fit_predict_probabilities(
        rows=features.rows,
        labels=labels,
        sample_weights=sample_weights,
        train_indices=train_indices,
    )
    _validate_probabilities(probabilities, len(labels))
    model_payload["prediction_reuse"] = "deferred"
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
    quality = evaluate_entry_meta_quality(
        dict(dataset.labels.summary),
        metrics,
        min_samples=min_samples,
        min_oos_samples=min_oos_samples,
        min_class_samples=min_class_samples,
    )
    if fit_status == "refit":
        quality = dict(quality)
        if quality.get("status") == "accepted":
            quality["gate_reason"] = quality.get("reason", "accepted")
            quality["reason"] = str(model_payload.get("reason", "estimator_refit"))
        quality["status"] = "refit"
    metrics["fit_status"] = fit_status
    metrics["quality"] = dict(quality)
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
        status=str(quality["status"]),
    )
    return EntryMetaTrainingBundle(artifact=artifact, features=features, dataset=dataset)


def _validate_training_contract(
    features: EntryMetaFeatureMatrix,
    dataset: EntryMetaDataset,
    labels: np.ndarray,
    sample_weights: np.ndarray,
) -> None:
    n_samples = len(dataset.trades)
    length_fields = {
        "dataset.trades": n_samples,
        "labels": len(dataset.labels.labels),
        "sample_weights": len(dataset.labels.sample_weights),
        "features.bar_times": len(features.bar_times),
        "rows": int(features.rows.shape[0]) if features.rows.ndim >= 1 else 0,
    }
    if len(set(length_fields.values())) != 1:
        details = ", ".join(f"{name}={length}" for name, length in length_fields.items())
        raise ValueError(
            "entry meta training contract length mismatch across "
            f"dataset.trades, labels, sample_weights, features.bar_times, rows: {details}"
        )
    if labels.shape[0] != n_samples or sample_weights.shape[0] != n_samples:
        raise ValueError(
            "entry meta training contract labels and sample_weights arrays "
            f"must match n_samples {n_samples}"
        )
    if features.rows.ndim != 2:
        raise ValueError("entry meta training contract rows must be a 2D matrix")
    if features.rows.shape[1] != len(features.feature_keys):
        raise ValueError(
            "entry meta training contract rows feature dimension "
            f"{features.rows.shape[1]} must match feature_keys {len(features.feature_keys)}"
        )
    _validate_index_contract("train_indices", features.train_indices, n_samples)
    _validate_index_contract("test_indices", features.test_indices, n_samples)


def _validate_index_contract(name: str, indices: list[int], n_samples: int) -> None:
    for index in indices:
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError(f"entry meta training contract {name} must contain int indices")
        if index < 0 or index >= n_samples:
            raise ValueError(
                f"entry meta training contract {name} index {index} out of range [0, {n_samples})"
            )


def _validate_probabilities(probabilities: np.ndarray, n_samples: int) -> None:
    if probabilities.shape != (n_samples, 2):
        raise ValueError(
            "entry meta training probabilities shape "
            f"{probabilities.shape} must be ({n_samples}, 2)"
        )
    if not np.all(np.isfinite(probabilities)):
        raise ValueError("entry meta training probabilities must be finite")
    if not np.all((probabilities >= 0.0) & (probabilities <= 1.0)):
        raise ValueError("entry meta training probabilities values must be in range [0, 1]")
    row_sums = probabilities.sum(axis=1)
    if not np.allclose(row_sums, 1.0, atol=1e-8):
        raise ValueError("entry meta training probabilities row sums must sum to 1.0")


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
        class_probs = _class_probs_from_probabilities(probabilities)
        return (
            probabilities,
            {
                "estimator": "constant_prior",
                "reason": "insufficient_train_classes_or_features",
                "train_samples": int(len(train_indices)),
                "class_probs": class_probs,
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


def _class_probs_from_probabilities(probabilities: np.ndarray) -> dict[str, float]:
    if len(probabilities) == 0:
        return {"block_entry": 0.5, "take_entry": 0.5}
    return {
        "block_entry": float(probabilities[0, 0]),
        "take_entry": float(probabilities[0, 1]),
    }


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
