from __future__ import annotations

import numpy as np
import pytest

from src.research.entry_meta.scoring import EntryMetaScorer, EntryMetaScoringError


def _payload() -> dict[str, object]:
    return {
        "estimator": "logistic_regression_v1",
        "feature_order": ["entry.confidence", "entry.strategy_code"],
        "classes": [0, 1],
        "coef": [[2.0, -1.0]],
        "intercept": [0.0],
        "normalization": {"mean": [0.5, 0.0], "scale": [0.5, 1.0]},
        "prediction_reuse": "dynamic_scorer",
    }


def test_logistic_scorer_outputs_block_and_take_probabilities() -> None:
    scorer = EntryMetaScorer.from_payload(
        _payload(),
        feature_keys=["entry.confidence", "entry.strategy_code"],
    )

    score = scorer.score(np.asarray([1.0, 0.0], dtype=float))

    assert score.take_entry_prob == pytest.approx(1.0 / (1.0 + np.exp(-2.0)))
    assert score.block_entry_prob == pytest.approx(1.0 - score.take_entry_prob)
    assert score.score_source == "dynamic_scorer"


def test_scorer_rejects_feature_order_mismatch() -> None:
    with pytest.raises(EntryMetaScoringError, match="feature_order"):
        EntryMetaScorer.from_payload(_payload(), feature_keys=["entry.confidence"])


def test_scorer_rejects_invalid_probability_dimensions() -> None:
    payload = {**_payload(), "coef": [[1.0, 2.0, 3.0]]}

    with pytest.raises(EntryMetaScoringError, match="coef"):
        EntryMetaScorer.from_payload(
            payload,
            feature_keys=["entry.confidence", "entry.strategy_code"],
        )


@pytest.mark.parametrize("bad_classes", [None, [1, 0], [1]])
def test_scorer_rejects_bad_logistic_classes(bad_classes: object) -> None:
    payload = _payload()
    if bad_classes is None:
        payload.pop("classes")
    else:
        payload["classes"] = bad_classes

    with pytest.raises(EntryMetaScoringError, match="classes"):
        EntryMetaScorer.from_payload(
            payload,
            feature_keys=["entry.confidence", "entry.strategy_code"],
        )


def test_constant_prior_scorer_outputs_fixed_probability() -> None:
    scorer = EntryMetaScorer.from_payload(
        {
            "estimator": "constant_prior",
            "class_probs": {"block_entry": 0.25, "take_entry": 0.75},
            "prediction_reuse": "constant_prior",
        },
        feature_keys=["entry.confidence"],
    )

    score = scorer.score(np.asarray([99.0], dtype=float))

    assert score.block_entry_prob == pytest.approx(0.25)
    assert score.take_entry_prob == pytest.approx(0.75)
    assert score.score_source == "constant_prior"
