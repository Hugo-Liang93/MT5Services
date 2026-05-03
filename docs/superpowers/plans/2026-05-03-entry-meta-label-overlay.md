# Entry Meta-Label Overlay Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a research-only Entry Meta-Label Overlay Lab that trains `take_entry_prob / block_entry_prob` from baseline backtest trades and validates it through backtest shadow/filter reports with exact blocked-trade attribution.

**Architecture:** Add `src/research/entry_meta/` as a separate research domain from `state_edge`; Entry Meta consumes baseline trade entries plus current/history-visible DataMatrix features, writes an auditable artifact, and is consumed by a backtest-only overlay. Backtest runtime receives the overlay through explicit constructor ports and records blocked events through public methods; demo/live runtime is not touched.

**Tech Stack:** Python 3.12, NumPy, scikit-learn CPU baseline, existing State Edge backend resolver for GPU fail-fast, existing `DataMatrix`, existing `backtest_runner`, pytest.

---

## File Structure

Create:

- `src/research/entry_meta/__init__.py`
  Public package marker and short module docstring.
- `src/research/entry_meta/labels.py`
  Converts baseline trade outcome into `take_entry` labels and sample weights.
- `src/research/entry_meta/artifacts.py`
  Dataclasses for `EntryMetaArtifact` and `EntryMetaPrediction`; JSON save/load.
- `src/research/entry_meta/features.py`
  Builds leak-guarded entry-time feature rows from `DataMatrix.indicator_series`, regime/session, strategy/direction/confidence, and optional State Edge probabilities.
- `src/research/entry_meta/dataset.py`
  Parses baseline backtest JSON/raw trades and joins entries to DataMatrix bar indices.
- `src/research/entry_meta/training.py`
  CPU/GPU training entry point; CPU baseline first, GPU mode still uses backend fail-fast readiness in phase 1.
- `src/research/entry_meta/quality.py`
  Small quality gate for sample count, class balance, and top probability bucket.
- `src/research/entry_meta/lab.py`
  Orchestrates baseline JSON -> DataMatrix -> FeatureHub -> dataset -> train -> artifact.
- `src/research/entry_meta/overlay.py`
  Backtest-only overlay with `shadow`/`filter` behavior.
- `src/research/entry_meta/evaluation.py`
  Entry Meta overlay report and blocked-trade attribution.
- `src/ops/cli/entry_meta_lab.py`
  Research CLI for dataset -> train -> artifact.
- `src/ops/cli/entry_meta_overlay_report.py`
  Report CLI for baseline/shadow/filter JSON.
- `tests/research/entry_meta/`
  New test package matching the source modules.

Modify:

- `src/backtesting/engine/runner.py`
  Add optional `entry_meta_overlay`, reset/report method, and event storage reuse.
- `src/backtesting/engine/signals.py`
  Evaluate Entry Meta overlay before entry execution using public overlay verdict and `record_blocked_entry`.
- `src/ops/cli/backtest_runner.py`
  Add `--entry-meta-artifact`, `--entry-meta-mode`, `--entry-meta-threshold-grid`, `--entry-meta-directions`.
- `config/research.ini`
  Add `[entry_meta_model]` defaults.
- `src/research/core/config.py` and `tests/research/core/test_config.py`
  Load and validate Entry Meta config.
- `docs/research-system.md` and `docs/codebase-review.md`
  Document Entry Meta responsibilities and first-phase runtime boundary.

---

### Task 1: Labels And Artifact Contracts

**Files:**
- Create: `src/research/entry_meta/__init__.py`
- Create: `src/research/entry_meta/labels.py`
- Create: `src/research/entry_meta/artifacts.py`
- Create: `tests/research/entry_meta/test_labels.py`
- Create: `tests/research/entry_meta/test_artifacts.py`

- [ ] **Step 1: Write failing label tests**

Create `tests/research/entry_meta/test_labels.py`:

```python
from __future__ import annotations

from src.research.entry_meta.labels import EntryMetaLabelBuilder


def test_labels_mark_positive_pnl_as_take_and_non_positive_as_block() -> None:
    trades = [
        {"pnl": 25.0, "pnl_pct": 1.2},
        {"pnl": 0.0, "pnl_pct": 0.0},
        {"pnl": -10.0, "pnl_pct": -0.5},
    ]

    labels = EntryMetaLabelBuilder().build(trades)

    assert labels.labels == [1, 0, 0]
    assert labels.summary == {"take_entry": 1, "block_entry": 2}


def test_sample_weights_protect_large_winners_and_large_losers() -> None:
    trades = [
        {"pnl": 5.0, "pnl_pct": 0.2},
        {"pnl": 100.0, "pnl_pct": 5.0},
        {"pnl": -80.0, "pnl_pct": -4.0},
    ]

    labels = EntryMetaLabelBuilder(max_weight=5.0).build(trades)

    assert labels.sample_weights[0] == 1.05
    assert labels.sample_weights[1] == 2.0
    assert labels.sample_weights[2] == 1.8
```

- [ ] **Step 2: Run label tests and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_labels.py
```

Expected: import failure for `src.research.entry_meta.labels`.

- [ ] **Step 3: Implement labels**

Create `src/research/entry_meta/__init__.py`:

```python
"""Entry Meta-Label research lab.

Research-only modules for deciding whether an existing strategy entry should be
kept or blocked. This package is not imported by demo/live runtime.
"""
```

Create `src/research/entry_meta/labels.py`:

```python
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
```

- [ ] **Step 4: Run label tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_labels.py
```

Expected: `2 passed`.

- [ ] **Step 5: Write failing artifact tests**

Create `tests/research/entry_meta/test_artifacts.py`:

```python
from __future__ import annotations

from pathlib import Path

from src.research.entry_meta.artifacts import (
    EntryMetaArtifact,
    EntryMetaPrediction,
    load_artifact,
    save_artifact,
)


def test_entry_meta_artifact_roundtrip(tmp_path: Path) -> None:
    artifact = EntryMetaArtifact(
        model_id="entry-meta-H1-test",
        symbol="XAUUSD",
        timeframe="H1",
        backend="cpu",
        model_kind="tabular",
        feature_keys=["entry.confidence"],
        label_summary={"take_entry": 1, "block_entry": 1},
        sample_weight_summary={"min": 1.0, "max": 2.0, "mean": 1.5},
        metrics={"oos_accuracy": 0.5},
        predictions=[
            EntryMetaPrediction(
                bar_time="2026-01-01T01:00:00+00:00",
                strategy="s",
                direction="buy",
                take_entry_prob=0.7,
                block_entry_prob=0.3,
            )
        ],
        model_payload={"kind": "constant", "take_entry_prob": 0.7},
        feature_manifest={"source": "test"},
        status="trained",
    )

    path = save_artifact(artifact, tmp_path / "artifact-dir")

    loaded = load_artifact(path)
    assert loaded == artifact
    assert path.name == "entry_meta_artifact.json"
```

- [ ] **Step 6: Run artifact tests and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_artifacts.py
```

Expected: import failure for `src.research.entry_meta.artifacts`.

- [ ] **Step 7: Implement artifacts**

Create `src/research/entry_meta/artifacts.py`:

```python
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EntryMetaPrediction:
    bar_time: str
    strategy: str
    direction: str
    take_entry_prob: float
    block_entry_prob: float
    threshold_context: dict[str, Any] | None = None


@dataclass(frozen=True)
class EntryMetaArtifact:
    model_id: str
    symbol: str
    timeframe: str
    backend: str
    model_kind: str
    feature_keys: list[str]
    label_summary: dict[str, int]
    sample_weight_summary: dict[str, float]
    metrics: dict[str, Any]
    predictions: list[EntryMetaPrediction]
    model_payload: dict[str, Any]
    feature_manifest: dict[str, Any]
    status: str = "trained"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifact_type"] = "entry_meta"
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EntryMetaArtifact":
        predictions = [
            EntryMetaPrediction(**item)
            for item in payload.get("predictions", [])
        ]
        return cls(
            model_id=str(payload["model_id"]),
            symbol=str(payload["symbol"]),
            timeframe=str(payload["timeframe"]),
            backend=str(payload["backend"]),
            model_kind=str(payload["model_kind"]),
            feature_keys=list(payload.get("feature_keys", [])),
            label_summary=dict(payload.get("label_summary", {})),
            sample_weight_summary=dict(payload.get("sample_weight_summary", {})),
            metrics=dict(payload.get("metrics", {})),
            predictions=predictions,
            model_payload=dict(payload.get("model_payload", {})),
            feature_manifest=dict(payload.get("feature_manifest", {})),
            status=str(payload.get("status", "trained")),
        )


