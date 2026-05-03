# Entry Meta Dynamic Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade Entry Meta backtest overlay from prediction lookup only to lookup plus JSON-native dynamic scoring for current signal entries.

**Architecture:** Add a formal `EntryMetaFeatureContext` and single-row feature builder, add a JSON-native `EntryMetaScorer`, serialize scorer payloads during training, and let the overlay use dynamic scoring after prediction lookup misses. The change remains Research + Backtest overlay only and does not touch demo/live runtime or real trading execution.

**Tech Stack:** Python 3.12, NumPy, scikit-learn LogisticRegression/StandardScaler for training only, pytest, existing Entry Meta artifact/overlay/backtest runner contracts.

---

## File Structure

Create:

- `src/research/entry_meta/scoring.py`  
  JSON-native scoring contract for `logistic_regression_v1` and `constant_prior`.
- `tests/research/entry_meta/test_scoring.py`  
  Scorer payload validation and probability tests.

Modify:

- `src/research/entry_meta/features.py`  
  Add `EntryMetaFeatureContext`, `EntryMetaFeatureRow`, `EntryMetaFeatureBuildError`, and `EntryMetaFeatureRowBuilder`.
- `tests/research/entry_meta/test_features.py`  
  Add single-row feature context tests.
- `src/research/entry_meta/training.py`  
  Replace dynamic-capable trained payload from `prediction_reuse=deferred` to `prediction_reuse=dynamic_scorer`; serialize logistic coefficients and normalization.
- `tests/research/entry_meta/test_training.py`  
  Update payload expectations and add scorer roundtrip consistency.
- `src/research/entry_meta/artifacts.py`  
  Tighten `model_payload` object validation and preserve scorer payload.
- `tests/research/entry_meta/test_artifacts.py`  
  Add invalid non-object `model_payload` coverage.
- `src/research/entry_meta/overlay.py`  
  Lookup prediction first; if missing, build a feature row and use `EntryMetaScorer`; record score source and missing reasons.
- `tests/backtesting/test_entry_meta_overlay.py`  
  Add dynamic scorer fallback, dynamic failure allow, and report counter tests.
- `src/backtesting/engine/signals.py`  
  Build `EntryMetaFeatureContext` from public current facts and pass it to overlay.
- `src/backtesting/engine/runner.py`  
  Pass current session string into `process_decision()` from the run loop.
- `docs/research-system.md` and `docs/codebase-review.md`  
  Document the dynamic scoring boundary and residual session/pending-entry risk.

---

### Task 1: Single-Row Feature Context And Builder

**Files:**
- Modify: `src/research/entry_meta/features.py`
- Modify: `tests/research/entry_meta/test_features.py`

- [ ] **Step 1: Add failing single-row feature tests**

Append to `tests/research/entry_meta/test_features.py`:

```python
from src.research.entry_meta.features import (
    EntryMetaFeatureBuildError,
    EntryMetaFeatureContext,
    EntryMetaFeatureRowBuilder,
)


def _context() -> EntryMetaFeatureContext:
    return EntryMetaFeatureContext(
        bar_time=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
        bar_index=1,
        strategy="breakout",
        direction="buy",
        confidence=0.75,
        entry_price=1.2345,
        indicators={"ema": {"value": 2.0}, "rsi": {"value": 50.0}},
        regime="trend",
        session="london",
    )


def test_feature_row_builder_matches_training_feature_order() -> None:
    builder = EntryMetaFeatureRowBuilder(
        feature_keys=[
            "entry.confidence",
            "entry.direction.buy",
            "entry.direction.sell",
            "entry.price",
            "entry.strategy_code",
            "indicator.ema.value",
            "indicator.rsi.value",
            "matrix.regime_code",
            "matrix.session_code",
        ],
        category_mappings={
            "strategy": {"breakout": 0.0, "mean_reversion": 1.0},
            "regime": {"range": 0.0, "trend": 1.0},
            "session": {"asia": 0.0, "london": 1.0},
        },
    )

    row = builder.build(_context())

    assert row.bar_time == "2026-01-01T00:05:00+00:00"
    assert row.strategy == "breakout"
    assert row.direction == "buy"
    assert row.values.shape == (9,)
    np.testing.assert_allclose(
        row.values,
        np.asarray([0.75, 1.0, 0.0, 1.2345, 0.0, 2.0, 50.0, 1.0, 1.0]),
    )


def test_feature_row_builder_rejects_unknown_category_without_implicit_code() -> None:
    builder = EntryMetaFeatureRowBuilder(
        feature_keys=["entry.strategy_code"],
        category_mappings={
            "strategy": {"breakout": 0.0},
            "regime": {"trend": 0.0},
            "session": {"london": 0.0},
        },
    )
    context = EntryMetaFeatureContext(
        bar_time="2026-01-01T00:05:00Z",
        bar_index=1,
        strategy="unknown",
        direction="buy",
        confidence=0.75,
        entry_price=1.2345,
        indicators={},
        regime="trend",
        session="london",
    )

    with pytest.raises(EntryMetaFeatureBuildError, match="unknown strategy category"):
        builder.build(context)


def test_feature_row_builder_rejects_missing_indicator_field() -> None:
    builder = EntryMetaFeatureRowBuilder(
        feature_keys=["indicator.atr14.atr"],
        category_mappings={"strategy": {}, "regime": {}, "session": {}},
    )

    with pytest.raises(EntryMetaFeatureBuildError, match="missing indicator"):
        builder.build(_context())
```

