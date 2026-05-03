# Entry Meta Runtime-Safe Feature Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Entry Meta Lab train runtime-safe artifacts whose dynamic scorer can be replayed on forward backtests without FeatureHub-only feature misses.

**Architecture:** Add a first-class `feature_scope` contract to Entry Meta. `runtime_safe` artifacts train only on entry fields, backtest-current indicators, regime, and session; `research_full` artifacts remain available for offline lookup analysis but are explicitly blocked from lookup-miss dynamic scoring.

**Tech Stack:** Python 3.12, pytest, NumPy/scikit-learn JSON-native scorer, existing Entry Meta Research + Backtest overlay modules.

---

## File Structure

- Modify `src/research/core/config.py`
  - Add `EntryMetaModelConfig.feature_scope`.
  - Load `[entry_meta_model] feature_scope` from `config/research.ini`.
- Modify `config/research.ini`
  - Set `feature_scope = runtime_safe` under `[entry_meta_model]`.
- Modify `src/ops/cli/entry_meta_lab.py`
  - Add `--feature-scope runtime_safe|research_full`, defaulting to config when omitted.
  - Pass the value to `EntryMetaLab.run(...)`.
- Modify `src/research/entry_meta/features.py`
  - Add `FEATURE_SCOPE_RUNTIME_SAFE`, `FEATURE_SCOPE_RESEARCH_FULL`, and `SUPPORTED_FEATURE_SCOPES`.
  - Let `EntryMetaFeatureBuilder` filter indicator keys by `runtime_indicator_names` when `feature_scope="runtime_safe"`.
  - Write `feature_scope`, `dynamic_scoring_supported`, and `runtime_indicator_names` into the manifest.
- Modify `src/research/entry_meta/lab.py`
  - Load baseline trades plus `raw_results[0].strategy_capability_execution_plan.required_indicators_union`.
  - Fail fast in `runtime_safe` mode when that union is missing or empty.
  - Pass feature scope and runtime indicator names into training.
- Modify `src/research/entry_meta/training.py`
  - Accept `feature_scope` and `runtime_indicator_names`.
  - Use `EntryMetaFeatureBuilder(...)` with those arguments.
  - Reject runtime-safe artifacts that have no runtime indicator feature columns.
- Modify `src/research/entry_meta/overlay.py`
  - Gate dynamic scorer construction on manifest `feature_scope="runtime_safe"` and `dynamic_scoring_supported=true`.
  - Return `entry_meta_dynamic_feature_scope_unsupported` on lookup miss for old or `research_full` artifacts.
- Modify tests:
  - `tests/research/core/test_config.py`
  - `tests/research/entry_meta/test_entry_meta_lab_cli.py`
  - `tests/research/entry_meta/test_features.py`
  - `tests/research/entry_meta/test_lab.py`
  - `tests/research/entry_meta/test_training.py`
  - `tests/backtesting/test_entry_meta_overlay.py`
- Modify `docs/codebase-review.md`
  - Record the new feature-scope boundary and the forward-shadow acceptance criterion.

---

### Task 1: Config And CLI Feature Scope Contract

**Files:**
- Modify: `src/research/core/config.py`
- Modify: `config/research.ini`
- Modify: `src/ops/cli/entry_meta_lab.py`
- Modify: `tests/research/core/test_config.py`
- Modify: `tests/research/entry_meta/test_entry_meta_lab_cli.py`

- [ ] **Step 1: Write failing config tests**

In `tests/research/core/test_config.py`, update `test_default_entry_meta_model_config_exists()` to include:

```python
assert cfg.entry_meta_model.feature_scope == "runtime_safe"
```

In `test_entry_meta_model_config_overrides_applied()`, add `feature_scope = research_full` to the INI text:

```ini
[entry_meta_model]
model_kind = tabular
feature_scope = research_full
top_bucket_quantile = 0.85
min_samples = 41
min_oos_samples = 12
min_class_samples = 13
threshold_grid = 0.45,0.50,0.75
```

Add this assertion:

```python
assert cfg.entry_meta_model.feature_scope == "research_full"
```

- [ ] **Step 2: Write failing CLI tests**

In `tests/research/entry_meta/test_entry_meta_lab_cli.py`, update `test_entry_meta_lab_help_exposes_required_options()`:

```python
assert "--feature-scope" in completed.stdout
```