def save_artifact(artifact: EntryMetaArtifact, directory: str | Path) -> Path:
    output_dir = Path(directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "entry_meta_artifact.json"
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(artifact.to_dict(), fh, ensure_ascii=False, indent=2)
    tmp_path.replace(path)
    return path


def load_artifact(path: str | Path) -> EntryMetaArtifact:
    raw_path = Path(path)
    if raw_path.is_dir():
        raw_path = raw_path / "entry_meta_artifact.json"
    with raw_path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"expected entry meta artifact object: {raw_path}")
    return EntryMetaArtifact.from_dict(payload)
```

- [ ] **Step 8: Run artifact tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_labels.py tests\research\entry_meta\test_artifacts.py
```

Expected: `3 passed`.

- [ ] **Step 9: Commit Task 1**

Run:

```powershell
git add src/research/entry_meta/__init__.py src/research/entry_meta/labels.py src/research/entry_meta/artifacts.py tests/research/entry_meta/test_labels.py tests/research/entry_meta/test_artifacts.py
git commit -m "feat: add entry meta labels and artifacts"
```

---

### Task 2: Dataset And Leak-Guarded Feature Builder

**Files:**
- Create: `src/research/entry_meta/dataset.py`
- Create: `src/research/entry_meta/features.py`
- Create: `tests/research/entry_meta/test_dataset.py`
- Create: `tests/research/entry_meta/test_features.py`

- [ ] **Step 1: Write failing dataset tests**

Create `tests/research/entry_meta/test_dataset.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.research.entry_meta.dataset import EntryMetaDatasetBuilder


def _matrix() -> SimpleNamespace:
    return SimpleNamespace(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=2,
        bars=[
            SimpleNamespace(time=datetime(2026, 1, 1, 0, tzinfo=timezone.utc)),
            SimpleNamespace(time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc)),
        ],
        train_slice=lambda: range(0, 1),
        test_slice=lambda: range(1, 2),
    )


def test_dataset_builder_matches_raw_trades_to_bar_indices() -> None:
    trades = [
        {
            "entry_time": "2026-01-01T01:00:00+00:00",
            "strategy": "structured_strong_trend_follow",
            "direction": "buy",
            "confidence": 0.7,
            "regime": "trending",
            "entry_price": 2000.0,
            "pnl": 25.0,
            "pnl_pct": 1.2,
        }
    ]

    dataset = EntryMetaDatasetBuilder().build(matrix=_matrix(), raw_trades=trades)

    assert dataset.bar_indices == [1]
    assert dataset.trades[0]["strategy"] == "structured_strong_trend_follow"
    assert dataset.labels.labels == [1]
    assert dataset.train_indices == []
    assert dataset.test_indices == [0]


def test_dataset_builder_records_unmatched_trades() -> None:
    trades = [
        {
            "entry_time": "2026-01-01T03:00:00+00:00",
            "strategy": "s",
            "direction": "buy",
            "pnl": -1.0,
        }
    ]

    dataset = EntryMetaDatasetBuilder().build(matrix=_matrix(), raw_trades=trades)

    assert dataset.trades == []
    assert dataset.unmatched_trades == trades
    assert dataset.label_summary == {"take_entry": 0, "block_entry": 0}
```

- [ ] **Step 2: Run dataset tests and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_dataset.py
```

Expected: import failure for `src.research.entry_meta.dataset`.

- [ ] **Step 3: Implement dataset builder**

Create `src/research/entry_meta/dataset.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.research.entry_meta.labels import EntryMetaLabelBuilder, EntryMetaLabelSet


@dataclass(frozen=True)
class EntryMetaDataset:
    trades: list[dict[str, Any]]
    bar_indices: list[int]
    labels: EntryMetaLabelSet
    train_indices: list[int]
    test_indices: list[int]
    unmatched_trades: list[dict[str, Any]]

    @property
    def label_summary(self) -> dict[str, int]:
        return self.labels.summary


class EntryMetaDatasetBuilder:
    def __init__(self, *, label_builder: EntryMetaLabelBuilder | None = None) -> None:
        self._label_builder = label_builder or EntryMetaLabelBuilder()

    def build(self, *, matrix: Any, raw_trades: list[dict[str, Any]]) -> EntryMetaDataset:
        time_to_index = {
            _normalize_time(getattr(bar, "time", "")): idx
            for idx, bar in enumerate(getattr(matrix, "bars", []))
        }
        matrix_train = set(int(i) for i in matrix.train_slice())
        matrix_test = set(int(i) for i in matrix.test_slice())
        matched_trades: list[dict[str, Any]] = []
        bar_indices: list[int] = []
        train_indices: list[int] = []
        test_indices: list[int] = []
        unmatched_trades: list[dict[str, Any]] = []
        for trade in raw_trades:
            idx = time_to_index.get(_normalize_time(trade.get("entry_time", "")))
            if idx is None:
                unmatched_trades.append(dict(trade))
                continue
            sample_idx = len(matched_trades)
            matched_trades.append(dict(trade))
            bar_indices.append(idx)
            if idx in matrix_train:
                train_indices.append(sample_idx)
            elif idx in matrix_test:
                test_indices.append(sample_idx)
        labels = self._label_builder.build(matched_trades)
        return EntryMetaDataset(
            trades=matched_trades,
            bar_indices=bar_indices,
            labels=labels,
            train_indices=train_indices,
            test_indices=test_indices,
            unmatched_trades=unmatched_trades,
        )


def _normalize_time(value: Any) -> str:
    if isinstance(value, datetime):
        item = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return item.astimezone(timezone.utc).isoformat()
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
```

- [ ] **Step 4: Run dataset tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_dataset.py
```

Expected: `2 passed`.

- [ ] **Step 5: Write failing feature tests**

Create `tests/research/entry_meta/test_features.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.research.entry_meta.dataset import EntryMetaDataset
from src.research.entry_meta.features import EntryMetaFeatureBuilder
from src.research.entry_meta.labels import EntryMetaLabelBuilder


def _matrix() -> SimpleNamespace:
    return SimpleNamespace(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=2,
        bars=[
            SimpleNamespace(time=datetime(2026, 1, 1, 0, tzinfo=timezone.utc)),
            SimpleNamespace(time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc)),
        ],
        indicator_series={
            ("atr14", "atr"): [1.0, 2.0],
            ("future_return", "value"): [99.0, 99.0],
            ("barrier_outcome", "value"): [88.0, 88.0],
        },
        regimes=["trending", "breakout"],
        sessions=["asia", "london"],
    )


def _dataset() -> EntryMetaDataset:
    trades = [
        {
            "entry_time": "2026-01-01T01:00:00+00:00",
            "strategy": "s",
            "direction": "buy",
            "confidence": 0.7,
            "entry_price": 2000.0,
            "pnl": 25.0,
        }
    ]
    labels = EntryMetaLabelBuilder().build(trades)
    return EntryMetaDataset(
        trades=trades,
        bar_indices=[1],
        labels=labels,
        train_indices=[],
        test_indices=[0],
        unmatched_trades=[],
    )


def test_features_include_entry_context_and_visible_indicators_only() -> None:
    features = EntryMetaFeatureBuilder().build(matrix=_matrix(), dataset=_dataset())

    assert features.feature_keys == [
        "entry.confidence",
        "entry.direction.buy",
        "entry.direction.sell",
        "entry.price",
        "entry.strategy_code",
        "indicator.atr14.atr",
        "matrix.regime_code",
        "matrix.session_code",
    ]
    assert features.rows.shape == (1, 8)
    assert features.rows[0, 0] == 0.7
    assert features.rows[0, 5] == 2.0
    assert "future_return" not in " ".join(features.feature_keys)
    assert "barrier_outcome" not in " ".join(features.feature_keys)
```

- [ ] **Step 6: Run feature tests and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_features.py
```

Expected: import failure for `src.research.entry_meta.features`.

- [ ] **Step 7: Implement feature builder**

Create `src/research/entry_meta/features.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from src.research.entry_meta.dataset import EntryMetaDataset

_FORBIDDEN_TOKENS = ("forward", "future", "barrier", "outcome", "label", "pnl", "exit")


@dataclass(frozen=True)
class EntryMetaFeatureMatrix:
    rows: np.ndarray
    feature_keys: list[str]
    bar_times: list[str]
    train_indices: list[int]
    test_indices: list[int]
    manifest: dict[str, Any]