- [ ] **Step 2: Run feature tests and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_features.py
```

Expected: import failure for `EntryMetaFeatureContext` or `EntryMetaFeatureRowBuilder`.

- [ ] **Step 3: Implement single-row feature contracts**

In `src/research/entry_meta/features.py`, add these dataclasses after `EntryMetaFeatureMatrix`:

```python
@dataclass(frozen=True)
class EntryMetaFeatureContext:
    bar_time: Any
    bar_index: int
    strategy: str
    direction: str
    confidence: float
    entry_price: float
    indicators: dict[str, dict[str, Any]]
    regime: str
    session: str


@dataclass(frozen=True)
class EntryMetaFeatureRow:
    values: np.ndarray
    feature_keys: list[str]
    bar_time: str
    strategy: str
    direction: str


class EntryMetaFeatureBuildError(ValueError):
    """Raised when a current entry cannot be converted into artifact features."""
```

Add `EntryMetaFeatureRowBuilder` below `EntryMetaFeatureBuilder`:

```python
class EntryMetaFeatureRowBuilder:
    def __init__(
        self,
        *,
        feature_keys: list[str],
        category_mappings: dict[str, dict[str, float]],
    ) -> None:
        self._feature_keys = [str(key) for key in feature_keys]
        self._category_mappings = {
            category: {str(name): float(code) for name, code in mapping.items()}
            for category, mapping in category_mappings.items()
        }

    def build(self, context: EntryMetaFeatureContext) -> EntryMetaFeatureRow:
        strategy = _required_context_text(context.strategy, "strategy")
        direction = _required_direction({"direction": context.direction}, -1)
        values = [self._value_for_key(key, context, strategy, direction) for key in self._feature_keys]
        return EntryMetaFeatureRow(
            values=np.asarray(values, dtype=float),
            feature_keys=list(self._feature_keys),
            bar_time=_format_bar_time(context.bar_time),
            strategy=strategy,
            direction=direction,
        )

    def _value_for_key(
        self,
        key: str,
        context: EntryMetaFeatureContext,
        strategy: str,
        direction: str,
    ) -> float:
        if key == "entry.confidence":
            return _finite_context_float(context.confidence, "confidence")
        if key == "entry.direction.buy":
            return 1.0 if direction == "buy" else 0.0
        if key == "entry.direction.sell":
            return 1.0 if direction == "sell" else 0.0
        if key == "entry.price":
            return _positive_context_float(context.entry_price, "entry_price")
        if key == "entry.strategy_code":
            return self._category_code("strategy", strategy)
        if key == "matrix.regime_code":
            return self._category_code("regime", _semantic_name(context.regime))
        if key == "matrix.session_code":
            return self._category_code("session", _semantic_name(context.session))
        if key.startswith("indicator."):
            return self._indicator_value(key, context.indicators)
        raise EntryMetaFeatureBuildError(f"unsupported entry meta feature key {key}")

    def _category_code(self, category: str, name: str) -> float:
        mapping = self._category_mappings.get(category, {})
        key = str(name)
        if key not in mapping:
            raise EntryMetaFeatureBuildError(f"unknown {category} category {key}")
        return float(mapping[key])

    def _indicator_value(self, key: str, indicators: dict[str, dict[str, Any]]) -> float:
        parts = key.split(".", 2)
        if len(parts) != 3:
            raise EntryMetaFeatureBuildError(f"unsupported indicator feature key {key}")
        _, indicator, field = parts
        payload = indicators.get(indicator)
        if not isinstance(payload, dict) or field not in payload:
            raise EntryMetaFeatureBuildError(f"missing indicator {indicator}.{field}")
        return _to_float(payload[field])
```

Add helper functions near existing validation helpers:

```python
def _required_context_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EntryMetaFeatureBuildError(f"context field {field} must be a non-empty string")
    return value.strip()


