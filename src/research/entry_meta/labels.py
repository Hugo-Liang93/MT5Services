from __future__ import annotations

import math
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
        if self._max_weight < 1.0:
            raise ValueError("max_weight must be at least 1.0")

    def build(self, trades: list[dict[str, Any]]) -> EntryMetaLabelSet:
        labels: list[int] = []
        weights: list[float] = []
        for index, trade in enumerate(trades):
            pnl = self._pnl_from_trade(trade, index)
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

    def _pnl_from_trade(self, trade: dict[str, Any], index: int) -> float:
        if "pnl" not in trade:
            raise ValueError(f"sample {index} missing required field pnl")
        raw_pnl = trade["pnl"]
        if raw_pnl is None or raw_pnl == "":
            raise ValueError(f"sample {index} field pnl must be a non-empty number")
        try:
            pnl = float(raw_pnl)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"sample {index} field pnl must be numeric") from exc
        if not math.isfinite(pnl):
            raise ValueError(f"sample {index} field pnl must be a finite number")
        return pnl