class EntryMetaFeatureBuilder:
    def build(self, *, matrix: Any, dataset: EntryMetaDataset) -> EntryMetaFeatureMatrix:
        strategy_codes = {
            name: float(idx)
            for idx, name in enumerate(sorted({str(t.get("strategy", "")) for t in dataset.trades}))
        }
        regimes = [str(item) for item in getattr(matrix, "regimes", [])]
        regime_codes = {name: float(idx) for idx, name in enumerate(sorted(set(regimes)))}
        sessions = [str(item) for item in getattr(matrix, "sessions", [])]
        session_codes = {name: float(idx) for idx, name in enumerate(sorted(set(sessions)))}

        visible_keys = [
            key for key in sorted(getattr(matrix, "indicator_series", {}).keys())
            if not self._is_forbidden(f"indicator.{key[0]}.{key[1]}")
        ]
        feature_keys = [
            "entry.confidence",
            "entry.direction.buy",
            "entry.direction.sell",
            "entry.price",
            "entry.strategy_code",
        ]
        feature_keys.extend(f"indicator.{ind}.{field}" for ind, field in visible_keys)
        feature_keys.extend(["matrix.regime_code", "matrix.session_code"])

        rows: list[list[float]] = []
        bar_times: list[str] = []
        for trade, bar_idx in zip(dataset.trades, dataset.bar_indices):
            direction = str(trade.get("direction", "")).lower()
            row = [
                self._to_float(trade.get("confidence")),
                1.0 if direction == "buy" else 0.0,
                1.0 if direction == "sell" else 0.0,
                self._to_float(trade.get("entry_price")),
                strategy_codes.get(str(trade.get("strategy", "")), 0.0),
            ]
            for key in visible_keys:
                row.append(self._to_float(matrix.indicator_series[key][bar_idx]))
            row.append(regime_codes.get(regimes[bar_idx] if bar_idx < len(regimes) else "", 0.0))
            row.append(session_codes.get(sessions[bar_idx] if bar_idx < len(sessions) else "", 0.0))
            rows.append(row)
            bar_times.append(str(trade.get("entry_time", "")))

        return EntryMetaFeatureMatrix(
            rows=np.asarray(rows, dtype=float),
            feature_keys=feature_keys,
            bar_times=bar_times,
            train_indices=list(dataset.train_indices),
            test_indices=list(dataset.test_indices),
            manifest={
                "source": "baseline.raw_results.trades+DataMatrix.indicator_series+regime+session",
                "forbidden_tokens": list(_FORBIDDEN_TOKENS),
                "n_features": len(feature_keys),
            },
        )

    @staticmethod
    def _is_forbidden(key: str) -> bool:
        lowered = key.lower()
        return any(token in lowered for token in _FORBIDDEN_TOKENS)

    @staticmethod
    def _to_float(value: Any) -> float:
        if value is None:
            return 0.0
        try:
            output = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not np.isfinite(output):
            return 0.0
        return output
```

- [ ] **Step 8: Run dataset and feature tests**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_dataset.py tests\research\entry_meta\test_features.py
```

Expected: `3 passed`.

- [ ] **Step 9: Commit Task 2**

Run:

```powershell
git add src/research/entry_meta/dataset.py src/research/entry_meta/features.py tests/research/entry_meta/test_dataset.py tests/research/entry_meta/test_features.py
git commit -m "feat: build entry meta dataset and features"
```

---

### Task 3: CPU Training, Quality Gate, And Config Defaults

**Files:**
- Create: `src/research/entry_meta/training.py`
- Create: `src/research/entry_meta/quality.py`
- Create: `tests/research/entry_meta/test_training.py`
- Create: `tests/research/entry_meta/test_quality.py`
- Modify: `config/research.ini`
- Modify: `src/research/core/config.py`
- Modify: `tests/research/core/test_config.py`

- [ ] **Step 1: Write failing training tests**

Create `tests/research/entry_meta/test_training.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.research.entry_meta.dataset import EntryMetaDatasetBuilder
from src.research.entry_meta.training import train_entry_meta_bundle


def _matrix() -> SimpleNamespace:
    return SimpleNamespace(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=4,
        bars=[SimpleNamespace(time=datetime(2026, 1, 1, i, tzinfo=timezone.utc)) for i in range(4)],
        indicator_series={("atr14", "atr"): [1.0, 2.0, 3.0, 4.0]},
        regimes=["trending", "trending", "breakout", "breakout"],
        sessions=["asia", "london", "london", "new_york"],
        train_slice=lambda: range(0, 3),
        test_slice=lambda: range(3, 4),
    )


def test_train_entry_meta_cpu_artifact_outputs_take_and_block_probabilities() -> None:
    raw_trades = [
        {"entry_time": "2026-01-01T00:00:00+00:00", "strategy": "s", "direction": "buy", "confidence": 0.8, "entry_price": 1.0, "pnl": 10.0},
        {"entry_time": "2026-01-01T01:00:00+00:00", "strategy": "s", "direction": "buy", "confidence": 0.7, "entry_price": 1.0, "pnl": -5.0},
        {"entry_time": "2026-01-01T02:00:00+00:00", "strategy": "s", "direction": "sell", "confidence": 0.6, "entry_price": 1.0, "pnl": -7.0},
        {"entry_time": "2026-01-01T03:00:00+00:00", "strategy": "s", "direction": "sell", "confidence": 0.9, "entry_price": 1.0, "pnl": 20.0},
    ]
    dataset = EntryMetaDatasetBuilder().build(matrix=_matrix(), raw_trades=raw_trades)

    bundle = train_entry_meta_bundle(
        _matrix(),
        dataset=dataset,
        backend_name="cpu",
        model_id="entry-meta-test",
    )

    artifact = bundle.artifact
    assert artifact.model_id == "entry-meta-test"
    assert artifact.label_summary == {"take_entry": 2, "block_entry": 2}
    assert len(artifact.predictions) == 4
    for prediction in artifact.predictions:
        assert round(prediction.take_entry_prob + prediction.block_entry_prob, 8) == 1.0
```

- [ ] **Step 2: Run training tests and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_training.py
```

Expected: import failure for `src.research.entry_meta.training`.

- [ ] **Step 3: Implement CPU training**

Create `src/research/entry_meta/training.py`:

```python
from __future__ import annotations

import base64
import pickle
import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np

from src.research.entry_meta.artifacts import EntryMetaArtifact, EntryMetaPrediction
from src.research.entry_meta.dataset import EntryMetaDataset
from src.research.entry_meta.features import EntryMetaFeatureBuilder, EntryMetaFeatureMatrix
from src.research.state_edge.backends import resolve_backend


@dataclass(frozen=True)
class EntryMetaTrainingBundle:
    artifact: EntryMetaArtifact
    features: EntryMetaFeatureMatrix
    dataset: EntryMetaDataset


def train_entry_meta_bundle(
    matrix: Any,
    *,
    dataset: EntryMetaDataset,
    backend_name: str,
    model_id: str | None = None,
) -> EntryMetaTrainingBundle:
    backend = resolve_backend(backend_name)
    backend.assert_available()
    features = EntryMetaFeatureBuilder().build(matrix=matrix, dataset=dataset)
    y = np.asarray(dataset.labels.labels, dtype=int)
    sample_weight = np.asarray(dataset.labels.sample_weights, dtype=float)
    train_idx = np.asarray(features.train_indices, dtype=int)
    if backend.name == "gpu":
        probs, payload, status = _fit_predict_cpu(features.rows, y, train_idx, sample_weight)
        payload["gpu_phase1_note"] = "phase1 validates CUDA readiness, while this tabular estimator remains CPU-bound by design"
    else:
        probs, payload, status = _fit_predict_cpu(features.rows, y, train_idx, sample_weight)
    predictions = [
        EntryMetaPrediction(
            bar_time=features.bar_times[idx],
            strategy=str(dataset.trades[idx].get("strategy", "")),
            direction=str(dataset.trades[idx].get("direction", "")),
            take_entry_prob=float(probs[idx, 1]),
            block_entry_prob=float(probs[idx, 0]),
        )
        for idx in range(len(dataset.trades))
    ]
    artifact = EntryMetaArtifact(
        model_id=model_id or f"entry-meta-{getattr(matrix, 'timeframe', 'TF')}-{uuid.uuid4().hex[:12]}",
        symbol=str(getattr(matrix, "symbol", "")),
        timeframe=str(getattr(matrix, "timeframe", "")),
        backend=backend.name,
        model_kind="tabular",
        feature_keys=features.feature_keys,
        label_summary=dataset.labels.summary,
        sample_weight_summary=dataset.labels.weight_summary,
        metrics=_metrics(y=y, probs=probs, test_indices=features.test_indices),
        predictions=predictions,
        model_payload=payload,
        feature_manifest=features.manifest,
        status=status,
    )
    return EntryMetaTrainingBundle(artifact=artifact, features=features, dataset=dataset)