def _finite_context_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EntryMetaFeatureBuildError(f"context field {field} must be finite") from exc
    if not math.isfinite(result):
        raise EntryMetaFeatureBuildError(f"context field {field} must be finite")
    return result


def _positive_context_float(value: Any, field: str) -> float:
    result = _finite_context_float(value, field)
    if result <= 0.0:
        raise EntryMetaFeatureBuildError(f"context field {field} must be > 0")
    return result
```

- [ ] **Step 4: Run feature tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_features.py
```

Expected: all feature tests pass.

- [ ] **Step 5: Commit Task 1**

Run:

```powershell
git add src/research/entry_meta/features.py tests/research/entry_meta/test_features.py
git commit -m "feat: add entry meta feature row builder"
```

---

### Task 2: JSON-Native Entry Meta Scorer

**Files:**
- Create: `src/research/entry_meta/scoring.py`
- Create: `tests/research/entry_meta/test_scoring.py`

- [ ] **Step 1: Write failing scorer tests**

Create `tests/research/entry_meta/test_scoring.py`:

```python
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
```

- [ ] **Step 2: Run scorer tests and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_scoring.py
```

Expected: import failure for `src.research.entry_meta.scoring`.

- [ ] **Step 3: Implement scorer**

Create `src/research/entry_meta/scoring.py`:

```python
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
    ) -> None:
        self._estimator = estimator
        self._feature_order = list(feature_order)
        self._coef = coef
        self._intercept = float(intercept)
        self._mean = mean
        self._scale = scale
        self._class_probs = class_probs or {}

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        feature_keys: list[str],
    ) -> "EntryMetaScorer":
        if not isinstance(payload, dict):
            raise EntryMetaScoringError("entry meta scorer payload must be an object")
        estimator = str(payload.get("estimator", ""))
        if estimator == "constant_prior":
            probs = payload.get("class_probs", {})
            if not isinstance(probs, dict):
                raise EntryMetaScoringError("constant_prior class_probs must be an object")
            block = _probability(probs.get("block_entry"), "block_entry")
            take = _probability(probs.get("take_entry"), "take_entry")
            _validate_prob_pair(block, take)
            return cls(
                estimator=estimator,
                feature_order=list(feature_keys),
                class_probs={"block_entry": block, "take_entry": take},
            )
        if estimator != "logistic_regression_v1":
            raise EntryMetaScoringError(f"unsupported entry meta scorer estimator {estimator}")

        feature_order = [str(item) for item in payload.get("feature_order", [])]
        if feature_order != list(feature_keys):
            raise EntryMetaScoringError("entry meta scorer feature_order must match artifact feature_keys")
        coef = np.asarray(payload.get("coef"), dtype=float)
        if coef.shape != (1, len(feature_order)):
            raise EntryMetaScoringError(
                f"entry meta scorer coef shape {coef.shape} must be (1, {len(feature_order)})"
            )
        intercept_values = np.asarray(payload.get("intercept"), dtype=float)
        if intercept_values.shape != (1,):
            raise EntryMetaScoringError("entry meta scorer intercept must contain one value")
        normalization = payload.get("normalization", {})
        if not isinstance(normalization, dict):
            raise EntryMetaScoringError("entry meta scorer normalization must be an object")
        mean = np.asarray(normalization.get("mean"), dtype=float)
        scale = np.asarray(normalization.get("scale"), dtype=float)
        if mean.shape != (len(feature_order),) or scale.shape != (len(feature_order),):
            raise EntryMetaScoringError("entry meta scorer normalization must match feature_order")
        if not np.all(np.isfinite(coef)) or not np.all(np.isfinite(mean)) or not np.all(np.isfinite(scale)):
            raise EntryMetaScoringError("entry meta scorer parameters must be finite")
        if np.any(scale == 0.0):
            raise EntryMetaScoringError("entry meta scorer scale values must be non-zero")
        return cls(
            estimator=estimator,
            feature_order=feature_order,
            coef=coef[0],
            intercept=float(intercept_values[0]),
            mean=mean,
            scale=scale,
        )

    def score(self, row: np.ndarray) -> EntryMetaScore:
        values = np.asarray(row, dtype=float)
        if values.shape != (len(self._feature_order),):
            raise EntryMetaScoringError(
                f"entry meta score row shape {values.shape} must be ({len(self._feature_order)},)"
            )
        if not np.all(np.isfinite(values)):
            raise EntryMetaScoringError("entry meta score row values must be finite")
        if self._estimator == "constant_prior":
            block = float(self._class_probs["block_entry"])
            take = float(self._class_probs["take_entry"])
            return EntryMetaScore(take_entry_prob=take, block_entry_prob=block, score_source="constant_prior")
        assert self._coef is not None and self._mean is not None and self._scale is not None
        z = (values - self._mean) / self._scale
        logit = float(np.dot(z, self._coef) + self._intercept)
        take = _sigmoid(logit)
        block = 1.0 - take
        _validate_prob_pair(block, take)
        return EntryMetaScore(take_entry_prob=take, block_entry_prob=block, score_source="dynamic_scorer")


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = np.exp(-value)
        return float(1.0 / (1.0 + z))
    z = np.exp(value)
    return float(z / (1.0 + z))