In `test_entry_meta_lab_stdout_uses_wrapped_payload(...)`, capture the `EntryMetaLab.run(...)` kwargs by adding this list before `FakeEntryMetaLab`:

```python
run_calls: list[dict[str, object]] = []
```

Replace the fake `run` method with:

```python
def run(self, **kwargs):
    run_calls.append(dict(kwargs))
    return SimpleNamespace(to_dict=lambda: result_payload)
```

Add `--feature-scope`, `research_full` to the monkeypatched `sys.argv` list after the `--backend cpu` pair:

```python
"--feature-scope",
"research_full",
```

After `entry_meta_lab.main()`, add:

```python
assert run_calls
assert run_calls[0]["feature_scope"] == "research_full"
```

- [ ] **Step 3: Run the focused config and CLI tests to verify failure**

Run:

```powershell
python -m pytest tests\research\core\test_config.py::TestResearchConfigDefaults::test_default_entry_meta_model_config_exists tests\research\core\test_config.py::TestLoadResearchConfigFromIni::test_entry_meta_model_config_overrides_applied tests\research\entry_meta\test_entry_meta_lab_cli.py -q
```

Expected: FAIL because `EntryMetaModelConfig.feature_scope` and `--feature-scope` do not exist yet.

- [ ] **Step 4: Add config field and loader support**

In `src/research/core/config.py`, update `EntryMetaModelConfig`:

```python
@dataclass(frozen=True)
class EntryMetaModelConfig:
    model_kind: str = "tabular"
    feature_scope: str = "runtime_safe"
    top_bucket_quantile: float = 0.80
    min_samples: int = 40
    min_oos_samples: int = 10
    min_class_samples: int = 10
    threshold_grid: List[float] = field(
        default_factory=lambda: [0.50, 0.55, 0.60, 0.65, 0.70]
    )
```

In the `EntryMetaModelConfig(...)` loader block, add:

```python
feature_scope=_get("entry_meta_model", "feature_scope", "runtime_safe"),
```

directly after `model_kind=...`.

- [ ] **Step 5: Add default INI setting**

In `config/research.ini`, under `[entry_meta_model]`, add:

```ini
# runtime_safe 只使用 backtest/runtime 可重建的 entry + current indicators + regime/session 特征。
# research_full 保留 FeatureHub 全量特征离线探索，但 lookup miss 不支持 dynamic scorer。
feature_scope = runtime_safe
```

- [ ] **Step 6: Add CLI option and pass-through**

In `src/ops/cli/entry_meta_lab.py`, add parser option after `--backend`:

```python
parser.add_argument(
    "--feature-scope",
    choices=["runtime_safe", "research_full"],
    default=None,
    help="Feature scope for Entry Meta training; default comes from research.ini",
)
```

In the `EntryMetaLab(...).run(...)` call, add:

```python
feature_scope=args.feature_scope,
```

- [ ] **Step 7: Run focused tests**

Run:

```powershell
python -m pytest tests\research\core\test_config.py::TestResearchConfigDefaults::test_default_entry_meta_model_config_exists tests\research\core\test_config.py::TestLoadResearchConfigFromIni::test_entry_meta_model_config_overrides_applied tests\research\entry_meta\test_entry_meta_lab_cli.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit Task 1**

Run:

```powershell
git add -- src/research/core/config.py config/research.ini src/ops/cli/entry_meta_lab.py tests/research/core/test_config.py tests/research/entry_meta/test_entry_meta_lab_cli.py
git commit -m "feat: add entry meta feature scope config"
```

Expected: commit contains only Task 1 files.

---

### Task 2: Runtime-Safe Feature Builder

**Files:**
- Modify: `src/research/entry_meta/features.py`
- Modify: `tests/research/entry_meta/test_features.py`

- [ ] **Step 1: Write failing runtime-safe feature tests**

In `tests/research/entry_meta/test_features.py`, add:

```python
def test_runtime_safe_feature_scope_keeps_only_allowed_runtime_indicators() -> None:
    matrix = SimpleNamespace(
        bar_times=[datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)],
        indicator_series={
            ("ema", "value"): [1.5],
            ("rsi", "value"): [55.0],
            ("temporal", "rsi_cross_up"): [1.0],
            ("microstructure", "body_ratio"): [0.25],
            ("session_event", "is_london"): [1.0],
            ("trade", "pnl_estimate"): [99.0],
        },
        regimes=["trend"],
        sessions=["london"],
    )
    trade = {
        "entry_time": "2026-01-01T00:00:00Z",
        "confidence": 0.80,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "pnl": 10.0,
    }

    features = EntryMetaFeatureBuilder(
        feature_scope="runtime_safe",
        runtime_indicator_names=["ema", "rsi"],
    ).build(matrix, _dataset([trade], [0]))

    assert features.feature_keys == [
        "entry.confidence",
        "entry.direction.buy",
        "entry.direction.sell",
        "entry.price",
        "entry.strategy_code",
        "indicator.ema.value",
        "indicator.rsi.value",
        "matrix.regime_code",
        "matrix.session_code",
    ]
    assert features.manifest["feature_scope"] == "runtime_safe"
    assert features.manifest["dynamic_scoring_supported"] is True
    assert features.manifest["runtime_indicator_names"] == ["ema", "rsi"]
    assert "indicator.temporal.rsi_cross_up" not in features.feature_keys
    assert "indicator.microstructure.body_ratio" not in features.feature_keys
    assert "indicator.session_event.is_london" not in features.feature_keys
    assert "indicator.trade.pnl_estimate" not in features.feature_keys


