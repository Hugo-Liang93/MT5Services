from __future__ import annotations

import base64
import pickle
import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np

from src.research.core.data_matrix import DataMatrix
from src.research.state_edge.artifacts import StateEdgeArtifact, StateEdgePrediction
from src.research.state_edge.backends import BackendUnavailableError, resolve_backend
from src.research.state_edge.features import (
    StateEdgeFeatureBuilder,
    StateEdgeFeatureMatrix,
)
from src.research.state_edge.labels import (
    StateEdgeClass,
    StateEdgeLabelBuilder,
    StateEdgeLabelSet,
)

CLASS_ORDER: tuple[StateEdgeClass, ...] = (
    StateEdgeClass.LONG,
    StateEdgeClass.SHORT,
    StateEdgeClass.NO_TRADE,
)


@dataclass(frozen=True)
class StateEdgeTrainingBundle:
    artifact: StateEdgeArtifact
    labels: StateEdgeLabelSet
    features: StateEdgeFeatureMatrix


def train_state_edge_artifact(
    matrix: DataMatrix,
    *,
    backend_name: str,
    model_id: str | None = None,
    top_bucket_quantile: float = 0.80,
) -> StateEdgeArtifact:
    return train_state_edge_bundle(
        matrix,
        backend_name=backend_name,
        model_id=model_id,
        top_bucket_quantile=top_bucket_quantile,
    ).artifact


def train_state_edge_bundle(
    matrix: DataMatrix,
    *,
    backend_name: str,
    model_id: str | None = None,
    top_bucket_quantile: float = 0.80,
) -> StateEdgeTrainingBundle:
    backend = resolve_backend(backend_name)
    backend.assert_available()

    labels = StateEdgeLabelBuilder().build(matrix)
    features = StateEdgeFeatureBuilder().build(matrix)

    y = np.asarray([_class_to_int(label) for label in labels.labels], dtype=int)
    train_idx = np.asarray(features.train_indices, dtype=int)
    train_idx = train_idx[(train_idx >= 0) & (train_idx < matrix.n_bars)]

    if backend.name == "gpu":
        probs, payload, status = _fit_predict_gpu(features.rows, y, train_idx)
    else:
        probs, payload, status = _fit_predict_cpu(features.rows, y, train_idx)

    predictions = [
        StateEdgePrediction(
            bar_time=features.bar_times[idx],
            long_edge_prob=float(probs[idx, 0]),
            short_edge_prob=float(probs[idx, 1]),
            no_trade_prob=float(probs[idx, 2]),
        )
        for idx in range(matrix.n_bars)
    ]

    metrics = _build_metrics(
        labels=labels,
        y=y,
        probs=probs,
        test_indices=features.test_indices,
        top_bucket_quantile=top_bucket_quantile,
    )
    artifact = StateEdgeArtifact(
        model_id=model_id or f"state-edge-{matrix.timeframe}-{uuid.uuid4().hex[:12]}",
        symbol=matrix.symbol,
        timeframe=matrix.timeframe,
        backend=backend.name,
        feature_keys=features.feature_keys,
        label_summary=labels.summary,
        metrics=metrics,
        predictions=predictions,
        model_payload=payload,
        feature_manifest=features.manifest,
        status=status,
    )
    return StateEdgeTrainingBundle(artifact=artifact, labels=labels, features=features)


def _fit_predict_cpu(
    x: np.ndarray,
    y: np.ndarray,
    train_idx: np.ndarray,
) -> tuple[np.ndarray, dict[str, Any], str]:
    if len(train_idx) == 0 or x.shape[1] == 0 or len(set(y[train_idx].tolist())) < 2:
        return (
            _constant_prediction(x.shape[0], y[train_idx]),
            {
                "kind": "constant",
                "class_probs": _class_prior(y[train_idx]).tolist(),
            },
            "refit",
        )

    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
    except Exception:
        return (
            _constant_prediction(x.shape[0], y[train_idx]),
            {
                "kind": "constant",
                "reason": "sklearn_unavailable",
                "class_probs": _class_prior(y[train_idx]).tolist(),
            },
            "refit",
        )

    model = HistGradientBoostingClassifier(
        learning_rate=0.06,
        max_iter=80,
        max_leaf_nodes=15,
        l2_regularization=0.05,
        random_state=17,
    )
    model.fit(x[train_idx], y[train_idx])
    probs = _align_sklearn_proba(model, x)
    payload = {
        "kind": "sklearn_pickle",
        "estimator": "HistGradientBoostingClassifier",
        "classes": [int(c) for c in model.classes_],
        "pickle_b64": base64.b64encode(pickle.dumps(model)).decode("ascii"),
    }
    return probs, payload, "trained"