def _probability(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EntryMetaScoringError(f"entry meta probability {name} must be finite") from exc
    if not np.isfinite(result) or result < 0.0 or result > 1.0:
        raise EntryMetaScoringError(f"entry meta probability {name} must be in [0, 1]")
    return result


def _validate_prob_pair(block: float, take: float) -> None:
    if not np.isclose(block + take, 1.0, atol=1e-8):
        raise EntryMetaScoringError("entry meta probabilities must sum to 1.0")
```

- [ ] **Step 4: Run scorer tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_scoring.py
```

Expected: all scorer tests pass.

- [ ] **Step 5: Commit Task 2**

Run:

```powershell
git add src/research/entry_meta/scoring.py tests/research/entry_meta/test_scoring.py
git commit -m "feat: add entry meta json scorer"
```

---

### Task 3: Training Emits Dynamic Scorer Payload

**Files:**
- Modify: `src/research/entry_meta/training.py`
- Modify: `src/research/entry_meta/artifacts.py`
- Modify: `tests/research/entry_meta/test_training.py`
- Modify: `tests/research/entry_meta/test_artifacts.py`

- [ ] **Step 1: Add failing training payload tests**

In `tests/research/entry_meta/test_training.py`, update `test_cpu_training_outputs_artifact_probabilities_aligned_to_take_and_block`:

```python
    assert bundle.artifact.model_payload["estimator"] == "logistic_regression_v1"
    assert bundle.artifact.model_payload["prediction_reuse"] == "dynamic_scorer"
    assert bundle.artifact.model_payload["feature_order"] == bundle.artifact.feature_keys
    assert "coef" in bundle.artifact.model_payload
    assert "intercept" in bundle.artifact.model_payload
    assert "normalization" in bundle.artifact.model_payload
```

Append this test:

```python
def test_training_dynamic_scorer_reproduces_saved_predictions() -> None:
    from src.research.entry_meta.scoring import EntryMetaScorer

    matrix = _matrix()
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0, 9.0, -4.0, 6.0, -2.0])

    bundle = train_entry_meta_bundle(
        matrix,
        dataset,
        "cpu",
        min_samples=8,
        min_oos_samples=4,
        min_class_samples=4,
    )
    scorer = EntryMetaScorer.from_payload(
        bundle.artifact.model_payload,
        feature_keys=bundle.artifact.feature_keys,
    )

    for row, prediction in zip(bundle.features.rows, bundle.artifact.predictions):
        score = scorer.score(row)
        assert score.take_entry_prob == pytest.approx(prediction.take_entry_prob)
        assert score.block_entry_prob == pytest.approx(prediction.block_entry_prob)
```

In `tests/research/entry_meta/test_artifacts.py`, append:

```python
def test_from_dict_rejects_non_object_model_payload() -> None:
    payload = {
        "model_id": "entry-meta-H1-test",
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "backend": "cpu",
        "model_kind": "tabular",
        "feature_keys": [],
        "label_summary": {"take_entry": 1, "block_entry": 0},
        "sample_weight_summary": {"min": 1.0, "max": 1.0, "mean": 1.0},
        "metrics": {},
        "predictions": [],
        "model_payload": ["bad"],
        "feature_manifest": {},
    }

    with pytest.raises(ValueError, match="model_payload"):
        EntryMetaArtifact.from_dict(payload)
```

- [ ] **Step 2: Run targeted tests and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_training.py tests\research\entry_meta\test_artifacts.py
```

Expected: training payload assertions fail because current payload is not dynamic scorer.

- [ ] **Step 3: Implement training payload serialization**

In `src/research/entry_meta/training.py`:

1. Import `StandardScaler`, `LogisticRegression`, and `EntryMetaScorer`.
2. Change `_fit_predict_probabilities(...)` to return logistic scorer payload for valid two-class training data:

```python
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

scaler = StandardScaler()
train_rows = rows[train_indices]
scaled_train_rows = scaler.fit_transform(train_rows)
safe_scale = np.asarray(scaler.scale_, dtype=float)
safe_scale[~np.isfinite(safe_scale) | (safe_scale == 0.0)] = 1.0
safe_mean = np.asarray(scaler.mean_, dtype=float)
safe_mean[~np.isfinite(safe_mean)] = 0.0
scaled_rows = (rows - safe_mean) / safe_scale

estimator = LogisticRegression(random_state=0, max_iter=1000)
estimator.fit(scaled_train_rows, train_labels, sample_weight=sample_weights[train_indices])
raw_probabilities = estimator.predict_proba(scaled_rows)
probabilities = _align_probabilities(raw_probabilities, estimator.classes_, len(labels))
model_payload = {
    "estimator": "logistic_regression_v1",
    "feature_order": [],
    "classes": [int(item) for item in estimator.classes_],
    "coef": estimator.coef_.astype(float).tolist(),
    "intercept": estimator.intercept_.astype(float).tolist(),
    "normalization": {"mean": safe_mean.tolist(), "scale": safe_scale.tolist()},
    "prediction_reuse": "dynamic_scorer",
    "train_samples": int(len(train_indices)),
}
```

3. After `_fit_predict_probabilities(...)`, set feature order before artifact creation:

```python
model_payload["feature_order"] = list(features.feature_keys)
```

4. Remove or avoid overriding trained payload with `model_payload["prediction_reuse"] = "deferred"`.
5. For constant prior, set:

```python
"prediction_reuse": "constant_prior"
```

6. Use `EntryMetaScorer.from_payload(...).score(row)` to recompute predictions from the serialized payload before building `EntryMetaPrediction`.

In `src/research/entry_meta/artifacts.py`, before `model_payload=dict(data["model_payload"])`, validate:

```python
        if not isinstance(data["model_payload"], dict):
            raise ValueError("entry meta artifact model_payload must be an object")
```

- [ ] **Step 4: Run targeted tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_training.py tests\research\entry_meta\test_artifacts.py tests\research\entry_meta\test_scoring.py
```

Expected: all targeted tests pass.

- [ ] **Step 5: Commit Task 3**

Run:

```powershell
git add src/research/entry_meta/training.py src/research/entry_meta/artifacts.py tests/research/entry_meta/test_training.py tests/research/entry_meta/test_artifacts.py
git commit -m "feat: serialize entry meta dynamic scorer"
```

---

### Task 4: Overlay Dynamic Scoring And Coverage Report

**Files:**
- Modify: `src/research/entry_meta/overlay.py`
- Modify: `tests/backtesting/test_entry_meta_overlay.py`

- [ ] **Step 1: Add failing overlay dynamic tests**

In `tests/backtesting/test_entry_meta_overlay.py`, add a dynamic artifact helper:

```python
def _dynamic_artifact(*, status: str = "accepted") -> EntryMetaArtifact:
    return EntryMetaArtifact(
        model_id="entry-meta-dynamic",
        symbol="XAUUSD",
        timeframe="H1",
        backend="cpu",
        model_kind="tabular",
        feature_keys=[
            "entry.confidence",
            "entry.direction.buy",
            "entry.direction.sell",
            "entry.price",
            "entry.strategy_code",
            "indicator.atr14.atr",
            "matrix.regime_code",
            "matrix.session_code",
        ],
        label_summary={"take_entry": 2, "block_entry": 2},
        sample_weight_summary={},
        metrics={},
        predictions=[],
        model_payload={
            "estimator": "logistic_regression_v1",
            "feature_order": [
                "entry.confidence",
                "entry.direction.buy",
                "entry.direction.sell",
                "entry.price",
                "entry.strategy_code",
                "indicator.atr14.atr",
                "matrix.regime_code",
                "matrix.session_code",
            ],
            "classes": [0, 1],
            "coef": [[-10.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]],
            "intercept": [5.0],
            "normalization": {
                "mean": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
            },
            "prediction_reuse": "dynamic_scorer",
        },
        feature_manifest={
            "category_mappings": {
                "strategy": {"breakout": 0.0},
                "regime": {"trend": 0.0},
                "session": {"london": 0.0},
            }
        },
        status=status,
    )


def _feature_context(confidence: float = 0.9):
    from src.research.entry_meta.features import EntryMetaFeatureContext

    return EntryMetaFeatureContext(
        bar_time=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
        bar_index=3,
        strategy="breakout",
        direction="buy",
        confidence=confidence,
        entry_price=1.25,
        indicators={"atr14": {"atr": 0.01}},
        regime="trend",
        session="london",
    )
```

Append tests:

```python
def test_overlay_uses_dynamic_scorer_when_prediction_lookup_misses() -> None:
    overlay = EntryMetaBacktestOverlay(_dynamic_artifact(), mode="filter", threshold=0.50)

    verdict = overlay.evaluate(
        "2026-01-01T03:00:00Z",
        "breakout",
        "buy",
        confidence=0.9,
        feature_context=_feature_context(confidence=0.9),
    )

    assert verdict.allowed is False
    assert verdict.reason == "entry_meta_probability_below_threshold"
    assert verdict.prediction is None
    assert verdict.score_source == "dynamic_scorer"
    report = overlay.report()
    assert report["score_source_counts"]["dynamic_scorer"] == 1
    assert report["dynamic_scored"] == 1
    assert report["missing_predictions"] == 0


def test_overlay_dynamic_feature_failure_allows_and_records_reason() -> None:
    overlay = EntryMetaBacktestOverlay(_dynamic_artifact(), mode="filter", threshold=0.50)
    bad_context = _feature_context(confidence=0.9)
    bad_context = dataclasses.replace(bad_context, strategy="unknown")

    verdict = overlay.evaluate(
        "2026-01-01T03:00:00Z",
        "unknown",
        "buy",
        confidence=0.9,
        feature_context=bad_context,
    )

    assert verdict.allowed is True
    assert verdict.reason == "entry_meta_unknown_category"
    assert verdict.score_source == "missing"
    report = overlay.report()
    assert report["missing_by_reason"]["entry_meta_unknown_category"] == 1
    assert report["dynamic_score_failures"] == 1
```

Add `import dataclasses` at the top of the test file.

- [ ] **Step 2: Run overlay tests and verify RED**

Run:

```powershell
python -m pytest -q tests\backtesting\test_entry_meta_overlay.py
```

Expected: `evaluate()` does not accept `feature_context` or verdict lacks `score_source`.

- [ ] **Step 3: Implement overlay dynamic path**

In `src/research/entry_meta/overlay.py`:

1. Import:

```python
from src.research.entry_meta.features import (
    EntryMetaFeatureBuildError,
    EntryMetaFeatureContext,
    EntryMetaFeatureRowBuilder,
)
from src.research.entry_meta.scoring import EntryMetaScorer, EntryMetaScoringError
```

2. Add `score_source: str = "artifact_prediction"` to `EntryMetaOverlayVerdict`.
3. In `__init__`, build scorer when possible:

```python
        self._feature_row_builder: EntryMetaFeatureRowBuilder | None = None
        self._scorer: EntryMetaScorer | None = None
        try:
            category_mappings = dict(artifact.feature_manifest.get("category_mappings", {}))
            self._feature_row_builder = EntryMetaFeatureRowBuilder(
                feature_keys=list(artifact.feature_keys),
                category_mappings=category_mappings,
            )
            self._scorer = EntryMetaScorer.from_payload(
                artifact.model_payload,
                feature_keys=list(artifact.feature_keys),
            )
        except (EntryMetaFeatureBuildError, EntryMetaScoringError, TypeError, ValueError):
            self._feature_row_builder = None
            self._scorer = None
```

4. Extend `reset()` with:

```python
        self._score_source_counts: dict[str, int] = {}
        self._missing_by_reason: dict[str, int] = {}
        self._dynamic_scored = 0
        self._dynamic_score_failures = 0
```

5. Change `evaluate(...)` signature:

```python
        feature_context: EntryMetaFeatureContext | None = None,
```

6. On lookup hit, call `_record_score_source("artifact_prediction")`.
7. On lookup miss:

```python
        dynamic = self._score_dynamic(feature_context)
        if isinstance(dynamic, str):
            return self._allow_missing(dynamic)
        take_entry_prob, block_entry_prob, score_source = dynamic
```

8. Implement `_score_dynamic`, `_allow_missing`, `_record_missing`, `_record_score_source`, and `_normalize_dynamic_failure_reason`:

```python
    def _score_dynamic(
        self,
        feature_context: EntryMetaFeatureContext | None,
    ) -> tuple[float, float, str] | str:
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
```

`_allow_missing(reason)` must call `_record_missing(reason)`, increment `_allowed`,
and return an allowed verdict with `score_source="missing"` and `prediction=None`.
`_record_missing(reason)` must increment `_missing_predictions`,
`_missing_by_reason[reason]`, and `_dynamic_score_failures` for dynamic reasons other
than `entry_meta_prediction_missing`.

Map error messages containing `"unknown"` to `entry_meta_unknown_category`, containing `"missing indicator"` to `entry_meta_feature_missing`, otherwise `entry_meta_dynamic_score_failed`.

9. `report()` adds:

```python
            "score_source_counts": dict(sorted(self._score_source_counts.items())),
            "missing_by_reason": dict(sorted(self._missing_by_reason.items())),
            "dynamic_scored": self._dynamic_scored,
            "dynamic_score_failures": self._dynamic_score_failures,
```

- [ ] **Step 4: Run overlay tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests\backtesting\test_entry_meta_overlay.py
```

Expected: all overlay tests pass.

- [ ] **Step 5: Commit Task 4**

Run:

```powershell
git add src/research/entry_meta/overlay.py tests/backtesting/test_entry_meta_overlay.py
git commit -m "feat: add entry meta overlay dynamic scoring"
```

---

### Task 5: Backtest Process Decision Passes Feature Context

**Files:**
- Modify: `src/backtesting/engine/signals.py`
- Modify: `src/backtesting/engine/runner.py`
- Modify: `tests/backtesting/test_entry_meta_overlay.py`

- [ ] **Step 1: Add failing process_decision context test**

Append to `tests/backtesting/test_entry_meta_overlay.py`:

```python
def test_process_decision_passes_entry_meta_feature_context() -> None:
    captured: list[object] = []
    entry_meta_overlay = _EntryMetaOverlayCapturingContext(captured)
    engine = _ProcessDecisionEngine(
        state_edge_overlay=None,
        entry_meta_overlay=entry_meta_overlay,
        rejections=[],
        blocked_events=[],
    )

    process_decision(
        engine,
        _decision(),
        _bar(),
        1,
        {"atr14": {"atr": 0.5}},
        "trend",
        entry_session="london",
    )

    context = captured[0]
    assert context.bar_time == _bar().time
    assert context.bar_index == 1
    assert context.strategy == "breakout"
    assert context.direction == "buy"
    assert context.confidence == 0.9
    assert context.entry_price == 1.0
    assert context.indicators == {"atr14": {"atr": 0.5}}
    assert context.regime == "trend"
    assert context.session == "london"


class _EntryMetaOverlayCapturingContext:
    def __init__(self, captured: list[object]) -> None:
        self._captured = captured

    def evaluate(
        self,
        bar_time: datetime,
        strategy: str,
        direction: str,
        *,
        confidence: float = 0.0,
        feature_context=None,
    ) -> SimpleNamespace:
        del bar_time, strategy, direction, confidence
        self._captured.append(feature_context)
        return SimpleNamespace(
            allowed=True,
            reason="allowed",
            take_entry_prob=0.9,
            block_entry_prob=0.1,
            threshold=0.65,
            prediction=None,
        )
```

- [ ] **Step 2: Run test and verify RED**

Run:

```powershell
python -m pytest -q tests\backtesting\test_entry_meta_overlay.py::test_process_decision_passes_entry_meta_feature_context
```

Expected: `process_decision()` does not accept `entry_session`.

- [ ] **Step 3: Implement context passing**

In `src/backtesting/engine/signals.py`:

1. Import `EntryMetaFeatureContext` under normal import or `TYPE_CHECKING` safe import:

```python
from src.research.entry_meta.features import EntryMetaFeatureContext
```

2. Add helper:

```python
def _entry_meta_feature_context(
    decision: SignalDecision,
    bar: OHLC,
    bar_index: int,
    indicators: Dict[str, Dict[str, Any]],
    regime: RegimeType | str,
    entry_session: str,
) -> EntryMetaFeatureContext:
    return EntryMetaFeatureContext(
        bar_time=bar.time,
        bar_index=int(bar_index),
        strategy=str(decision.strategy),
        direction=str(decision.direction),
        confidence=float(decision.confidence),
        entry_price=float(bar.close),
        indicators=dict(indicators),
        regime=str(getattr(regime, "value", regime)),
        session=str(entry_session or "unknown"),
    )
```

3. Change `process_decision(...)` signature:

```python
    entry_session: str = "unknown",
) -> None:
```

4. Pass context:

```python
        verdict = entry_meta_overlay.evaluate(
            bar.time,
            decision.strategy,
            decision.direction,
            confidence=decision.confidence,
            feature_context=_entry_meta_feature_context(
                decision,
                bar,
                bar_index,
                indicators,
                regime,
                entry_session,
            ),
        )
