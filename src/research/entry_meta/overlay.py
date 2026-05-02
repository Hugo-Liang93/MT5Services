from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.research.entry_meta.artifacts import (
    EntryMetaArtifact,
    EntryMetaPrediction,
    load_artifact,
)


@dataclass(frozen=True)
class EntryMetaOverlayVerdict:
    allowed: bool
    reason: str
    take_entry_prob: float
    block_entry_prob: float
    threshold: float
    prediction: EntryMetaPrediction | None = None


class EntryMetaBacktestOverlay:
    """Entry Meta artifact 的回测只读 overlay。

    shadow: 记录模型覆盖率与概率，不改变交易。
    filter: 仅允许 accepted artifact 参与入场过滤。
    """

    def __init__(
        self,
        artifact: EntryMetaArtifact,
        *,
        mode: str = "shadow",
        threshold: float = 0.50,
    ) -> None:
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"shadow", "filter"}:
            raise ValueError(f"Unsupported entry meta overlay mode: {mode}")
        artifact_status = str(getattr(artifact, "status", "trained"))
        if normalized_mode == "filter" and artifact_status != "accepted":
            raise ValueError(
                "Entry meta filter mode requires artifact status accepted; "
                f"got status={artifact_status}"
            )
        self._artifact = artifact
        self._mode = normalized_mode
        self._threshold = float(threshold)
        self._predictions = {
            self._prediction_key(pred.bar_time, pred.strategy, pred.direction): pred
            for pred in artifact.predictions
        }
        self.reset()

    @classmethod
    def from_artifact_path(
        cls,
        path: str | Path,
        *,
        mode: str = "shadow",
        threshold: float = 0.50,
    ) -> "EntryMetaBacktestOverlay":
        return cls(load_artifact(Path(path)), mode=mode, threshold=threshold)

    def reset(self) -> None:
        self._observed = 0
        self._allowed = 0
        self._blocked = 0
        self._missing_predictions = 0
        self._blocked_by_reason: dict[str, int] = {}
        self._blocked_by_strategy: dict[str, int] = {}

    def evaluate(
        self,
        bar_time: datetime | str,
        strategy: str,
        direction: str,
        *,
        confidence: float = 0.0,
    ) -> EntryMetaOverlayVerdict:
        del confidence
        self._observed += 1
        normalized_strategy = self._normalize_text(strategy)
        prediction = self._predictions.get(
            self._prediction_key(bar_time, normalized_strategy, direction)
        )
        if prediction is None:
            self._missing_predictions += 1
            self._allowed += 1
            return EntryMetaOverlayVerdict(
                allowed=True,
                reason="entry_meta_prediction_missing",
                take_entry_prob=1.0,
                block_entry_prob=0.0,
                threshold=self._threshold,
                prediction=None,
            )

        take_entry_prob = float(prediction.take_entry_prob)
        block_entry_prob = float(prediction.block_entry_prob)
        if self._mode == "filter" and take_entry_prob < self._threshold:
            reason = "entry_meta_probability_below_threshold"
            self._record_block(normalized_strategy, reason)
            return EntryMetaOverlayVerdict(
                allowed=False,
                reason=reason,
                take_entry_prob=take_entry_prob,
                block_entry_prob=block_entry_prob,
                threshold=self._threshold,
                prediction=prediction,
            )

        self._allowed += 1
        return EntryMetaOverlayVerdict(
            allowed=True,
            reason="allowed",
            take_entry_prob=take_entry_prob,
            block_entry_prob=block_entry_prob,
            threshold=self._threshold,
            prediction=prediction,
        )

    def report(self) -> dict[str, Any]:
        return {
            "model_id": self._artifact.model_id,
            "artifact_timeframe": self._artifact.timeframe,
            "artifact_status": self._artifact.status,
            "mode": self._mode,
            "threshold": self._threshold,
            "observed": self._observed,
            "allowed": self._allowed,
            "blocked": self._blocked,
            "missing_predictions": self._missing_predictions,
            "blocked_by_reason": dict(sorted(self._blocked_by_reason.items())),
            "blocked_by_strategy": dict(sorted(self._blocked_by_strategy.items())),
        }

    def _record_block(self, strategy: str, reason: str) -> None:
        self._blocked += 1
        self._blocked_by_reason[reason] = self._blocked_by_reason.get(reason, 0) + 1
        self._blocked_by_strategy[strategy] = (
            self._blocked_by_strategy.get(strategy, 0) + 1
        )

    @classmethod
    def _prediction_key(
        cls,
        bar_time: datetime | str,
        strategy: str,
        direction: str,
    ) -> tuple[str, str, str]:
        return (
            cls._normalize_time(bar_time),
            cls._normalize_text(strategy),
            cls._normalize_text(direction),
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        return str(value).strip().lower()

    @staticmethod
    def _normalize_time(value: datetime | str) -> str:
        if isinstance(value, datetime):
            normalized = (
                value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
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
