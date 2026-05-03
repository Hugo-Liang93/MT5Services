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
from src.research.entry_meta.features import (
    EntryMetaFeatureBuildError,
    EntryMetaFeatureContext,
    EntryMetaFeatureRowBuilder,
)
from src.research.entry_meta.scoring import EntryMetaScorer, EntryMetaScoringError


@dataclass(frozen=True)
class EntryMetaOverlayVerdict:
    allowed: bool
    reason: str
    take_entry_prob: float
    block_entry_prob: float
    threshold: float
    prediction: EntryMetaPrediction | None = None
    score_source: str = "artifact_prediction"


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
        feature_manifest = getattr(artifact, "feature_manifest", {})
        if not isinstance(feature_manifest, dict):
            feature_manifest = {}
        self._feature_scope = str(
            feature_manifest.get("feature_scope", "research_full")
        )
        self._dynamic_scoring_supported = bool(
            feature_manifest.get("dynamic_scoring_supported", False)
        )
        if self._dynamic_scoring_supported and self._feature_scope != "runtime_safe":
            raise ValueError(
                "Entry meta artifact declares dynamic_scoring_supported=True "
                f"but feature_scope={self._feature_scope!r}"
            )
        self._dynamic_unsupported_reason = (
            "entry_meta_dynamic_feature_scope_unsupported"
        )
        category_mappings = dict(feature_manifest.get("category_mappings", {}))
        self._feature_row_builder: EntryMetaFeatureRowBuilder | None = None
        self._scorer: EntryMetaScorer | None = None
        if self._dynamic_scoring_supported:
            try:
                self._feature_row_builder = EntryMetaFeatureRowBuilder(
                    feature_keys=list(artifact.feature_keys),
                    category_mappings=category_mappings,
                )
                self._scorer = EntryMetaScorer.from_payload(
                    artifact.model_payload,
                    feature_keys=list(artifact.feature_keys),
                )
            except (
                EntryMetaFeatureBuildError,
                EntryMetaScoringError,
                TypeError,
                ValueError,
            ):
                self._feature_row_builder = None
                self._scorer = None
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
        self._matched_predictions = 0
        self._take_entry_prob_sum = 0.0
        self._block_entry_prob_sum = 0.0
        self._take_entry_prob_min: float | None = None
        self._take_entry_prob_max: float | None = None
        self._block_entry_prob_min: float | None = None
        self._block_entry_prob_max: float | None = None
        self._take_entry_prob_below_threshold = 0
        self._take_entry_prob_at_or_above_threshold = 0
        self._blocked_by_reason: dict[str, int] = {}
        self._blocked_by_strategy: dict[str, int] = {}
        self._score_source_counts: dict[str, int] = {}
        self._missing_by_reason: dict[str, int] = {}
        self._dynamic_scored = 0
        self._dynamic_score_failures = 0

    def evaluate(
        self,
        bar_time: datetime | str,
        strategy: str,
        direction: str,
        *,
        confidence: float = 0.0,
        feature_context: EntryMetaFeatureContext | None = None,
    ) -> EntryMetaOverlayVerdict:
        del confidence
        self._observed += 1
        normalized_strategy = self._normalize_text(strategy)
        prediction = self._predictions.get(
            self._prediction_key(bar_time, normalized_strategy, direction)
        )
        if prediction is None:
            dynamic = self._score_dynamic(feature_context)
            if isinstance(dynamic, str):
                return self._allow_missing(dynamic)
            take_entry_prob, block_entry_prob, score_source = dynamic
            prediction_for_verdict = None
        else:
            take_entry_prob = float(prediction.take_entry_prob)
            block_entry_prob = float(prediction.block_entry_prob)
            score_source = "artifact_prediction"
            prediction_for_verdict = prediction
            self._record_score_source(score_source)

        self._record_probability(take_entry_prob, block_entry_prob)
        if self._mode == "filter" and take_entry_prob < self._threshold:
            reason = "entry_meta_probability_below_threshold"
            self._record_block(normalized_strategy, reason)
            return EntryMetaOverlayVerdict(
                allowed=False,
                reason=reason,
                take_entry_prob=take_entry_prob,
                block_entry_prob=block_entry_prob,
                threshold=self._threshold,
                prediction=prediction_for_verdict,
                score_source=score_source,
            )

        self._allowed += 1
        return EntryMetaOverlayVerdict(
            allowed=True,
            reason="allowed",
            take_entry_prob=take_entry_prob,
            block_entry_prob=block_entry_prob,
            threshold=self._threshold,
            prediction=prediction_for_verdict,
            score_source=score_source,
        )

    def report(self) -> dict[str, Any]:
        return {
            "model_id": self._artifact.model_id,
            "artifact_timeframe": self._artifact.timeframe,
            "artifact_status": self._artifact.status,
            "feature_scope": self._feature_scope,
            "dynamic_scoring_supported": self._dynamic_scoring_supported,
            "mode": self._mode,
            "threshold": self._threshold,
            "observed": self._observed,
            "allowed": self._allowed,
            "blocked": self._blocked,
            "missing_predictions": self._missing_predictions,
            "probability_summary": self._probability_summary(),
            "blocked_by_reason": dict(sorted(self._blocked_by_reason.items())),
            "blocked_by_strategy": dict(sorted(self._blocked_by_strategy.items())),
            "score_source_counts": dict(sorted(self._score_source_counts.items())),
            "missing_by_reason": dict(sorted(self._missing_by_reason.items())),
            "dynamic_scored": self._dynamic_scored,
            "dynamic_score_failures": self._dynamic_score_failures,
        }

    def _score_dynamic(
        self,
        feature_context: EntryMetaFeatureContext | None,
    ) -> tuple[float, float, str] | str:
        if not self._dynamic_scoring_supported:
            return self._dynamic_unsupported_reason
        if feature_context is None:
            return "entry_meta_feature_context_missing"
        if self._feature_row_builder is None or self._scorer is None:
            return "entry_meta_unsupported_scorer"
        try:
            row = self._feature_row_builder.build(feature_context)
            score = self._scorer.score(row.values)
        except (EntryMetaFeatureBuildError, EntryMetaScoringError) as exc:
            return self._normalize_dynamic_failure_reason(str(exc))
        self._dynamic_scored += 1
        self._record_score_source(score.score_source)
        return score.take_entry_prob, score.block_entry_prob, score.score_source

    def _allow_missing(self, reason: str) -> EntryMetaOverlayVerdict:
        self._record_missing(reason)
        self._record_score_source("missing")
        self._allowed += 1
        return EntryMetaOverlayVerdict(
            allowed=True,
            reason=reason,
            take_entry_prob=1.0,
            block_entry_prob=0.0,
            threshold=self._threshold,
            prediction=None,
            score_source="missing",
        )

    def _record_missing(self, reason: str) -> None:
        self._missing_predictions += 1
        self._missing_by_reason[reason] = self._missing_by_reason.get(reason, 0) + 1
        if reason != "entry_meta_prediction_missing":
            self._dynamic_score_failures += 1

    def _record_score_source(self, score_source: str) -> None:
        self._score_source_counts[score_source] = (
            self._score_source_counts.get(score_source, 0) + 1
        )

    @staticmethod
    def _normalize_dynamic_failure_reason(message: str) -> str:
        normalized = message.lower()
        if "unknown" in normalized:
            return "entry_meta_unknown_category"
        if "missing indicator" in normalized:
            return "entry_meta_feature_missing"
        return "entry_meta_dynamic_score_failed"

    def _record_probability(
        self,
        take_entry_prob: float,
        block_entry_prob: float,
    ) -> None:
        self._matched_predictions += 1
        self._take_entry_prob_sum += take_entry_prob
        self._block_entry_prob_sum += block_entry_prob
        self._take_entry_prob_min = (
            take_entry_prob
            if self._take_entry_prob_min is None
            else min(self._take_entry_prob_min, take_entry_prob)
        )
        self._take_entry_prob_max = (
            take_entry_prob
            if self._take_entry_prob_max is None
            else max(self._take_entry_prob_max, take_entry_prob)
        )
        self._block_entry_prob_min = (
            block_entry_prob
            if self._block_entry_prob_min is None
            else min(self._block_entry_prob_min, block_entry_prob)
        )
        self._block_entry_prob_max = (
            block_entry_prob
            if self._block_entry_prob_max is None
            else max(self._block_entry_prob_max, block_entry_prob)
        )
        if take_entry_prob < self._threshold:
            self._take_entry_prob_below_threshold += 1
        else:
            self._take_entry_prob_at_or_above_threshold += 1

    def _probability_summary(self) -> dict[str, float | int | None]:
        matched = self._matched_predictions
        take_avg = self._take_entry_prob_sum / matched if matched else None
        block_avg = self._block_entry_prob_sum / matched if matched else None
        return {
            "matched_predictions": matched,
            "take_entry_prob_min": self._take_entry_prob_min,
            "take_entry_prob_max": self._take_entry_prob_max,
            "take_entry_prob_avg": take_avg,
            "block_entry_prob_min": self._block_entry_prob_min,
            "block_entry_prob_max": self._block_entry_prob_max,
            "block_entry_prob_avg": block_avg,
            "take_entry_prob_below_threshold": self._take_entry_prob_below_threshold,
            "take_entry_prob_at_or_above_threshold": (
                self._take_entry_prob_at_or_above_threshold
            ),
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