```

In `src/backtesting/engine/runner.py`, before `_process_decision_helper(...)`, compute:

```python
                entry_session = "unknown"
                if self._session_filter is not None:
                    sessions = self._session_filter.current_sessions(bar.time)
                    if sessions:
                        entry_session = str(sessions[0])
```

Then call:

```python
                _process_decision_helper(
                    self,
                    decision,
                    bar,
                    bar_index,
                    indicators,
                    regime,
                    entry_session=entry_session,
                )
```

- [ ] **Step 4: Run backtest overlay tests**

Run:

```powershell
python -m pytest -q tests\backtesting\test_entry_meta_overlay.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 5**

Run:

```powershell
git add src/backtesting/engine/signals.py src/backtesting/engine/runner.py tests/backtesting/test_entry_meta_overlay.py
git commit -m "feat: pass entry meta feature context in backtests"
```

---

### Task 6: Documentation And Final Verification

**Files:**
- Modify: `docs/research-system.md`
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Update research documentation**

In `docs/research-system.md`, under `#### Entry Meta-Label Overlay Lab（2026-05-03）`, add:

```markdown
动态打分端口（2026-05-03）：

- 新版 Entry Meta artifact 会保存 JSON-native `logistic_regression_v1` scorer payload；overlay 先查历史 prediction，未命中时用当前 signal entry 的 feature context 动态计算 `take_entry_prob / block_entry_prob`。
- `feature_context` 只来自 backtest 当前已公开事实：bar time/index、strategy、direction、confidence、bar close、当前 indicators、hard regime、session；不读取 engine/portfolio/model 私有字段。
- 动态打分失败不会阻断交易，会以 `missing_by_reason` 和 `score_source_counts` 进入 overlay report。
- 该能力仍只属于 Research + Backtest overlay，不接入 demo/live runtime。
```