def _fit_predict_cpu(
    x: np.ndarray,
    y: np.ndarray,
    train_idx: np.ndarray,
    sample_weight: np.ndarray,
) -> tuple[np.ndarray, dict[str, Any], str]:
    if len(train_idx) == 0 or x.shape[1] == 0 or len(set(y[train_idx].tolist())) < 2:
        probs = _constant_probs(len(y), y[train_idx])
        return probs, {"kind": "constant", "class_probs": probs[0].tolist() if len(probs) else [0.5, 0.5]}, "refit"
    from sklearn.ensemble import HistGradientBoostingClassifier

    model = HistGradientBoostingClassifier(max_iter=80, learning_rate=0.06, random_state=17)
    try:
        model.fit(x[train_idx], y[train_idx], sample_weight=sample_weight[train_idx])
    except TypeError:
        model.fit(x[train_idx], y[train_idx])
    raw = model.predict_proba(x)
    probs = _align_probs(raw, model.classes_, len(y))
    return probs, {
        "kind": "sklearn_pickle",
        "estimator": "HistGradientBoostingClassifier",
        "classes": [int(c) for c in model.classes_],
        "pickle_b64": base64.b64encode(pickle.dumps(model)).decode("ascii"),
    }, "trained"


def _constant_probs(n_rows: int, y_train: np.ndarray) -> np.ndarray:
    if len(y_train) == 0:
        prior = np.asarray([0.5, 0.5], dtype=float)
    else:
        counts = np.bincount(y_train, minlength=2).astype(float) + 1.0
        prior = counts / counts.sum()
    return np.tile(prior, (n_rows, 1))


def _align_probs(raw: np.ndarray, classes: np.ndarray, n_rows: int) -> np.ndarray:
    aligned = np.zeros((n_rows, 2), dtype=float)
    for col, class_id in enumerate(classes):
        aligned[:, int(class_id)] = raw[:, col]
    row_sums = aligned.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0.0] = 1.0
    return aligned / row_sums


def _metrics(*, y: np.ndarray, probs: np.ndarray, test_indices: list[int]) -> dict[str, Any]:
    valid = [idx for idx in test_indices if 0 <= idx < len(y)]
    accuracy = float(np.mean(np.argmax(probs[valid], axis=1) == y[valid])) if valid else 0.0
    return {
        "oos_samples": len(valid),
        "oos_accuracy": accuracy,
        "probability_distribution": {
            "mean_take_entry_prob": float(probs[:, 1].mean()) if len(probs) else 0.0,
            "mean_block_entry_prob": float(probs[:, 0].mean()) if len(probs) else 0.0,
        },
    }
```

- [ ] **Step 4: Run training tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_training.py
```

Expected: `1 passed`.

- [ ] **Step 5: Write failing quality tests**

Create `tests/research/entry_meta/test_quality.py`:

```python
from __future__ import annotations

from src.research.entry_meta.quality import evaluate_entry_meta_quality


def test_quality_accepts_balanced_sufficient_artifact() -> None:
    result = evaluate_entry_meta_quality(
        label_summary={"take_entry": 25, "block_entry": 30},
        metrics={"oos_samples": 20},
        min_samples=40,
        min_oos_samples=10,
        min_class_samples=10,
    )

    assert result["status"] == "accepted"


def test_quality_refits_when_class_is_too_small() -> None:
    result = evaluate_entry_meta_quality(
        label_summary={"take_entry": 3, "block_entry": 30},
        metrics={"oos_samples": 20},
        min_samples=20,
        min_oos_samples=10,
        min_class_samples=10,
    )

    assert result["status"] == "refit"
    assert result["reason"] == "insufficient_class_samples"
```

- [ ] **Step 6: Run quality tests and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_quality.py
```

Expected: import failure for `src.research.entry_meta.quality`.

- [ ] **Step 7: Implement quality gate**

Create `src/research/entry_meta/quality.py`:

```python
from __future__ import annotations

from typing import Any


def evaluate_entry_meta_quality(
    *,
    label_summary: dict[str, Any],
    metrics: dict[str, Any],
    min_samples: int = 40,
    min_oos_samples: int = 10,
    min_class_samples: int = 10,
) -> dict[str, Any]:
    take = int(label_summary.get("take_entry", 0) or 0)
    block = int(label_summary.get("block_entry", 0) or 0)
    total = take + block
    oos_samples = int(metrics.get("oos_samples", 0) or 0)
    if total < min_samples:
        return {"status": "refit", "reason": "insufficient_samples", "sample_count": total}
    if oos_samples < min_oos_samples:
        return {"status": "refit", "reason": "insufficient_oos_samples", "oos_samples": oos_samples}
    if min(take, block) < min_class_samples:
        return {
            "status": "refit",
            "reason": "insufficient_class_samples",
            "take_entry": take,
            "block_entry": block,
        }
    return {"status": "accepted", "reason": "quality_gate_passed", "sample_count": total, "oos_samples": oos_samples}
```

- [ ] **Step 8: Run training and quality tests**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_training.py tests\research\entry_meta\test_quality.py
```

Expected: `3 passed`.

- [ ] **Step 9: Write failing config test**

Append to `tests/research/core/test_config.py`:

```python
def test_research_config_loads_entry_meta_defaults() -> None:
    from src.research.core.config import load_research_config

    cfg = load_research_config()

    assert cfg.entry_meta_model.model_kind in {"tabular", "sequence_mlp"}
    assert cfg.entry_meta_model.top_bucket_quantile == 0.80
    assert cfg.entry_meta_model.min_samples >= 1
```

- [ ] **Step 10: Run config test and verify RED**

Run:

```powershell
python -m pytest -q tests\research\core\test_config.py::test_research_config_loads_entry_meta_defaults
```

Expected: `ResearchConfig` has no `entry_meta_model`.

- [ ] **Step 11: Add config model**

In `src/research/core/config.py`, add:

```python
@dataclass(frozen=True)
class EntryMetaModelConfig:
    model_kind: str = "tabular"
    top_bucket_quantile: float = 0.80
    min_samples: int = 40
    min_oos_samples: int = 10
    min_class_samples: int = 10
    threshold_grid: tuple[float, ...] = (0.50, 0.55, 0.60, 0.65, 0.70)
```

Add field to `ResearchConfig`:

```python
entry_meta_model: EntryMetaModelConfig = field(default_factory=EntryMetaModelConfig)
```

In loader section parsing, read `[entry_meta_model]` and parse `threshold_grid` with the same comma-splitting style used for State Edge.

- [ ] **Step 12: Add config defaults**

Append to `config/research.ini`:

```ini
[entry_meta_model]
model_kind = tabular
top_bucket_quantile = 0.80
min_samples = 40
min_oos_samples = 10
min_class_samples = 10
threshold_grid = 0.50,0.55,0.60,0.65,0.70
```

- [ ] **Step 13: Run Task 3 tests**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_training.py tests\research\entry_meta\test_quality.py tests\research\core\test_config.py
```

Expected: all tests pass.

- [ ] **Step 14: Commit Task 3**

Run:

```powershell
git add src/research/entry_meta/training.py src/research/entry_meta/quality.py tests/research/entry_meta/test_training.py tests/research/entry_meta/test_quality.py config/research.ini src/research/core/config.py tests/research/core/test_config.py
git commit -m "feat: train entry meta baseline model"
```

---

### Task 4: Backtest Overlay Port

**Files:**
- Create: `src/research/entry_meta/overlay.py`
- Create: `tests/backtesting/test_entry_meta_overlay.py`
- Modify: `src/backtesting/engine/runner.py`
- Modify: `src/backtesting/engine/signals.py`

- [ ] **Step 1: Write failing overlay unit tests**

Create `tests/backtesting/test_entry_meta_overlay.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from src.research.entry_meta.artifacts import EntryMetaArtifact, EntryMetaPrediction
from src.research.entry_meta.overlay import EntryMetaBacktestOverlay


def _artifact() -> EntryMetaArtifact:
    return EntryMetaArtifact(
        model_id="entry-meta-test",
        symbol="XAUUSD",
        timeframe="H1",
        backend="cpu",
        model_kind="tabular",
        feature_keys=[],
        label_summary={"take_entry": 1, "block_entry": 1},
        sample_weight_summary={"min": 1.0, "max": 1.0, "mean": 1.0},
        metrics={},
        predictions=[
            EntryMetaPrediction(
                bar_time="2026-01-01T01:00:00+00:00",
                strategy="s",
                direction="buy",
                take_entry_prob=0.40,
                block_entry_prob=0.60,
            )
        ],
        model_payload={"kind": "constant"},
        feature_manifest={},
        status="trained",
    )


def test_shadow_mode_records_without_blocking() -> None:
    overlay = EntryMetaBacktestOverlay(_artifact(), mode="shadow", threshold=0.50)

    verdict = overlay.evaluate(
        datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        "s",
        "buy",
        confidence=0.7,
    )

    assert verdict.allowed is True
    assert verdict.take_entry_prob == 0.40
    assert overlay.report()["blocked"] == 0


