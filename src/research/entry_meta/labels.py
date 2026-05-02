from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EntryMetaLabelSet:
    labels: list[int]
    sample_weights: list[float]
    summary: dict[str, int]
    weight_summary: dict[str, float]


class EntryMetaLabelBuilder:
    def __init__(self, *, max_weight: float = 5.0) -> None:
        self._max_weight = float(max_weight)

    def build(self, trades: list[dict[str, Any]]) -> EntryMetaLabelSet:
        labels: list[int] = []
        weights: list[float] = []
        for trade in trades:
            pnl = float(trade.get("pnl", 0.0) or 0.0)
            labels.append(1 if pnl > 0.0 else 0)
            weights.append(self._weight_for_pnl(pnl))
        take = sum(1 for item in labels if item == 1)
        block = len(labels) - take
        return EntryMetaLabelSet(
            labels=labels,
            sample_weights=weights,
            summary={"take_entry": take, "block_entry": block},
            weight_summary={
                "min": min(weights) if weights else 0.0,
                "max": max(weights) if weights else 0.0,
                "mean": round(sum(weights) / len(weights), 10) if weights else 0.0,
            },
        )

    def _weight_for_pnl(self, pnl: float) -> float:
        raw = 1.0 + min(abs(pnl) / 100.0, self._max_weight - 1.0)
        return round(min(raw, self._max_weight), 10)
