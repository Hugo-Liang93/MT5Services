from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.research.state_edge.artifacts import (
    StateEdgeArtifact,
    StateEdgePrediction,
    load_artifact,
)


@dataclass(frozen=True)
class StateEdgeOverlayVerdict:
    allowed: bool
    reason: str
    direction_probability: float
    threshold: float
    prediction: StateEdgePrediction | None = None


class StateEdgeBacktestOverlay:
    """State Edge artifact 的回测只读 overlay。

    shadow: 记录概率与覆盖率，不改变交易。
    filter: 按入场方向概率过滤回测入场，不接入 demo/live runtime。
    """

    def __init__(
        self,
        artifact: StateEdgeArtifact,
        *,
        mode: str = "shadow",
        threshold: float = 0.50,
        filter_directions: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"shadow", "filter"}:
            raise ValueError(f"Unsupported state edge overlay mode: {mode}")
        self._artifact = artifact
        self._mode = normalized_mode
        self._threshold = float(threshold)
        self._filter_directions = self._normalize_filter_directions(filter_directions)
        self._predictions = {
            self._normalize_time(pred.bar_time): pred for pred in artifact.predictions
        }
        self.reset()

    @classmethod
    def from_artifact_path(
        cls,
        path: str | Path,
        *,
        mode: str = "shadow",
        threshold: float = 0.50,
        filter_directions: tuple[str, ...] | list[str] | None = None,
    ) -> "StateEdgeBacktestOverlay":
        return cls(
            load_artifact(path),
            mode=mode,
            threshold=threshold,
            filter_directions=filter_directions,
        )

    def reset(self) -> None:
        self._observed = 0
        self._allowed = 0
        self._blocked = 0
        self._missing_predictions = 0
        self._blocked_by_direction: dict[str, int] = {}
        self._blocked_by_reason: dict[str, int] = {}
        self._probability_sum_by_direction: dict[str, float] = {}
        self._count_by_direction: dict[str, int] = {}

    def evaluate(
        self,
        bar_time: datetime,
        direction: str,
        *,
        strategy: str = "",
        confidence: float = 0.0,
    ) -> StateEdgeOverlayVerdict:
        del strategy, confidence
        normalized_direction = str(direction).strip().lower()
        self._observed += 1
        prediction = self._predictions.get(self._normalize_time(bar_time))
        if prediction is None:
            self._missing_predictions += 1
            if self._mode == "filter":
                self._record_block(
                    normalized_direction, "state_edge_prediction_missing"
                )
                return StateEdgeOverlayVerdict(
                    allowed=False,
                    reason="state_edge_prediction_missing",
                    direction_probability=0.0,
                    threshold=self._threshold,
                    prediction=None,
                )
            self._allowed += 1
            return StateEdgeOverlayVerdict(
                allowed=True,
                reason="state_edge_prediction_missing",
                direction_probability=0.0,
                threshold=self._threshold,
                prediction=None,
            )

        probability = self._direction_probability(prediction, normalized_direction)
        self._probability_sum_by_direction[normalized_direction] = (
            self._probability_sum_by_direction.get(normalized_direction, 0.0)
            + probability
        )
        self._count_by_direction[normalized_direction] = (
            self._count_by_direction.get(normalized_direction, 0) + 1
        )
        if (
            self._mode == "filter"
            and normalized_direction not in self._filter_directions
        ):
            self._allowed += 1
            return StateEdgeOverlayVerdict(
                allowed=True,
                reason="state_edge_direction_not_filtered",
                direction_probability=probability,
                threshold=self._threshold,
                prediction=prediction,
            )
        if self._mode == "filter" and probability < self._threshold:
            self._record_block(
                normalized_direction,
                "state_edge_probability_below_threshold",
            )
            return StateEdgeOverlayVerdict(
                allowed=False,
                reason="state_edge_probability_below_threshold",
                direction_probability=probability,
                threshold=self._threshold,
                prediction=prediction,
            )
        self._allowed += 1
        return StateEdgeOverlayVerdict(
            allowed=True,
            reason="allowed",
            direction_probability=probability,
            threshold=self._threshold,
            prediction=prediction,
        )

    def report(self) -> dict[str, Any]:
        avg_probability = {
            direction: (
                self._probability_sum_by_direction[direction]
                / self._count_by_direction[direction]
            )
            for direction in sorted(self._count_by_direction)
        }
        return {
            "model_id": self._artifact.model_id,
            "artifact_timeframe": self._artifact.timeframe,
            "mode": self._mode,
            "threshold": self._threshold,
            "filter_directions": sorted(self._filter_directions),
            "observed": self._observed,
            "allowed": self._allowed,
            "blocked": self._blocked,
            "missing_predictions": self._missing_predictions,
            "blocked_by_direction": dict(sorted(self._blocked_by_direction.items())),
            "blocked_by_reason": dict(sorted(self._blocked_by_reason.items())),
            "average_direction_probability": avg_probability,
        }

    def _record_block(self, direction: str, reason: str) -> None:
        self._blocked += 1
        self._blocked_by_direction[direction] = (
            self._blocked_by_direction.get(direction, 0) + 1
        )
        self._blocked_by_reason[reason] = self._blocked_by_reason.get(reason, 0) + 1

    @staticmethod
    def _normalize_filter_directions(
        directions: tuple[str, ...] | list[str] | None,
    ) -> set[str]:
        if directions is None:
            return {"buy", "sell"}
        normalized: set[str] = set()
        for item in directions:
            raw = str(item).strip().lower()
            if raw in {"all", "*"}:
                return {"buy", "sell"}
            if raw in {"buy", "long"}:
                normalized.add("buy")
            elif raw in {"sell", "short"}:
                normalized.add("sell")
            elif raw:
                raise ValueError(f"Unsupported state edge filter direction: {item}")
        return normalized or {"buy", "sell"}

    @staticmethod
    def _direction_probability(
        prediction: StateEdgePrediction, direction: str
    ) -> float:
        if direction == "buy":
            return float(prediction.long_edge_prob)
        if direction == "sell":
            return float(prediction.short_edge_prob)
        return float(prediction.no_trade_prob)

    @staticmethod
    def _normalize_time(value: datetime | str) -> str:
        if isinstance(value, datetime):
            normalized = (
                value
                if value.tzinfo is not None
                else value.replace(tzinfo=timezone.utc)
            )
            return normalized.astimezone(timezone.utc).isoformat()
        raw = str(value)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return raw
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