def test_research_full_feature_scope_keeps_visible_provider_features() -> None:
    matrix = SimpleNamespace(
        bar_times=[datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)],
        indicator_series={
            ("ema", "value"): [1.5],
            ("temporal", "rsi_cross_up"): [1.0],
            ("trade", "pnl_estimate"): [99.0],
        },
        regimes=["trend"],
        sessions=["london"],
    )
    trade = {
        "entry_time": "2026-01-01T00:00:00Z",
        "confidence": 0.80,
        "direction": "buy",
        "entry_price": 1.2345,
        "strategy": "breakout",
        "pnl": 10.0,
    }

    features = EntryMetaFeatureBuilder(feature_scope="research_full").build(
        matrix,
        _dataset([trade], [0]),
    )

    assert "indicator.ema.value" in features.feature_keys
    assert "indicator.temporal.rsi_cross_up" in features.feature_keys
    assert "indicator.trade.pnl_estimate" not in features.feature_keys
    assert features.manifest["feature_scope"] == "research_full"
    assert features.manifest["dynamic_scoring_supported"] is False
    assert features.manifest["runtime_indicator_names"] == []


def test_feature_builder_rejects_unknown_feature_scope() -> None:
    with pytest.raises(ValueError, match="unsupported entry meta feature_scope"):
        EntryMetaFeatureBuilder(feature_scope="unknown")
```

- [ ] **Step 2: Run feature tests to verify failure**

Run:

```powershell
python -m pytest tests\research\entry_meta\test_features.py -q
```

Expected: FAIL because `EntryMetaFeatureBuilder` does not accept `feature_scope` yet.

- [ ] **Step 3: Implement feature-scope filtering**

In `src/research/entry_meta/features.py`, add constants below `ENTRY_FEATURE_KEYS`:

```python
FEATURE_SCOPE_RUNTIME_SAFE = "runtime_safe"
FEATURE_SCOPE_RESEARCH_FULL = "research_full"
SUPPORTED_FEATURE_SCOPES = {
    FEATURE_SCOPE_RUNTIME_SAFE,
    FEATURE_SCOPE_RESEARCH_FULL,
}
```

Update `EntryMetaFeatureBuilder.__init__`:

```python
def __init__(
    self,
    category_mappings: dict[str, dict[str, float]] | None = None,
    *,
    feature_scope: str = FEATURE_SCOPE_RESEARCH_FULL,
    runtime_indicator_names: list[str] | tuple[str, ...] | set[str] | None = None,
) -> None:
    self._feature_scope = _normalize_feature_scope(feature_scope)
    self._runtime_indicator_names = tuple(
        sorted({str(name).strip() for name in runtime_indicator_names or [] if str(name).strip()})
    )
    self._frozen_category_mappings = (
        {
            category: {str(name): float(code) for name, code in mapping.items()}
            for category, mapping in category_mappings.items()
        }
        if category_mappings is not None
        else None
    )
```

Replace `_visible_indicator_keys` with:

```python
def _visible_indicator_keys(self, matrix: Any) -> list[tuple[str, str]]:
    keys = []
    runtime_allowed = set(self._runtime_indicator_names)
    for indicator, field in matrix.indicator_series.keys():
        indicator_name = str(indicator)
        field_name = str(field)
        key_text = f"{indicator_name}.{field_name}".lower()
        if any(token in key_text for token in FORBIDDEN_TOKENS):
            continue
        if (
            self._feature_scope == FEATURE_SCOPE_RUNTIME_SAFE
            and indicator_name not in runtime_allowed
        ):
            continue
        keys.append((indicator_name, field_name))
    return sorted(keys, key=lambda item: (item[0], item[1]))
