from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


class EntryMetaScoringError(ValueError):
    """Raised when an Entry Meta scorer payload or score input is invalid."""


@dataclass(frozen=True)
class EntryMetaScore:
    take_entry_prob: float
    block_entry_prob: float
    score_source: str


class EntryMetaScorer:
    def __init__(
        self,
        *,
        estimator: str,
        feature_order: list[str],
        coef: np.ndarray | None = None,
        intercept: float = 0.0,
        mean: np.ndarray | None = None,
        scale: np.ndarray | None = None,
        class_probs: dict[str, float] | None = None,
        score_source: str,
    ) -> None:
        self._estimator = estimator
        self._feature_order = list(feature_order)
        self._coef = coef
        self._intercept = float(intercept)
        self._mean = mean
        self._scale = scale
        self._class_probs = class_probs or {}
        self._score_source = score_source

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        feature_keys: list[str],
    ) -> EntryMetaScorer:
        if not isinstance(payload, dict):
            raise EntryMetaScoringError("entry meta scorer payload must be an object")

        artifact_feature_keys = [str(key) for key in feature_keys]
        estimator = str(payload.get("estimator", ""))
        if estimator == "constant_prior":
            probs = payload.get("class_probs")
            if not isinstance(probs, dict):
                raise EntryMetaScoringError(
                    "constant_prior class_probs must be an object"
                )
            block = _probability(probs.get("block_entry"), "block_entry")
            take = _probability(probs.get("take_entry"), "take_entry")
            _validate_prob_pair(block, take)
            return cls(
                estimator=estimator,
                feature_order=artifact_feature_keys,
                class_probs={"block_entry": block, "take_entry": take},
                score_source="constant_prior",
            )

        if estimator != "logistic_regression_v1":
            raise EntryMetaScoringError(
                f"unsupported entry meta scorer estimator {estimator}"
            )

        classes = payload.get("classes")
        if (
            not isinstance(classes, list)
            or classes != [0, 1]
            or any(type(item) is not int for item in classes)
        ):
            raise EntryMetaScoringError("entry meta scorer classes must be [0, 1]")

        feature_order_raw = payload.get("feature_order")
        if not isinstance(feature_order_raw, list):
            raise EntryMetaScoringError(
                "entry meta scorer feature_order must be a list"
            )
        feature_order = [str(item) for item in feature_order_raw]
        if feature_order != artifact_feature_keys:
            raise EntryMetaScoringError(
                "entry meta scorer feature_order must match artifact feature_keys"
            )

        n_features = len(feature_order)
        coef = _float_array(payload.get("coef"), "coef")
        if coef.shape != (1, n_features):
            raise EntryMetaScoringError(
                f"entry meta scorer coef shape {coef.shape} must be (1, {n_features})"
            )

        intercept_values = _float_array(payload.get("intercept"), "intercept")
        if intercept_values.shape != (1,):
            raise EntryMetaScoringError(
                "entry meta scorer intercept must contain one value"
            )

        normalization = payload.get("normalization")
        if not isinstance(normalization, dict):
            raise EntryMetaScoringError(
                "entry meta scorer normalization must be an object"
            )
        mean = _float_array(normalization.get("mean"), "normalization.mean")
        scale = _float_array(normalization.get("scale"), "normalization.scale")
        if mean.shape != (n_features,) or scale.shape != (n_features,):
            raise EntryMetaScoringError(
                "entry meta scorer normalization must match feature_order"
            )
        if np.any(scale == 0.0):
            raise EntryMetaScoringError(
                "entry meta scorer scale values must be non-zero"
            )

        return cls(
            estimator=estimator,
            feature_order=feature_order,
            coef=coef[0],
            intercept=float(intercept_values[0]),
            mean=mean,
            scale=scale,
            score_source="dynamic_scorer",
        )

    def score(self, row: np.ndarray) -> EntryMetaScore:
        values = _float_array(row, "row")
        if values.shape != (len(self._feature_order),):
            raise EntryMetaScoringError(
                f"entry meta score row shape {values.shape} must be ({len(self._feature_order)},)"
            )

        if self._estimator == "constant_prior":
            block = float(self._class_probs["block_entry"])
            take = float(self._class_probs["take_entry"])
            return EntryMetaScore(
                take_entry_prob=take,
                block_entry_prob=block,
                score_source=self._score_source,
            )

        if self._coef is None or self._mean is None or self._scale is None:
            raise EntryMetaScoringError(
                "entry meta logistic scorer parameters are incomplete"
            )
        z = (values - self._mean) / self._scale
        logit = float(np.dot(z, self._coef) + self._intercept)
        take = _sigmoid(logit)
        block = 1.0 - take
        _validate_prob_pair(block, take)
        return EntryMetaScore(
            take_entry_prob=take,
            block_entry_prob=block,
            score_source=self._score_source,
        )


def _float_array(value: Any, name: str) -> np.ndarray:
    try:
        result = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise EntryMetaScoringError(
            f"entry meta scorer {name} values must be numeric"
        ) from exc
    if not np.all(np.isfinite(result)):
        raise EntryMetaScoringError(f"entry meta scorer {name} values must be finite")
    return result


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        z = np.exp(-value)
        return float(1.0 / (1.0 + z))
    z = np.exp(value)
    return float(z / (1.0 + z))


def _probability(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EntryMetaScoringError(
            f"entry meta probability {name} must be finite"
        ) from exc
    if not np.isfinite(result) or result < 0.0 or result > 1.0:
        raise EntryMetaScoringError(f"entry meta probability {name} must be in [0, 1]")
    return result


def _validate_prob_pair(block: float, take: float) -> None:
    if not np.isclose(block + take, 1.0, atol=1e-8):
        raise EntryMetaScoringError("entry meta probabilities must sum to 1.0")