def test_filter_blocks_when_take_entry_probability_is_below_threshold() -> None:
    overlay = EntryMetaBacktestOverlay(_artifact(), mode="filter", threshold=0.50)

    verdict = overlay.evaluate(
        datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        "s",
        "buy",
        confidence=0.7,
    )

    assert verdict.allowed is False
    assert verdict.reason == "entry_meta_probability_below_threshold"
    assert overlay.report()["blocked_by_reason"] == {"entry_meta_probability_below_threshold": 1}
```

- [ ] **Step 2: Run overlay tests and verify RED**

Run:

```powershell
python -m pytest -q tests\backtesting\test_entry_meta_overlay.py
```

Expected: import failure for `src.research.entry_meta.overlay`.

- [ ] **Step 3: Implement overlay**

Create `src/research/entry_meta/overlay.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.research.entry_meta.artifacts import EntryMetaArtifact, EntryMetaPrediction, load_artifact


@dataclass(frozen=True)
class EntryMetaOverlayVerdict:
    allowed: bool
    reason: str
    take_entry_prob: float
    block_entry_prob: float
    threshold: float
    prediction: EntryMetaPrediction | None = None


class EntryMetaBacktestOverlay:
    def __init__(self, artifact: EntryMetaArtifact, *, mode: str = "shadow", threshold: float = 0.50) -> None:
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"shadow", "filter"}:
            raise ValueError(f"Unsupported entry meta overlay mode: {mode}")
        self._artifact = artifact
        self._mode = normalized_mode
        self._threshold = float(threshold)
        self._predictions = {
            self._key(pred.bar_time, pred.strategy, pred.direction): pred
            for pred in artifact.predictions
        }
        self.reset()

    @classmethod
    def from_artifact_path(cls, path: str | Path, *, mode: str = "shadow", threshold: float = 0.50) -> "EntryMetaBacktestOverlay":
        return cls(load_artifact(path), mode=mode, threshold=threshold)

    def reset(self) -> None:
        self._observed = 0
        self._allowed = 0
        self._blocked = 0
        self._missing_predictions = 0
        self._blocked_by_reason: dict[str, int] = {}
        self._blocked_by_strategy: dict[str, int] = {}

    def evaluate(self, bar_time: datetime, strategy: str, direction: str, *, confidence: float = 0.0) -> EntryMetaOverlayVerdict:
        del confidence
        self._observed += 1
        prediction = self._predictions.get(self._key(bar_time, strategy, direction))
        if prediction is None:
            self._missing_predictions += 1
            self._allowed += 1
            return EntryMetaOverlayVerdict(True, "entry_meta_prediction_missing", 1.0, 0.0, self._threshold, None)
        if self._mode == "filter" and prediction.take_entry_prob < self._threshold:
            self._blocked += 1
            self._blocked_by_reason["entry_meta_probability_below_threshold"] = self._blocked_by_reason.get("entry_meta_probability_below_threshold", 0) + 1
            self._blocked_by_strategy[strategy] = self._blocked_by_strategy.get(strategy, 0) + 1
            return EntryMetaOverlayVerdict(False, "entry_meta_probability_below_threshold", prediction.take_entry_prob, prediction.block_entry_prob, self._threshold, prediction)
        self._allowed += 1
        return EntryMetaOverlayVerdict(True, "allowed", prediction.take_entry_prob, prediction.block_entry_prob, self._threshold, prediction)

    def report(self) -> dict[str, Any]:
        return {
            "model_id": self._artifact.model_id,
            "artifact_timeframe": self._artifact.timeframe,
            "mode": self._mode,
            "threshold": self._threshold,
            "observed": self._observed,
            "allowed": self._allowed,
            "blocked": self._blocked,
            "missing_predictions": self._missing_predictions,
            "blocked_by_reason": dict(sorted(self._blocked_by_reason.items())),
            "blocked_by_strategy": dict(sorted(self._blocked_by_strategy.items())),
        }

    def _key(self, bar_time: datetime | str, strategy: str, direction: str) -> tuple[str, str, str]:
        return (self._normalize_time(bar_time), str(strategy), str(direction).lower())

    @staticmethod
    def _normalize_time(value: datetime | str) -> str:
        if isinstance(value, datetime):
            normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            return normalized.astimezone(timezone.utc).isoformat()
        raw = str(value)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
```

- [ ] **Step 4: Run overlay tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests\backtesting\test_entry_meta_overlay.py
```

Expected: `2 passed`.

- [ ] **Step 5: Write failing process_decision test**

Append to `tests/backtesting/test_entry_meta_overlay.py`:

```python
from types import SimpleNamespace

from src.backtesting.engine.signals import process_decision
from src.clients.mt5_market import OHLC
from src.signals.models import SignalDecision


def test_process_decision_records_entry_meta_blocked_event_before_entry() -> None:
    overlay = EntryMetaBacktestOverlay(_artifact(), mode="filter", threshold=0.50)
    rejections: list[str] = []
    blocked_events: list[dict] = []
    engine = SimpleNamespace(
        _config=SimpleNamespace(enable_state_machine=False, confidence=SimpleNamespace(min_confidence=0.1)),
        _circuit_breaker=None,
        _portfolio=SimpleNamespace(_open_positions=[]),
        _strategy_deployments={},
        _state_edge_overlay=None,
        _entry_meta_overlay=overlay,
        _pending_entry_enabled=False,
        record_execution_rejection=lambda reason: rejections.append(reason),
        record_blocked_entry=lambda event: blocked_events.append(event),
    )
    decision = SignalDecision(
        strategy="s",
        symbol="XAUUSD",
        timeframe="H1",
        direction="buy",
        confidence=0.7,
        reason="test",
    )
    bar = OHLC(
        symbol="XAUUSD",
        timeframe="H1",
        time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
    )

    process_decision(engine, decision, bar, 1, {"atr14": {"atr": 1.0}}, "trending")

    assert rejections == ["entry_meta_filter"]
    assert blocked_events[0]["source"] == "entry_meta_overlay"
    assert blocked_events[0]["reason"] == "entry_meta_probability_below_threshold"
    assert blocked_events[0]["take_entry_prob"] == 0.40
```

- [ ] **Step 6: Run process_decision test and verify RED**

Run:

```powershell
python -m pytest -q tests\backtesting\test_entry_meta_overlay.py::test_process_decision_records_entry_meta_blocked_event_before_entry
```

Expected: `rejections == []` or missing `_entry_meta_overlay` handling.

- [ ] **Step 7: Modify BacktestEngine ports**

Modify `src/backtesting/engine/runner.py`:

- Add constructor parameter:

```python
entry_meta_overlay: Optional[Any] = None,
```

- Store it:

```python
self._entry_meta_overlay = entry_meta_overlay
```

- Reset it in `_reset_run_state()`:

```python
if self._entry_meta_overlay is not None:
    self._entry_meta_overlay.reset()
```

- Add report method:

```python
def entry_meta_overlay_report(self) -> Optional[dict[str, Any]]:
    if self._entry_meta_overlay is None:
        return None
    return self._entry_meta_overlay.report()
```

Do not remove or rename existing State Edge ports.

- [ ] **Step 8: Modify process_decision**

In `src/backtesting/engine/signals.py`, after State Edge overlay check and before ATR/entry execution, add:

```python
entry_meta_overlay = getattr(engine, "_entry_meta_overlay", None)
if entry_meta_overlay is not None:
    verdict = entry_meta_overlay.evaluate(
        bar.time,
        decision.strategy,
        decision.direction,
        confidence=decision.confidence,
    )
    if not verdict.allowed:
        engine.record_execution_rejection("entry_meta_filter")
        engine.record_blocked_entry(
            {
                "source": "entry_meta_overlay",
                "execution_reason": "entry_meta_filter",
                "reason": verdict.reason,
                "bar_time": bar.time.isoformat() if hasattr(bar.time, "isoformat") else str(bar.time),
                "bar_index": int(bar_index),
                "strategy": str(decision.strategy),
                "direction": str(decision.direction),
                "confidence": float(decision.confidence),
                "regime": str(getattr(regime, "value", regime)),
                "price": float(bar.close),
                "take_entry_prob": float(verdict.take_entry_prob),
                "block_entry_prob": float(verdict.block_entry_prob),
                "threshold": float(verdict.threshold),
            }
        )
        return
```

- [ ] **Step 9: Run backtest overlay tests**

Run:

```powershell
python -m pytest -q tests\backtesting\test_entry_meta_overlay.py tests\backtesting\test_state_edge_overlay.py
```

Expected: all tests pass.

- [ ] **Step 10: Commit Task 4**

Run:

```powershell
git add src/research/entry_meta/overlay.py src/backtesting/engine/runner.py src/backtesting/engine/signals.py tests/backtesting/test_entry_meta_overlay.py
git commit -m "feat: add entry meta backtest overlay"
```

---