In `docs/codebase-review.md`, add one bullet under the 2026-05-03 Entry Meta section:

```markdown
- 2026-05-03：Entry Meta overlay 增加动态打分端口。职责边界：training 产出 JSON-native scorer payload，feature row builder 负责当前 entry 特征同构，overlay 只做 lookup/dynamic score/filter/report。动态失败默认放行并记录，不进入 demo/live。
```

- [ ] **Step 2: Run full focused verification**

Run:

```powershell
python -m pytest -q tests\research\entry_meta tests\backtesting\test_entry_meta_overlay.py tests\backtesting\test_entry_meta_backtest_runner_cli.py tests\research\core\test_config.py
```

Expected: all Entry Meta focused tests pass.

- [ ] **Step 3: Run State Edge regression**

Run:

```powershell
python -m pytest -q tests\research\state_edge tests\backtesting\test_state_edge_overlay.py
```

Expected: all State Edge tests pass. If pre-existing untracked State Edge files are still present, do not stage them unless they are intentionally part of this task.

- [ ] **Step 4: Run CLI smoke**

Run:

```powershell
python -m src.ops.cli.entry_meta_lab --help
python -m src.ops.cli.entry_meta_overlay_report --help
python -m src.ops.cli.backtest_runner --help
git diff --check
```