```

In the manifest returned by `build(...)`, add:

```python
"feature_scope": self._feature_scope,
"dynamic_scoring_supported": self._feature_scope == FEATURE_SCOPE_RUNTIME_SAFE,
"runtime_indicator_names": list(self._runtime_indicator_names),
```

Add helper near `_semantic_name`:

```python
def _normalize_feature_scope(value: Any) -> str:
    scope = str(value or "").strip().lower()
    if scope not in SUPPORTED_FEATURE_SCOPES:
        raise ValueError(f"unsupported entry meta feature_scope {value}")
    return scope
```

- [ ] **Step 4: Run feature tests**

Run:

```powershell
python -m pytest tests\research\entry_meta\test_features.py -q
```

Expected: all feature tests pass.

- [ ] **Step 5: Commit Task 2**

Run:

```powershell
git add -- src/research/entry_meta/features.py tests/research/entry_meta/test_features.py
git commit -m "feat: add entry meta runtime-safe features"
```

Expected: commit contains only Task 2 files.

---

### Task 3: Lab And Training Runtime Indicator Contract

**Files:**
- Modify: `src/research/entry_meta/lab.py`
- Modify: `src/research/entry_meta/training.py`
- Modify: `tests/research/entry_meta/test_lab.py`
- Modify: `tests/research/entry_meta/test_training.py`

- [ ] **Step 1: Write failing Lab tests**

In `tests/research/entry_meta/test_lab.py`, update the baseline fixture in `test_lab_trains_from_raw_results_trades_and_writes_artifact(...)` to include the capability plan:

```python
baseline = {
    "raw_results": [
        {
            "strategy_capability_execution_plan": {
                "required_indicators_union": ["ema", "rsi"]
            },
            "trades": [
                _trade(matrix.bar_times[index], pnl, index)
                for index, pnl in enumerate(
                    [10.0, -8.0, 7.0, -3.0, 9.0, -4.0, 6.0, -2.0]
                )
            ],
        }
    ]
}
```

After reading the artifact JSON, add:

```python
assert artifact["feature_manifest"]["feature_scope"] == "runtime_safe"
assert artifact["feature_manifest"]["dynamic_scoring_supported"] is True
assert artifact["feature_manifest"]["runtime_indicator_names"] == ["ema", "rsi"]
assert "indicator.provider.score" not in artifact["feature_keys"]
```

Add this fail-fast test:

```python
def test_lab_runtime_safe_requires_baseline_required_indicator_union(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.research.entry_meta import lab

    matrix = _matrix()
    baseline = {
        "raw_results": [
            {
                "trades": [
                    _trade(matrix.bar_times[index], pnl, index)
                    for index, pnl in enumerate(
                        [10.0, -8.0, 7.0, -3.0, 9.0, -4.0, 6.0, -2.0]
                    )
                ]
            }
        ]
    }
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    monkeypatch.setattr(lab, "build_data_matrix", lambda **kwargs: matrix)

    class _FeatureHub:
        def __init__(self, config: ResearchConfig) -> None:
            self.config = config

        def required_extra_data(self) -> list[object]:
            return []

        def compute_all(self, matrix, extra_data=None):  # noqa: ANN001
            return FeatureComputeResult()

    monkeypatch.setattr(lab, "FeatureHub", _FeatureHub)

    with pytest.raises(ValueError, match="required_indicators_union"):
        lab.EntryMetaLab(
            config=ResearchConfig(),
            deps=SimpleNamespace(name="deps"),
        ).run(
            baseline_path=baseline_path,
            symbol="XAUUSD",
            timeframe="H1",
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
            backend_name="cpu",
            artifact_dir=tmp_path / "artifacts",
            model_id="entry-meta-test",
            feature_scope="runtime_safe",
        )
```

Add `import pytest` near the top of `tests/research/entry_meta/test_lab.py`.

- [ ] **Step 2: Write failing training tests**

In `tests/research/entry_meta/test_training.py`, add:

```python
def test_training_runtime_safe_passes_indicator_allowlist_to_feature_builder() -> None:
    matrix = _matrix()
    matrix.indicator_series[("provider", "score")] = [0.1] * matrix.n_bars
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0, 9.0, -4.0, 6.0, -2.0])

    bundle = train_entry_meta_bundle(
        matrix,
        dataset,
        "cpu",
        feature_scope="runtime_safe",
        runtime_indicator_names=["ema"],
        min_samples=8,
        min_oos_samples=4,
        min_class_samples=4,
    )

    assert "indicator.ema.value" in bundle.artifact.feature_keys
    assert "indicator.rsi.value" not in bundle.artifact.feature_keys
    assert "indicator.provider.score" not in bundle.artifact.feature_keys
    assert bundle.artifact.feature_manifest["feature_scope"] == "runtime_safe"
    assert bundle.artifact.feature_manifest["dynamic_scoring_supported"] is True
    assert bundle.artifact.feature_manifest["runtime_indicator_names"] == ["ema"]