### Task 5: Research Lab CLI And Backtest Runner Args

**Files:**
- Create: `src/research/entry_meta/lab.py`
- Create: `src/ops/cli/entry_meta_lab.py`
- Modify: `src/ops/cli/backtest_runner.py`
- Create: `tests/research/entry_meta/test_lab.py`
- Create: `tests/research/entry_meta/test_entry_meta_lab_cli.py`
- Create: `tests/backtesting/test_entry_meta_backtest_runner_cli.py`

- [ ] **Step 1: Write failing lab orchestration test**

Create `tests/research/entry_meta/test_lab.py`:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.research.entry_meta.lab import EntryMetaLab


class _Deps:
    pass


def test_entry_meta_lab_trains_artifact_from_baseline_json(tmp_path: Path, monkeypatch) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "raw_results": [
                    {
                        "trades": [
                            {
                                "entry_time": "2026-01-01T00:00:00+00:00",
                                "strategy": "s",
                                "direction": "buy",
                                "confidence": 0.8,
                                "entry_price": 1.0,
                                "pnl": 10.0,
                            },
                            {
                                "entry_time": "2026-01-01T01:00:00+00:00",
                                "strategy": "s",
                                "direction": "buy",
                                "confidence": 0.6,
                                "entry_price": 1.0,
                                "pnl": -5.0,
                            },
                        ]
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    matrix = SimpleNamespace(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=2,
        bars=[
            SimpleNamespace(time=datetime(2026, 1, 1, 0, tzinfo=timezone.utc)),
            SimpleNamespace(time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc)),
        ],
        indicator_series={("atr14", "atr"): [1.0, 2.0]},
        regimes=["trending", "trending"],
        sessions=["asia", "london"],
        train_slice=lambda: range(0, 1),
        test_slice=lambda: range(1, 2),
    )
    monkeypatch.setattr("src.research.entry_meta.lab.build_data_matrix", lambda **kwargs: matrix)

    result = EntryMetaLab(
        deps=_Deps(),
        backend_name="cpu",
        artifact_dir=tmp_path / "artifacts",
    ).run(
        baseline_path=baseline,
        symbol="XAUUSD",
        timeframe="H1",
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert result.artifact.model_id.startswith("entry-meta-H1-")
    assert result.artifact_path.name == "entry_meta_artifact.json"
    assert result.dataset_summary["matched_trades"] == 2
```

- [ ] **Step 2: Run lab orchestration test and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_lab.py
```

Expected: import failure for `src.research.entry_meta.lab`.

- [ ] **Step 3: Implement lab orchestration**

Create `src/research/entry_meta/lab.py`:

```python
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.research.core.config import ResearchConfig, load_research_config
from src.research.core.data_matrix import build_data_matrix
from src.research.core.ports import ResearchDataDeps
from src.research.entry_meta.artifacts import EntryMetaArtifact, save_artifact
from src.research.entry_meta.dataset import EntryMetaDatasetBuilder
from src.research.entry_meta.quality import evaluate_entry_meta_quality
from src.research.entry_meta.training import train_entry_meta_bundle
from src.research.features.hub import FeatureHub
from src.utils.timezone import utc_now


@dataclass(frozen=True)
class EntryMetaLabResult:
    artifact: EntryMetaArtifact
    artifact_path: Path
    dataset_summary: dict[str, Any]
    quality: dict[str, Any]
    feature_compute_summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.artifact.model_id,
            "symbol": self.artifact.symbol,
            "timeframe": self.artifact.timeframe,
            "status": self.artifact.status,
            "artifact_path": str(self.artifact_path),
            "backend": self.artifact.backend,
            "label_summary": self.artifact.label_summary,
            "sample_weight_summary": self.artifact.sample_weight_summary,
            "metrics": self.artifact.metrics,
            "dataset_summary": self.dataset_summary,
            "quality": self.quality,
            "feature_compute_summary": self.feature_compute_summary,
            "feature_manifest": self.artifact.feature_manifest,
        }


class EntryMetaLab:
    def __init__(
        self,
        *,
        deps: ResearchDataDeps,
        config: ResearchConfig | None = None,
        backend_name: str = "cpu",
        artifact_dir: str | Path = "artifacts/entry_meta",
    ) -> None:
        self._deps = deps
        self._config = config or load_research_config()
        self._backend_name = backend_name
        self._artifact_dir = Path(artifact_dir)
        self._feature_hub = FeatureHub(self._config)

    def run(
        self,
        *,
        baseline_path: str | Path,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
    ) -> EntryMetaLabResult:
        raw_trades = _load_raw_trades(baseline_path)
        extra_reqs = self._feature_hub.required_extra_data()
        if extra_reqs:
            raise ValueError(
                "EntryMetaLab phase 1 does not support cross-TF extra data yet; "
                "disable cross_tf provider in research.ini for this run."
            )
        matrix = build_data_matrix(
            symbol=symbol,
            timeframe=timeframe,
            start_time=start_time,
            end_time=end_time,
            forward_horizons=self._config.forward_horizons,
            warmup_bars=self._config.warmup_bars,
            train_ratio=self._config.train_ratio,
            round_trip_cost_pct=self._config.round_trip_cost_pct,
            deps=self._deps,
        )
        feature_summary = self._feature_hub.compute_all(matrix).to_dict()
        dataset = EntryMetaDatasetBuilder().build(matrix=matrix, raw_trades=raw_trades)
        model_id = f"entry-meta-{timeframe}-{utc_now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        bundle = train_entry_meta_bundle(
            matrix,
            dataset=dataset,
            backend_name=self._backend_name,
            model_id=model_id,
        )
        artifact_path = save_artifact(bundle.artifact, self._artifact_dir / model_id)
        quality = evaluate_entry_meta_quality(
            label_summary=bundle.artifact.label_summary,
            metrics=bundle.artifact.metrics,
            min_samples=self._config.entry_meta_model.min_samples,
            min_oos_samples=self._config.entry_meta_model.min_oos_samples,
            min_class_samples=self._config.entry_meta_model.min_class_samples,
        )
        return EntryMetaLabResult(
            artifact=bundle.artifact,
            artifact_path=artifact_path,
            dataset_summary={
                "raw_trades": len(raw_trades),
                "matched_trades": len(dataset.trades),
                "unmatched_trades": len(dataset.unmatched_trades),
            },
            quality=quality,
            feature_compute_summary=feature_summary,
        )


def _load_raw_trades(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    raw_results = payload.get("raw_results", [])
    trades: list[dict[str, Any]] = []
    if isinstance(raw_results, list):
        for raw_result in raw_results:
            if isinstance(raw_result, dict) and isinstance(raw_result.get("trades"), list):
                trades.extend(dict(item) for item in raw_result["trades"] if isinstance(item, dict))
    if not trades and isinstance(payload.get("trades"), list):
        trades.extend(dict(item) for item in payload["trades"] if isinstance(item, dict))
    return trades
```

- [ ] **Step 4: Run lab orchestration test and verify GREEN**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_lab.py
```

Expected: `1 passed`.

- [ ] **Step 5: Write failing CLI help test**

Create `tests/research/entry_meta/test_entry_meta_lab_cli.py`:

```python
from __future__ import annotations

import subprocess
import sys


def test_entry_meta_lab_cli_help() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "src.ops.cli.entry_meta_lab", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--baseline" in completed.stdout
    assert "--tf" in completed.stdout
    assert "--backend" in completed.stdout
    assert "--json-output" in completed.stdout
```

- [ ] **Step 6: Run CLI help test and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_entry_meta_lab_cli.py
```

Expected: module not found for `src.ops.cli.entry_meta_lab`.

- [ ] **Step 7: Implement CLI using EntryMetaLab**

Create `src/ops/cli/entry_meta_lab.py`:

```python
from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

from src.utils.timezone import parse_iso_to_utc


def main() -> None:
    from src.backtesting.component_factory import build_research_data_deps
    from src.config.instance_context import set_current_environment
    from src.ops.cli._coverage import ensure_ohlc_data_coverage
    from src.research.core import load_research_config
    from src.research.entry_meta.lab import EntryMetaLab
    from src.research.state_edge.backends import resolve_backend

    parser = argparse.ArgumentParser(description="Entry Meta-Label research lab")
    parser.add_argument("--environment", choices=["live", "demo"], required=True)
    parser.add_argument("--baseline", required=True, help="Baseline backtest JSON with raw_results.trades")
    parser.add_argument("--tf", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--backend", choices=["cpu", "gpu"], default="cpu")
    parser.add_argument("--artifact-dir", default="artifacts/entry_meta")
    parser.add_argument("--json-output", required=True)
    parser.add_argument("--no-auto-backfill", action="store_true")
    args = parser.parse_args()

    set_current_environment(args.environment)
    backend = resolve_backend(args.backend)
    backend.assert_available()
    config = load_research_config()
    coverage = ensure_ohlc_data_coverage(
        symbol="XAUUSD",
        timeframes=[args.tf.upper()],
        start=parse_iso_to_utc(args.start),
        end=parse_iso_to_utc(args.end),
        auto_backfill=not args.no_auto_backfill,
    )
    with build_research_data_deps() as deps:
        result = EntryMetaLab(
            deps=deps,
            config=config,
            backend_name=args.backend,
            artifact_dir=args.artifact_dir,
        ).run(
            baseline_path=args.baseline,
            symbol="XAUUSD",
            timeframe=args.tf.upper(),
            start_time=parse_iso_to_utc(args.start),
            end_time=parse_iso_to_utc(args.end),
        )
    payload = {
        "symbol": "XAUUSD",
        "environment": args.environment,
        "backend": args.backend,
        "coverage": {tf: info.to_dict() for tf, info in coverage.items()},
        "result": result.to_dict(),
    }
    output_path = Path(args.json_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 8: Run CLI help test and verify GREEN**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_entry_meta_lab_cli.py
```

Expected: `1 passed`.

- [ ] **Step 9: Write failing backtest_runner parser smoke test**

Create `tests/backtesting/test_entry_meta_backtest_runner_cli.py`:

```python
from __future__ import annotations

import subprocess
import sys


def test_backtest_runner_help_exposes_entry_meta_overlay_args() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "src.ops.cli.backtest_runner", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--entry-meta-artifact" in completed.stdout
    assert "--entry-meta-mode" in completed.stdout
    assert "--entry-meta-threshold-grid" in completed.stdout
```

- [ ] **Step 10: Run parser smoke test and verify RED**

Run:

```powershell
python -m pytest -q tests\backtesting\test_entry_meta_backtest_runner_cli.py
```

Expected: assertion failure for missing `--entry-meta-artifact`.

- [ ] **Step 11: Modify backtest_runner args and _run_single**

In `src/ops/cli/backtest_runner.py`:

- Add `_run_single()` parameters:

```python
entry_meta_artifact: Optional[str] = None,
entry_meta_mode: str = "shadow",
entry_meta_threshold: float = 0.50,
```

- Build overlay:

```python
entry_meta_overlay = None
if entry_meta_artifact:
    from src.research.entry_meta.overlay import EntryMetaBacktestOverlay

    entry_meta_overlay = EntryMetaBacktestOverlay.from_artifact_path(
        entry_meta_artifact,
        mode=entry_meta_mode,
        threshold=entry_meta_threshold,
    )
```

- Pass to engine:

```python
entry_meta_overlay=entry_meta_overlay,
```

- After `engine.run()`:

```python
entry_meta_overlay_report = engine.entry_meta_overlay_report()
```

- Add output fields:

```python
if entry_meta_overlay_report is not None:
    output["entry_meta_overlay"] = entry_meta_overlay_report
    output["entry_meta_threshold"] = entry_meta_threshold
```

- Add parser args:

```python
parser.add_argument("--entry-meta-artifact", default=None)
parser.add_argument("--entry-meta-mode", choices=["shadow", "filter"], default="shadow")
parser.add_argument("--entry-meta-threshold-grid", default="0.50,0.55,0.60,0.65,0.70")
```

Use the existing `_parse_threshold_grid()` for threshold parsing.

- [ ] **Step 12: Run parser smoke test and existing State Edge runner tests**

Run:

```powershell
python -m pytest -q tests\backtesting\test_entry_meta_backtest_runner_cli.py tests\backtesting\test_state_edge_overlay.py
```

Expected: all tests pass.

- [ ] **Step 13: Commit Task 5**

Run:

```powershell
git add src/research/entry_meta/lab.py src/ops/cli/entry_meta_lab.py src/ops/cli/backtest_runner.py tests/research/entry_meta/test_lab.py tests/research/entry_meta/test_entry_meta_lab_cli.py tests/backtesting/test_entry_meta_backtest_runner_cli.py
git commit -m "feat: add entry meta research cli ports"
```

---

### Task 6: Overlay Report And Attribution

**Files:**
- Create: `src/research/entry_meta/evaluation.py`
- Create: `src/ops/cli/entry_meta_overlay_report.py`
- Create: `tests/research/entry_meta/test_evaluation.py`
- Create: `tests/research/entry_meta/test_overlay_report_cli.py`

- [ ] **Step 1: Write failing evaluation tests**

Create `tests/research/entry_meta/test_evaluation.py`:

```python
from __future__ import annotations

from src.research.entry_meta.evaluation import build_entry_meta_overlay_report


def _result(*, trades: int, pnl: float, pf: float, expectancy: float) -> dict:
    return {
        "metrics": {"trades": trades, "pnl": pnl, "pf": pf, "expectancy": expectancy, "max_dd": 3.0},
        "execution_summary": {},
        "raw_trades": [],
    }


def test_report_rejects_pf_gain_when_pnl_and_expectancy_degrade() -> None:
    baseline = _result(trades=53, pnl=581.49, pf=2.299, expectancy=10.97)
    filter_result = _result(trades=45, pnl=438.94, pf=2.315, expectancy=9.75)
    filter_result["entry_meta_overlay"] = {"blocked": 19, "threshold": 0.5}

    report = build_entry_meta_overlay_report(
        baseline=baseline,
        shadow=None,
        filters=[filter_result],
    )

    payload = report.to_dict()
    assert payload["status"] == "rejected"
    assert payload["threshold_report"]["threshold_results"][0]["decision"]["reason"] == "return_expectancy_degraded"


def test_report_matches_blocked_events_to_baseline_raw_trades() -> None:
    baseline = _result(trades=2, pnl=10.0, pf=1.5, expectancy=5.0)
    baseline["raw_trades"] = [
        {
            "signal_id": "bt_1",
            "entry_time": "2026-01-01T01:00:00+00:00",
            "strategy": "s",
            "direction": "buy",
            "pnl": 25.0,
            "exit_reason": "take_profit",
        }
    ]
    filter_result = _result(trades=1, pnl=-15.0, pf=0.5, expectancy=-15.0)
    filter_result["entry_meta_overlay"] = {"blocked": 1, "threshold": 0.5}
    filter_result["execution_summary"] = {
        "blocked_entry_events": [
            {
                "source": "entry_meta_overlay",
                "bar_time": "2026-01-01T01:00:00+00:00",
                "strategy": "s",
                "direction": "buy",
                "take_entry_prob": 0.1,
                "threshold": 0.5,
            }
        ]
    }

    report = build_entry_meta_overlay_report(
        baseline=baseline,
        shadow=None,
        filters=[filter_result],
    )

    attribution = report.to_dict()["filter_diagnostics"][0]["blocked_trade_attribution"]
    assert attribution["matched_trades"] == 1
    assert attribution["matched_pnl"] == 25.0
```

- [ ] **Step 2: Run evaluation tests and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_evaluation.py
```

Expected: import failure for `src.research.entry_meta.evaluation`.

- [ ] **Step 3: Implement evaluation by reusing State Edge semantics**

Create `src/research/entry_meta/evaluation.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.research.state_edge.evaluation import (
    StateEdgeOverlayValidationReport,
    build_overlay_validation_report,
)


@dataclass(frozen=True)
class EntryMetaOverlayValidationReport:
    payload: StateEdgeOverlayValidationReport

    def to_dict(self) -> dict[str, Any]:
        data = self.payload.to_dict()
        for item in data.get("threshold_report", {}).get("threshold_results", []):
            if "state_edge_overlay" in item:
                item["entry_meta_overlay"] = item.pop("state_edge_overlay")
        return data

    @property
    def status(self) -> str:
        return self.payload.status


def build_entry_meta_overlay_report(
    *,
    baseline: dict[str, Any],
    shadow: dict[str, Any] | None,
    filters: list[dict[str, Any]],
    max_dd_worsen_ratio: float = 0.10,
    min_trades: int = 10,
) -> EntryMetaOverlayValidationReport:
    normalized_filters = [_normalize_overlay_key(item) for item in filters]
    normalized_shadow = _normalize_overlay_key(shadow) if shadow is not None else None
    return EntryMetaOverlayValidationReport(
        build_overlay_validation_report(
            baseline=baseline,
            shadow=normalized_shadow,
            filters=normalized_filters,
            max_dd_worsen_ratio=max_dd_worsen_ratio,
            min_trades=min_trades,
        )
    )


def _normalize_overlay_key(payload: dict[str, Any] | None) -> dict[str, Any]:
    item = dict(payload or {})
    if "entry_meta_overlay" in item and "state_edge_overlay" not in item:
        item["state_edge_overlay"] = dict(item["entry_meta_overlay"])
    return item
```

- [ ] **Step 4: Run evaluation tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_evaluation.py
```

Expected: `2 passed`.

- [ ] **Step 5: Write failing report CLI tests**

Create `tests/research/entry_meta/test_overlay_report_cli.py`:

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_entry_meta_overlay_report_cli_help() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "src.ops.cli.entry_meta_overlay_report", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--baseline" in completed.stdout
    assert "--filters" in completed.stdout


def test_entry_meta_overlay_report_reads_raw_trades(tmp_path: Path) -> None:
    from src.ops.cli.entry_meta_overlay_report import build_report_from_paths

    baseline = tmp_path / "baseline.json"
    filter_path = tmp_path / "filter.json"
    baseline.write_text(
        json.dumps(
            {
                "results": [{"metrics": {"trades": 1, "pnl": 25.0, "pf": 2.0, "expectancy": 25.0, "max_dd": 1.0}}],
                "raw_results": [{"trades": [{"entry_time": "2026-01-01T01:00:00+00:00", "strategy": "s", "direction": "buy", "pnl": 25.0}]}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    filter_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "metrics": {"trades": 0, "pnl": 0.0, "pf": 0.0, "expectancy": 0.0, "max_dd": 1.0},
                        "entry_meta_overlay": {"blocked": 1, "threshold": 0.5},
                        "execution_summary": {"blocked_entry_events": [{"bar_time": "2026-01-01T01:00:00+00:00", "strategy": "s", "direction": "buy"}]},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = build_report_from_paths(
        baseline_path=baseline,
        shadow_path=None,
        filter_paths=[filter_path],
    )

    attribution = payload["overlay_report"]["filter_diagnostics"][0]["blocked_trade_attribution"]
    assert attribution["matched_trades"] == 1
```

- [ ] **Step 6: Run report CLI tests and verify RED**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_overlay_report_cli.py
```

Expected: module not found for `src.ops.cli.entry_meta_overlay_report`.

- [ ] **Step 7: Implement report CLI**

Create `src/ops/cli/entry_meta_overlay_report.py`:

```python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))


def build_report_from_paths(
    *,
    baseline_path: str | Path,
    shadow_path: str | Path | None,
    filter_paths: list[str | Path],
    max_dd_worsen_ratio: float = 0.10,
    min_trades: int = 10,
) -> dict[str, Any]:
    from src.research.entry_meta.evaluation import build_entry_meta_overlay_report
    from src.ops.cli.state_edge_overlay_report import _load_results

    baseline = _load_single_result(baseline_path)
    shadow = _load_single_result(shadow_path) if shadow_path is not None else None
    filters: list[dict[str, Any]] = []
    for path in filter_paths:
        filters.extend(_load_results(path))
    report = build_entry_meta_overlay_report(
        baseline=baseline,
        shadow=shadow,
        filters=filters,
        max_dd_worsen_ratio=max_dd_worsen_ratio,
        min_trades=min_trades,
    )
    return {
        "status": report.status,
        "input_files": {
            "baseline": str(Path(baseline_path)),
            "shadow": str(Path(shadow_path)) if shadow_path is not None else None,
            "filters": [str(Path(path)) for path in filter_paths],
        },
        "overlay_report": report.to_dict(),
    }


def _load_single_result(path: str | Path) -> dict[str, Any]:
    from src.ops.cli.state_edge_overlay_report import _load_results

    results = _load_results(path)
    if len(results) != 1:
        raise ValueError(f"expected exactly one result: {path}")
    return results[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Entry Meta overlay report")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--shadow", default=None)
    parser.add_argument("--filters", nargs="+", required=True)
    parser.add_argument("--json-output", required=True)
    parser.add_argument("--max-dd-worsen-ratio", type=float, default=0.10)
    parser.add_argument("--min-trades", type=int, default=10)
    args = parser.parse_args()
    payload = build_report_from_paths(
        baseline_path=args.baseline,
        shadow_path=args.shadow,
        filter_paths=list(args.filters),
        max_dd_worsen_ratio=args.max_dd_worsen_ratio,
        min_trades=args.min_trades,
    )
    output_path = Path(args.json_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 8: Run Task 6 tests**

Run:

```powershell
python -m pytest -q tests\research\entry_meta\test_evaluation.py tests\research\entry_meta\test_overlay_report_cli.py
```

Expected: all tests pass.

- [ ] **Step 9: Commit Task 6**

Run:

```powershell
git add src/research/entry_meta/evaluation.py src/ops/cli/entry_meta_overlay_report.py tests/research/entry_meta/test_evaluation.py tests/research/entry_meta/test_overlay_report_cli.py
git commit -m "feat: report entry meta overlay attribution"
```

---

### Task 7: Documentation

**Files:**
- Modify: `docs/research-system.md`
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Update docs**

In `docs/research-system.md`, add a subsection under State Edge:

```markdown
#### Entry Meta-Label Overlay Lab

Entry Meta-Label Overlay 是 State Edge 之后的 trade-aware 研究层。State Edge 预测市场状态；Entry Meta 预测已有策略 entry 是否值得保留。第一阶段只使用 baseline backtest 的 `raw_results.trades` 训练 `take_entry_prob / block_entry_prob`，只进入 Research + Backtest overlay，不接入 demo/live runtime。

验收必须使用 `entry_meta_overlay_report`，并检查 `blocked_trade_attribution` 是否挡掉大盈利单。
```

In `docs/codebase-review.md`, add to the GPU Research Lab section:

```markdown
- 2026-05-03：新增 Entry Meta-Label Overlay Lab 设计与实施计划。职责边界：State Edge 是市场状态概率，Entry Meta 是交易入场保留概率。第一阶段只消费 baseline `raw_results.trades`，不接入 demo/live。
```

- [ ] **Step 2: Commit Task 7**

Run:

```powershell
git add docs/research-system.md docs/codebase-review.md
git commit -m "docs: document entry meta research lab"
```

---

### Task 8: End-To-End Verification

**Files:**
- No new source files.
- Uses artifacts generated by previous tasks.

- [ ] **Step 1: Run focused tests**

Run:

```powershell
python -m pytest -q tests\research\entry_meta tests\backtesting\test_entry_meta_overlay.py tests\backtesting\test_entry_meta_backtest_runner_cli.py tests\research\core\test_config.py
```

Expected: all tests pass.

- [ ] **Step 2: Run State Edge regression tests**

Run:

```powershell
python -m pytest -q tests\research\state_edge tests\backtesting\test_state_edge_overlay.py
```

Expected: all tests pass. This confirms Entry Meta did not break the existing State Edge research chain.

- [ ] **Step 3: Run CLI help smoke**

Run:

```powershell
python -m src.ops.cli.entry_meta_lab --help
python -m src.ops.cli.entry_meta_overlay_report --help
python -m src.ops.cli.backtest_runner --help
```

Expected: each command exits 0 and prints its help text.

- [ ] **Step 4: Run H1 small-window smoke only after tests are green**

Use an existing baseline JSON that includes `raw_results.trades`. If no small baseline exists, generate one with:

```powershell
python -m src.ops.cli.backtest_runner --environment live --tf H1 --start 2026-03-01 --end 2026-03-15 --include-demo-validation --json-output artifacts\entry_meta_h1_smoke_baseline.json
```

Then train:

```powershell
python -m src.ops.cli.entry_meta_lab --environment live --baseline artifacts\entry_meta_h1_smoke_baseline.json --tf H1 --start 2026-03-01 --end 2026-03-15 --backend cpu --json-output artifacts\entry_meta_h1_smoke_train.json
```

Expected: the train command writes JSON with `result.artifact_path`, `result.dataset_summary`, `result.quality`, and `result.metrics`.

- [ ] **Step 5: Final status commit**

If Task 8 required documentation updates from smoke findings, commit those updates:

```powershell
git add docs/research-system.md docs/codebase-review.md artifacts\entry_meta_h1_smoke_train.json
git commit -m "docs: record entry meta smoke result"
```

If no documentation or tracked artifact updates are needed, skip this commit and state that the tree has no Task 8 source changes.

---

## Self-Review

Spec coverage:

- Entry candidate dataset from `raw_results.trades`: Task 2.
- Trade outcome labels and sample weights: Task 1.
- CPU baseline and GPU fail-fast boundary: Task 3.
- Backtest shadow/filter overlay: Task 4 and Task 5.
- Report with blocked attribution: Task 6.
- Config and documentation: Task 7.
- H1 smoke and regression verification: Task 8.

Scope check:

- The plan stays inside Research + Backtest overlay.
- The plan does not add demo/live runtime integration.
- The plan keeps State Edge and Entry Meta in separate modules.

Placeholder scan:

- No task uses incomplete placeholder markers or unspecified file names.
- Every code-producing step includes concrete file paths and code blocks.
- Every test step includes an exact command and expected outcome.