Expected: CLI commands exit 0; `git diff --check` has no output.

- [ ] **Step 5: Optional H1 smoke**

If a baseline JSON with `raw_results.trades` is available, run:

```powershell
python -m src.ops.cli.entry_meta_lab --environment live --baseline artifacts\state_edge_segment_baseline_h1_20260301_0315.json --tf H1 --start 2026-03-01 --end 2026-03-15 --backend cpu --artifact-dir artifacts\entry_meta_h1_dynamic_smoke --json-output artifacts\entry_meta_h1_dynamic_smoke_train.json --no-auto-backfill
```

Expected: JSON contains `result.artifact_path`, `result.metrics`, `result.quality`, and `result.result.model_payload.prediction_reuse` is not required because `model_payload` lives inside the artifact. Inspect the artifact and confirm `model_payload.prediction_reuse` is `dynamic_scorer` or `constant_prior`.

- [ ] **Step 6: Commit documentation**

Run:

```powershell
git add docs/research-system.md docs/codebase-review.md
git commit -m "docs: document entry meta dynamic scoring"
```

If no documentation changes are needed beyond prior tasks, skip this commit and state why.

---

## Self-Review

Spec coverage:

- Dynamic feature context: Task 1 and Task 5.
- JSON-native scorer: Task 2.
- Training serialization: Task 3.
- Lookup → dynamic scorer → missing allow overlay path: Task 4.
- Report coverage counters: Task 4.
- Backtest-only boundary: Task 5 and Task 6.
- Documentation and codebase-review update: Task 6.

Boundary check:

- No task touches `src/trading/`, `src/api/`, or demo/live runtime.
- The only backtest engine change is passing a formal `EntryMetaFeatureContext` into an existing research overlay port.
- Artifact-invalid cases fail fast in scorer construction; single-entry context failures degrade to allow and report counters.

Known residual risk:

- Session context depends on backtest session filter availability. If no session is available, `unknown` can cause dynamic scorer coverage loss unless the artifact mapping contains `unknown`.
- Pending-entry fills are still scored at signal time, not at eventual fill time. That is explicit non-goal for this phase.