def _fit_predict_gpu(
    x: np.ndarray,
    y: np.ndarray,
    train_idx: np.ndarray,
) -> tuple[np.ndarray, dict[str, Any], str]:
    try:
        import torch
    except Exception as exc:  # pragma: no cover - environment dependent
        raise BackendUnavailableError(
            f"CUDA backend unavailable: PyTorch import failed: {exc}"
        ) from exc

    if not torch.cuda.is_available():  # pragma: no cover - environment dependent
        raise BackendUnavailableError(
            "CUDA backend unavailable: PyTorch CUDA device unavailable"
        )
    if len(train_idx) == 0 or x.shape[1] == 0 or len(set(y[train_idx].tolist())) < 2:
        return (
            _constant_prediction(x.shape[0], y[train_idx]),
            {
                "kind": "constant",
                "reason": "insufficient_gpu_training_classes",
                "class_probs": _class_prior(y[train_idx]).tolist(),
            },
            "refit",
        )

    device = torch.device("cuda")
    mean = x[train_idx].mean(axis=0)
    std = x[train_idx].std(axis=0)
    std[std == 0] = 1.0
    x_scaled = (x - mean) / std

    torch.manual_seed(17)
    model = torch.nn.Linear(x.shape[1], len(CLASS_ORDER)).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.03, weight_decay=1e-4)
    loss_fn = torch.nn.CrossEntropyLoss()
    x_train = torch.tensor(x_scaled[train_idx], dtype=torch.float32, device=device)
    y_train = torch.tensor(y[train_idx], dtype=torch.long, device=device)
    for _ in range(160):  # pragma: no cover - environment dependent
        optimizer.zero_grad()
        loss = loss_fn(model(x_train), y_train)
        loss.backward()
        optimizer.step()
    with torch.no_grad():  # pragma: no cover - environment dependent
        all_tensor = torch.tensor(x_scaled, dtype=torch.float32, device=device)
        probs = torch.softmax(model(all_tensor), dim=1).detach().cpu().numpy()
    payload = {
        "kind": "torch_linear",
        "mean": mean.tolist(),
        "std": std.tolist(),
        "weight": model.weight.detach().cpu().numpy().tolist(),
        "bias": model.bias.detach().cpu().numpy().tolist(),
    }
    return _normalize_probs(probs), payload, "trained"


def _align_sklearn_proba(model: Any, x: np.ndarray) -> np.ndarray:
    raw = model.predict_proba(x)
    aligned = np.zeros((x.shape[0], len(CLASS_ORDER)), dtype=float)
    for col, class_id in enumerate(model.classes_):
        aligned[:, int(class_id)] = raw[:, col]
    return _normalize_probs(aligned)


def _constant_prediction(n_rows: int, y_train: np.ndarray) -> np.ndarray:
    prior = _class_prior(y_train)
    return np.tile(prior, (n_rows, 1))


def _class_prior(y_train: np.ndarray) -> np.ndarray:
    if len(y_train) == 0:
        return np.asarray([1 / 3, 1 / 3, 1 / 3], dtype=float)
    counts = np.bincount(y_train, minlength=len(CLASS_ORDER)).astype(float)
    counts += 1.0
    return counts / counts.sum()


def _normalize_probs(probs: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(probs, dtype=float), 0.0, 1.0)
    row_sums = clipped.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0.0] = 1.0
    return clipped / row_sums


def _class_to_int(label: StateEdgeClass) -> int:
    return CLASS_ORDER.index(label)


def _build_metrics(
    *,
    labels: StateEdgeLabelSet,
    y: np.ndarray,
    probs: np.ndarray,
    test_indices: list[int],
    top_bucket_quantile: float,
) -> dict[str, Any]:
    valid_test = [idx for idx in test_indices if 0 <= idx < len(y)]
    if valid_test:
        predicted = np.argmax(probs[valid_test], axis=1)
        oos_accuracy = float(np.mean(predicted == y[valid_test]))
    else:
        oos_accuracy = 0.0

    prob_means = probs.mean(axis=0) if len(probs) else np.zeros(len(CLASS_ORDER))
    return {
        "oos_samples": len(valid_test),
        "oos_accuracy": oos_accuracy,
        "probability_distribution": {
            "mean_long_edge_prob": float(prob_means[0]),
            "mean_short_edge_prob": float(prob_means[1]),
            "mean_no_trade_prob": float(prob_means[2]),
        },
        "top_probability_buckets": {
            "long": _top_bucket_summary(
                probs[:, 0],
                labels.long_best_return,
                valid_test,
                top_bucket_quantile,
            ),
            "short": _top_bucket_summary(
                probs[:, 1],
                labels.short_best_return,
                valid_test,
                top_bucket_quantile,
            ),
        },
    }


def _top_bucket_summary(
    direction_probs: np.ndarray,
    returns: list[float | None],
    test_indices: list[int],
    quantile: float,
) -> dict[str, Any]:
    valid = [
        idx for idx in test_indices if idx < len(returns) and returns[idx] is not None
    ]
    if not valid:
        return {
            "sample_count": 0,
            "mean_cost_after_return": 0.0,
            "hit_rate": 0.0,
            "hit_rate_lift": 0.0,
            "threshold": 0.0,
        }
    base_returns = np.asarray([float(returns[idx]) for idx in valid], dtype=float)
    threshold = float(np.quantile(direction_probs[valid], quantile))
    selected = [idx for idx in valid if direction_probs[idx] >= threshold]
    selected_returns = np.asarray(
        [float(returns[idx]) for idx in selected], dtype=float
    )
    baseline_hit = float(np.mean(base_returns > 0.0)) if len(base_returns) else 0.0
    bucket_hit = (
        float(np.mean(selected_returns > 0.0)) if len(selected_returns) else 0.0
    )
    return {
        "sample_count": int(len(selected_returns)),
        "mean_cost_after_return": (
            float(selected_returns.mean()) if len(selected_returns) else 0.0
        ),
        "hit_rate": bucket_hit,
        "hit_rate_lift": bucket_hit - baseline_hit,
        "threshold": threshold,
    }
