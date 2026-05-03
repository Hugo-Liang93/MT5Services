from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from src.research.state_edge.labels import StateEdgeClass, StateEdgeLabelSet
from src.research.state_edge.sequence import SequenceWindowMatrix


@dataclass(frozen=True)
class ShapeNeighbor:
    target_index: int
    similarity: float
    label: str
    best_return: float


@dataclass(frozen=True)
class ShapeNeighborResult:
    target_index: int
    neighbors: list[ShapeNeighbor]
    label_counts: dict[str, int]
    mean_best_return: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_index": self.target_index,
            "neighbors": [item.__dict__ for item in self.neighbors],
            "label_counts": dict(self.label_counts),
            "mean_best_return": self.mean_best_return,
        }


class ShapeNeighborIndex:
    """历史相似 K 线形态检索索引。

    查询时只返回 target_index 之前的窗口，避免把未来 analog 当作证据。
    """

    def __init__(
        self,
        *,
        embeddings: np.ndarray,
        target_indices: list[int],
        labels: StateEdgeLabelSet,
    ) -> None:
        self._embeddings = embeddings
        self._target_indices = target_indices
        self._labels = labels
        self._pos_by_target = {target: pos for pos, target in enumerate(target_indices)}

    @classmethod
    def build(
        cls,
        sequence: SequenceWindowMatrix,
        labels: StateEdgeLabelSet,
    ) -> "ShapeNeighborIndex":
        flat = sequence.windows.reshape(sequence.windows.shape[0], -1)
        flat = np.nan_to_num(flat.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)
        mean = flat.mean(axis=0, keepdims=True) if len(flat) else 0.0
        std = flat.std(axis=0, keepdims=True) if len(flat) else 1.0
        std = np.where(std == 0.0, 1.0, std)
        normalized = (flat - mean) / std
        norms = np.linalg.norm(normalized, axis=1, keepdims=True)
        norms = np.where(norms == 0.0, 1.0, norms)
        embeddings = normalized / norms
        return cls(
            embeddings=embeddings,
            target_indices=list(sequence.target_indices),
            labels=labels,
        )

    def query(self, *, target_index: int, top_k: int = 5) -> ShapeNeighborResult:
        if target_index not in self._pos_by_target:
            raise ValueError(f"target_index is not in sequence index: {target_index}")
        target_pos = self._pos_by_target[target_index]
        candidate_positions = [
            pos
            for pos, idx in enumerate(self._target_indices)
            if idx < target_index
        ]
        if not candidate_positions:
            return ShapeNeighborResult(
                target_index=target_index,
                neighbors=[],
                label_counts={},
                mean_best_return=0.0,
            )
        target_embedding = self._embeddings[target_pos]
        candidate_embeddings = self._embeddings[candidate_positions]
        similarities = candidate_embeddings @ target_embedding
        order = np.argsort(-similarities)[:top_k]
        neighbors: list[ShapeNeighbor] = []
        for rank_pos in order:
            source_pos = candidate_positions[int(rank_pos)]
            source_index = self._target_indices[source_pos]
            label = self._labels.labels[source_index]
            neighbors.append(
                ShapeNeighbor(
                    target_index=source_index,
                    similarity=float(similarities[int(rank_pos)]),
                    label=str(label.value),
                    best_return=self._best_return(source_index, label),
                )
            )
        counts: dict[str, int] = {}
        for item in neighbors:
            counts[item.label] = counts.get(item.label, 0) + 1
        mean_return = (
            float(np.mean([item.best_return for item in neighbors])) if neighbors else 0.0
        )
        return ShapeNeighborResult(
            target_index=target_index,
            neighbors=neighbors,
            label_counts=counts,
            mean_best_return=mean_return,
        )

    def _best_return(self, index: int, label: StateEdgeClass) -> float:
        if label is StateEdgeClass.LONG:
            value = self._labels.long_best_return[index]
        elif label is StateEdgeClass.SHORT:
            value = self._labels.short_best_return[index]
        else:
            long_value = self._labels.long_best_return[index]
            short_value = self._labels.short_best_return[index]
            candidates = [v for v in (long_value, short_value) if v is not None]
            return float(max(candidates)) if candidates else 0.0
        return 0.0 if value is None else float(value)
