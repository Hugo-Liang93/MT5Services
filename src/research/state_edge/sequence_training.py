from __future__ import annotations

from typing import Any

import numpy as np

from src.research.core.data_matrix import DataMatrix
from src.research.state_edge.artifacts import StateEdgeArtifact, StateEdgePrediction
from src.research.state_edge.backends import BackendUnavailableError, resolve_backend
from src.research.state_edge.features import StateEdgeFeatureBuilder
from src.research.state_edge.labels import StateEdgeLabelBuilder
from src.research.state_edge.neighbors import ShapeNeighborIndex
from src.research.state_edge.sequence import SequenceWindowBuilder
from src.research.state_edge.training import (
    CLASS_ORDER,
    _build_metrics,
    _class_prior,
    _normalize_probs,
)


def train_sequence_tcn_artifact(
    matrix: DataMatrix,
    *,
    backend_name: str,
    model_id: str,
    sequence_window: int = 64,
    epochs: int = 8,
    batch_size: int = 512,
    learning_rate: float = 1e-3,
) -> StateEdgeArtifact:
    return _train_sequence_artifact(
        matrix,
        backend_name=backend_name,
        model_id=model_id,
        sequence_window=sequence_window,
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
        architecture="sequence_tcn",
    )


def train_sequence_mlp_artifact(
    matrix: DataMatrix,
    *,
    backend_name: str,
    model_id: str,
    sequence_window: int = 64,
    epochs: int = 8,
    batch_size: int = 512,
    learning_rate: float = 1e-3,
) -> StateEdgeArtifact:
    return _train_sequence_artifact(
        matrix,
        backend_name=backend_name,
        model_id=model_id,
        sequence_window=sequence_window,
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
        architecture="sequence_mlp",
    )


def _train_sequence_artifact(
    matrix: DataMatrix,
    *,
    backend_name: str,
    model_id: str,
    sequence_window: int,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    architecture: str,
) -> StateEdgeArtifact:
    backend = resolve_backend(backend_name)
    backend.assert_available()
    if backend.name == "gpu":
        try:
            import torch
        except Exception as exc:  # pragma: no cover - environment dependent
            raise BackendUnavailableError(f"PyTorch unavailable: {exc}") from exc
        if not torch.cuda.is_available():  # pragma: no cover - environment dependent
            raise BackendUnavailableError("CUDA backend unavailable for sequence_tcn")

    labels = StateEdgeLabelBuilder().build(matrix)
    features = StateEdgeFeatureBuilder().build(matrix)
    sequence = SequenceWindowBuilder(window=sequence_window).build(matrix, features, labels)
    y = np.asarray(sequence.labels, dtype=np.int64)
    train_samples = np.asarray(sequence.train_sample_indices, dtype=np.int64)

    probs_by_sample, payload, status = _fit_sequence_model(
        sequence.windows,
        y,
        train_samples,
        backend_name=backend.name,
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
        architecture=architecture,
    )
    full_probs = _fill_full_bar_probabilities(
        matrix.n_bars,
        sequence.target_indices,
        probs_by_sample,
        y[train_samples] if len(train_samples) else y,
    )
    predictions = [
        StateEdgePrediction(
            bar_time=matrix.bar_times[idx].isoformat(),
            long_edge_prob=float(full_probs[idx, 0]),
            short_edge_prob=float(full_probs[idx, 1]),
            no_trade_prob=float(full_probs[idx, 2]),
        )
        for idx in range(matrix.n_bars)
    ]
    all_y = np.asarray([CLASS_ORDER.index(label) for label in labels.labels], dtype=int)
    metrics = _build_metrics(
        labels=labels,
        y=all_y,
        probs=full_probs,
        test_indices=list(matrix.test_slice()),
        top_bucket_quantile=0.80,
    )
    metrics["shape_neighbors"] = _shape_neighbor_metric(sequence, labels)
    return StateEdgeArtifact(
        model_id=model_id,
        symbol=matrix.symbol,
        timeframe=matrix.timeframe,
        backend=backend.name,
        feature_keys=sequence.feature_keys,
        label_summary=labels.summary,
        metrics=metrics,
        predictions=predictions,
        model_payload={
            **payload,
            "kind": architecture,
            "sequence_window": sequence_window,
            "epochs": epochs,
            "batch_size": batch_size,
            "learning_rate": learning_rate,
        },
        feature_manifest=sequence.manifest,
        status=status,
    )