def test_training_runtime_safe_requires_indicator_feature_columns() -> None:
    matrix = _matrix()
    dataset = _dataset(matrix, [10.0, -8.0, 7.0, -3.0, 9.0, -4.0, 6.0, -2.0])

    with pytest.raises(ValueError, match="runtime_safe.*indicator"):
        train_entry_meta_bundle(
            matrix,
            dataset,
            "cpu",
            feature_scope="runtime_safe",
            runtime_indicator_names=[],
        )
```

- [ ] **Step 3: Run Lab and training tests to verify failure**

Run:

```powershell
python -m pytest tests\research\entry_meta\test_lab.py tests\research\entry_meta\test_training.py -q
```

Expected: FAIL because Lab and training do not accept/pass `feature_scope` yet.

- [ ] **Step 4: Implement baseline input extraction in Lab**

In `src/research/entry_meta/lab.py`, add dataclass below `EntryMetaLabResult`:

```python
@dataclass(frozen=True)
class EntryMetaBaselineInput:
    trades: list[dict[str, Any]]
    runtime_indicator_names: list[str]
```

Update `EntryMetaLab.run(...)` signature:

```python
feature_scope: str | None = None,
```

Inside `run(...)`, replace:

```python
baseline_trades = _load_baseline_trades(Path(baseline_path))
```

with:

```python
baseline_input = _load_baseline_input(Path(baseline_path))
effective_feature_scope = str(
    feature_scope or self._config.entry_meta_model.feature_scope
).strip().lower()
if effective_feature_scope == "runtime_safe" and not baseline_input.runtime_indicator_names:
    raise ValueError(
        "Entry Meta runtime_safe feature scope requires baseline "
        "raw_results[0].strategy_capability_execution_plan.required_indicators_union"
    )
```

Replace uses of `baseline_trades` with `baseline_input.trades`.

In the `train_entry_meta_bundle(...)` call, add:

```python
feature_scope=effective_feature_scope,
runtime_indicator_names=baseline_input.runtime_indicator_names,
```

Replace `_load_baseline_trades(...)` with:

```python
def _load_baseline_input(path: Path) -> EntryMetaBaselineInput:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("baseline JSON must be an object")

    trades: list[dict[str, Any]] = []
    runtime_indicator_names: list[str] = []
    raw_results = payload.get("raw_results")
    if isinstance(raw_results, list):
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            if not runtime_indicator_names:
                runtime_indicator_names = _extract_required_indicators(item)
            trades.extend(_coerce_trades(item.get("trades")))

    if not trades:
        trades.extend(_coerce_trades(payload.get("trades")))

    if not trades:
        raise ValueError("baseline JSON must contain raw_results[].trades or trades")
    return EntryMetaBaselineInput(
        trades=trades,
        runtime_indicator_names=runtime_indicator_names,
    )


def _extract_required_indicators(raw_result: dict[str, Any]) -> list[str]:
    plan = raw_result.get("strategy_capability_execution_plan")
    if not isinstance(plan, dict):
        return []
    raw_names = plan.get("required_indicators_union")
    if not isinstance(raw_names, list):
        return []
    return sorted({str(name).strip() for name in raw_names if str(name).strip()})
```

- [ ] **Step 5: Implement training feature-scope pass-through**

In `src/research/entry_meta/training.py`, update `train_entry_meta_bundle(...)` signature:

```python
feature_scope: str = "research_full",
runtime_indicator_names: list[str] | tuple[str, ...] | set[str] | None = None,
```

Replace:

```python
features = EntryMetaFeatureBuilder().build(matrix=matrix, dataset=dataset)
```

with:

```python
features = EntryMetaFeatureBuilder(
    feature_scope=feature_scope,
    runtime_indicator_names=runtime_indicator_names,
).build(matrix=matrix, dataset=dataset)
_validate_runtime_safe_features(features)
```

Add helper below `_validate_training_contract(...)`:

```python
def _validate_runtime_safe_features(features: EntryMetaFeatureMatrix) -> None:
    if features.manifest.get("feature_scope") != "runtime_safe":
        return
    indicator_features = [
        key for key in features.feature_keys if str(key).startswith("indicator.")
    ]
    if not indicator_features:
        raise ValueError(
            "entry meta runtime_safe feature scope must include at least one indicator feature"
        )
```

- [ ] **Step 6: Run Lab and training tests**

Run:

```powershell
python -m pytest tests\research\entry_meta\test_lab.py tests\research\entry_meta\test_training.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 3**

Run:

```powershell
git add -- src/research/entry_meta/lab.py src/research/entry_meta/training.py tests/research/entry_meta/test_lab.py tests/research/entry_meta/test_training.py
git commit -m "feat: train entry meta runtime-safe artifacts"
```

Expected: commit contains only Task 3 files.

---

### Task 4: Overlay Dynamic Scope Gate

**Files:**
- Modify: `src/research/entry_meta/overlay.py`
- Modify: `tests/backtesting/test_entry_meta_overlay.py`
- Modify: `tests/research/entry_meta/test_artifacts.py`

- [ ] **Step 1: Update dynamic artifact fixture and add scope-gate tests**

In `tests/backtesting/test_entry_meta_overlay.py`, update `_dynamic_artifact(...)` feature manifest to include:

```python
feature_manifest={
    "feature_scope": "runtime_safe",
    "dynamic_scoring_supported": True,
    "runtime_indicator_names": ["atr14"],
    "category_mappings": {
        "strategy": {"breakout": 0.0},
        "regime": {"trend": 0.0},
        "session": {"london": 0.0},
    },
},
```

Add a research-full fixture:

```python
def _research_full_artifact(*, status: str = "accepted") -> EntryMetaArtifact:
    artifact = _dynamic_artifact(status=status)
    return dataclasses.replace(
        artifact,
        feature_manifest={
            "feature_scope": "research_full",
            "dynamic_scoring_supported": False,
            "category_mappings": artifact.feature_manifest["category_mappings"],
        },
    )
```

Add tests:

```python
def test_overlay_research_full_lookup_miss_is_allowed_and_reports_scope() -> None:
    overlay = EntryMetaBacktestOverlay(
        _research_full_artifact(),
        mode="filter",
        threshold=0.50,
    )

    verdict = overlay.evaluate(
        "2026-01-01T03:00:00Z",
        "breakout",
        "buy",
        confidence=0.9,
        feature_context=_feature_context(confidence=0.9),
    )

    assert verdict.allowed is True
    assert verdict.reason == "entry_meta_dynamic_feature_scope_unsupported"
    assert verdict.score_source == "missing"
    report = overlay.report()
    assert report["feature_scope"] == "research_full"
    assert report["dynamic_scoring_supported"] is False
    assert report["missing_by_reason"] == {
        "entry_meta_dynamic_feature_scope_unsupported": 1
    }
    assert report["dynamic_scored"] == 0


def test_overlay_rejects_inconsistent_dynamic_scope_manifest() -> None:
    artifact = dataclasses.replace(
        _dynamic_artifact(),
        feature_manifest={
            "feature_scope": "research_full",
            "dynamic_scoring_supported": True,
            "category_mappings": {
                "strategy": {"breakout": 0.0},
                "regime": {"trend": 0.0},
                "session": {"london": 0.0},
            },
        },
    )

    with pytest.raises(ValueError, match="dynamic_scoring_supported"):
        EntryMetaBacktestOverlay(artifact, mode="shadow")
```

- [ ] **Step 2: Add artifact roundtrip assertion**

In `tests/research/entry_meta/test_artifacts.py`, in `test_entry_meta_artifact_roundtrip(...)`, change `feature_manifest={"source": "test"}` to:

```python
feature_manifest={
    "source": "test",
    "feature_scope": "runtime_safe",
    "dynamic_scoring_supported": True,
    "runtime_indicator_names": ["atr14"],
},
```

Add assertions after loading payload:

```python
assert payload["feature_manifest"]["feature_scope"] == "runtime_safe"
assert payload["feature_manifest"]["dynamic_scoring_supported"] is True
assert payload["feature_manifest"]["runtime_indicator_names"] == ["atr14"]
```

- [ ] **Step 3: Run overlay/artifact tests to verify failure**

Run:

```powershell
python -m pytest tests\backtesting\test_entry_meta_overlay.py tests\research\entry_meta\test_artifacts.py -q
```

Expected: FAIL because overlay does not gate dynamic scoring by feature scope or report `feature_scope` yet.

- [ ] **Step 4: Implement overlay scope gate**

In `src/research/entry_meta/overlay.py`, in `__init__`, after `_predictions` initialization, add:

```python
feature_manifest = getattr(artifact, "feature_manifest", {})
feature_scope = "research_full"
dynamic_scoring_supported = False
category_mappings = {}
if isinstance(feature_manifest, dict):
    feature_scope = str(feature_manifest.get("feature_scope") or "research_full")
    dynamic_scoring_supported = bool(
        feature_manifest.get("dynamic_scoring_supported", False)
    )
    category_mappings = dict(feature_manifest.get("category_mappings", {}))
if dynamic_scoring_supported and feature_scope != "runtime_safe":
    raise ValueError(
        "Entry meta artifact dynamic_scoring_supported requires "
        f"feature_scope=runtime_safe; got {feature_scope}"
    )
self._feature_scope = feature_scope
self._dynamic_scoring_supported = (
    dynamic_scoring_supported and feature_scope == "runtime_safe"
)
self._dynamic_unsupported_reason = (
    ""
    if self._dynamic_scoring_supported
    else "entry_meta_dynamic_feature_scope_unsupported"
)
```

Replace the current `try:` block that derives `feature_manifest` and `category_mappings` with:

```python
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
    except (EntryMetaFeatureBuildError, EntryMetaScoringError, TypeError, ValueError):
        self._feature_row_builder = None
        self._scorer = None
        self._dynamic_unsupported_reason = "entry_meta_unsupported_scorer"
```

In `report(...)`, add:

```python
"feature_scope": self._feature_scope,
"dynamic_scoring_supported": self._dynamic_scoring_supported,
```

At the start of `_score_dynamic(...)`, after feature context check:

```python
if not self._dynamic_scoring_supported:
    return self._dynamic_unsupported_reason
```

- [ ] **Step 5: Run overlay/artifact tests**

Run:

```powershell
python -m pytest tests\backtesting\test_entry_meta_overlay.py tests\research\entry_meta\test_artifacts.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 4**

Run:

```powershell
git add -- src/research/entry_meta/overlay.py tests/backtesting/test_entry_meta_overlay.py tests/research/entry_meta/test_artifacts.py
git commit -m "feat: gate entry meta dynamic scoring by feature scope"
```

Expected: commit contains only Task 4 files.

---

### Task 5: Documentation And Runtime-Safe Verification

**Files:**
- Modify: `docs/codebase-review.md`
- No source code edits in this task unless prior task review finds a defect.

- [ ] **Step 1: Update codebase review notes**

In `docs/codebase-review.md`, in the `2026-05-03 Entry Meta-Label Overlay Lab` section, add:

```markdown
- 2026-05-03：Entry Meta 新增 `feature_scope` 合同。`runtime_safe` artifact 只训练 entry、当前 indicators、regime、session 等 backtest/runtime 可重建特征，并允许 lookup miss 后 dynamic scorer；`research_full` artifact 保留 FeatureHub 全量特征离线探索，但 lookup miss 会以 `entry_meta_dynamic_feature_scope_unsupported` 放行并计入 coverage report。该边界避免把离线 FeatureHub 特征误当成可实时计算概率。
```

- [ ] **Step 2: Run focused unit and regression tests**

Run:

```powershell
python -m pytest tests\research\core\test_config.py tests\research\entry_meta tests\backtesting\test_entry_meta_overlay.py tests\backtesting\test_entry_meta_session_context.py -q
```

Expected: all selected Entry Meta tests pass.

Run:

```powershell
python -m pytest tests\backtesting\test_state_edge_overlay.py tests\research\state_edge -q
```

Expected: State Edge tests pass. If State Edge paths fail due to pre-existing untracked State Edge work, capture the failure output and do not edit State Edge files in this task.

- [ ] **Step 3: Run CLI help smoke**

Run:

```powershell
python -m src.ops.cli.entry_meta_lab --help
python -m src.ops.cli.backtest_runner --help
```

Expected: `entry_meta_lab --help` includes `--feature-scope`; `backtest_runner --help` still includes `--entry-meta-artifact`, `--entry-meta-mode`, and `--entry-meta-threshold-grid`.

- [ ] **Step 4: Train H1 runtime-safe artifact**

Run:

```powershell
python -m src.ops.cli.entry_meta_lab --environment live --baseline artifacts/state_edge_overlay_h1_20260101_20260415_baseline.json --tf H1 --start 2026-01-01 --end 2026-04-15 --backend cpu --feature-scope runtime_safe --artifact-dir artifacts/entry_meta_h1_20260101_20260415_runtime_safe --json-output artifacts/entry_meta_h1_20260101_20260415_runtime_safe_train.json --no-auto-backfill
```

Expected: command exits 0. The resulting JSON has:

```text
result.feature_manifest.feature_scope = runtime_safe
result.feature_manifest.dynamic_scoring_supported = true
result.feature_manifest.runtime_indicator_names is not empty
```

Check with:

```powershell
@'
import json
from pathlib import Path
p = Path("artifacts/entry_meta_h1_20260101_20260415_runtime_safe_train.json")
data = json.loads(p.read_text(encoding="utf-8"))
manifest = data["result"]["feature_manifest"]
assert manifest["feature_scope"] == "runtime_safe"
assert manifest["dynamic_scoring_supported"] is True
assert manifest["runtime_indicator_names"]
print(data["result"]["status"], manifest["runtime_indicator_names"])
'@ | python -
```

- [ ] **Step 5: Run forward shadow to verify dynamic scorer coverage**

Run:

```powershell
$artifact = (Get-ChildItem -Path artifacts\entry_meta_h1_20260101_20260415_runtime_safe -Recurse -Filter entry_meta_artifact.json | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
python -m src.ops.cli.backtest_runner --environment live --tf H1 --start 2026-04-15 --end 2026-04-30 --include-demo-validation --entry-meta-artifact $artifact --entry-meta-mode shadow --entry-meta-threshold-grid 0.50 --json-output artifacts/entry_meta_runtime_safe_forward_h1_20260415_0430_shadow.json --no-auto-backfill
```

Expected: command exits 0 and shadow metrics match baseline for the same window.

Check coverage with:

```powershell
@'
import json
from pathlib import Path
p = Path("artifacts/entry_meta_runtime_safe_forward_h1_20260415_0430_shadow.json")
data = json.loads(p.read_text(encoding="utf-8"))
overlay = data["results"][0]["entry_meta_overlay"]
assert overlay["score_source_counts"].get("dynamic_scorer", 0) > 0
assert overlay["dynamic_scored"] > 0
print(overlay)
'@ | python -
```

- [ ] **Step 6: Run whitespace check**

Run:

```powershell
git diff --check -- src/research/core/config.py config/research.ini src/ops/cli/entry_meta_lab.py src/research/entry_meta/features.py src/research/entry_meta/lab.py src/research/entry_meta/training.py src/research/entry_meta/overlay.py tests/research/core/test_config.py tests/research/entry_meta/test_entry_meta_lab_cli.py tests/research/entry_meta/test_features.py tests/research/entry_meta/test_lab.py tests/research/entry_meta/test_training.py tests/backtesting/test_entry_meta_overlay.py tests/research/entry_meta/test_artifacts.py docs/codebase-review.md
```

Expected: no output.

- [ ] **Step 7: Commit Task 5**

Run:

```powershell
git add -- docs/codebase-review.md
git commit -m "docs: record entry meta runtime-safe feature scope"
```

Expected: commit contains only `docs/codebase-review.md`.

---

## Self-Review

- Spec coverage: Task 1 covers config and CLI; Task 2 implements feature-scope filtering; Task 3 wires baseline capability plan into training; Task 4 gates overlay dynamic scorer by manifest; Task 5 verifies docs, tests, runtime-safe training, and forward dynamic scorer coverage.
- Red-flag scan: no incomplete task descriptions remain; each code and verification step has exact files, commands, and expected outcomes.
- Type consistency: the plan consistently uses `feature_scope`, `runtime_indicator_names`, `dynamic_scoring_supported`, `runtime_safe`, `research_full`, and `entry_meta_dynamic_feature_scope_unsupported`.