def _fit_sequence_model(
    windows: np.ndarray,
    y: np.ndarray,
    train_samples: np.ndarray,
    *,
    backend_name: str,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    architecture: str,
) -> tuple[np.ndarray, dict[str, Any], str]:
    if len(train_samples) == 0 or windows.shape[0] == 0 or len(set(y[train_samples])) < 2:
        prior = _class_prior(y[train_samples] if len(train_samples) else y)
        return np.tile(prior, (windows.shape[0], 1)), {
            "kind": "sequence_tcn",
            "reason": "insufficient_training_classes",
        }, "refit"

    import torch
    device = torch.device("cuda" if backend_name == "gpu" else "cpu")
    train_x = windows[train_samples]
    mean = train_x.mean(axis=(0, 1), keepdims=True)
    std = train_x.std(axis=(0, 1), keepdims=True)
    std = np.where(std == 0.0, 1.0, std)
    x_scaled = (windows - mean) / std

    torch.manual_seed(17)
    if architecture == "sequence_mlp":
        model = _WindowMLP(
            window=windows.shape[1],
            input_features=windows.shape[2],
            classes=len(CLASS_ORDER),
        ).to(device)
    elif architecture == "sequence_tcn":
        model = _TinyTCN(input_features=windows.shape[2], classes=len(CLASS_ORDER)).to(device)
    else:
        raise ValueError(f"Unsupported sequence architecture: {architecture}")
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=1e-4)
    loss_fn = torch.nn.CrossEntropyLoss()
    x_all = torch.tensor(x_scaled, dtype=torch.float32, device=device)
    y_all = torch.tensor(y, dtype=torch.long, device=device)
    train_idx = torch.tensor(train_samples, dtype=torch.long, device=device)
    batch = max(1, int(batch_size))
    model.train()
    for _ in range(max(1, int(epochs))):
        order = train_idx[torch.randperm(len(train_idx), device=device)]
        for start in range(0, len(order), batch):
            idx = order[start : start + batch]
            xb = x_all[idx]
            yb = y_all[idx]
            optimizer.zero_grad()
            loss = loss_fn(model(xb), yb)
            loss.backward()
            optimizer.step()

    model.eval()
    chunks: list[np.ndarray] = []
    with torch.no_grad():
        for start in range(0, len(x_scaled), batch):
            xb = x_all[start : start + batch]
            chunks.append(torch.softmax(model(xb), dim=1).detach().cpu().numpy())
    probs = _normalize_probs(np.vstack(chunks) if chunks else np.zeros((0, 3)))
    return probs, {
        "input_features": int(windows.shape[2]),
        "architecture": architecture,
        "mean": np.squeeze(mean, axis=(0, 1)).tolist(),
        "std": np.squeeze(std, axis=(0, 1)).tolist(),
    }, "trained"


class _TinyTCN:
    def __new__(cls, *, input_features: int, classes: int):
        import torch

        return torch.nn.Sequential(
            _TransposeForConv(),
            torch.nn.Conv1d(input_features, 32, kernel_size=3, padding=1),
            torch.nn.GELU(),
            torch.nn.Conv1d(32, 32, kernel_size=3, padding=1),
            torch.nn.GELU(),
            torch.nn.AdaptiveAvgPool1d(1),
            torch.nn.Flatten(),
            torch.nn.Linear(32, classes),
        )


class _WindowMLP:
    def __new__(cls, *, window: int, input_features: int, classes: int):
        import torch

        return torch.nn.Sequential(
            torch.nn.Flatten(),
            torch.nn.Linear(window * input_features, 512),
            torch.nn.GELU(),
            torch.nn.Linear(512, 128),
            torch.nn.GELU(),
            torch.nn.Linear(128, classes),
        )


class _TransposeForConv:
    def __new__(cls):
        import torch

        class _Layer(torch.nn.Module):
            def forward(self, x):
                return x.transpose(1, 2)

        return _Layer()


def _fill_full_bar_probabilities(
    n_bars: int,
    target_indices: list[int],
    probs_by_sample: np.ndarray,
    y_train: np.ndarray,
) -> np.ndarray:
    prior = _class_prior(y_train)
    full = np.tile(prior, (n_bars, 1))
    for sample_idx, target_idx in enumerate(target_indices):
        full[target_idx] = probs_by_sample[sample_idx]
    return _normalize_probs(full)


def _shape_neighbor_metric(sequence, labels) -> dict[str, Any]:
    if not sequence.target_indices:
        return {"available": False}
    index = ShapeNeighborIndex.build(sequence, labels)
    target_index = sequence.target_indices[-1]
    result = index.query(target_index=target_index, top_k=5)
    return {"available": True, "last_bar": result.to_dict()}
