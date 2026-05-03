# State Edge Sequence Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a research-only evaluation pipeline that trains State Edge sequence artifacts, runs backtest overlay threshold grids, attaches shape-neighbor evidence, and marks each TF as `accepted`, `refit`, or `rejected`.

**Architecture:** Keep training, overlay, and evaluation as separate responsibilities. The evaluator consumes existing State Edge artifacts and `backtest_runner._run_single()` results through formal functions, then writes a JSON report. Demo/live runtime remains untouched.

**Tech Stack:** Python, NumPy, PyTorch for sequence artifacts, existing BacktestEngine overlay, pytest.

---

### Task 1: Evaluation Decision Model

**Files:**
- Create: `src/research/state_edge/evaluation.py`
- Test: `tests/research/state_edge/test_evaluation.py`

- [ ] **Step 1: Write failing tests**

```python
def test_decision_accepts_pf_or_expectancy_gain_without_dd_breach():
    baseline = {"metrics": {"pf": 1.10, "expectancy": 1.0, "max_dd": 5.0, "trades": 40}}
    overlay = {"metrics": {"pf": 1.30, "expectancy": 1.2, "max_dd": 5.4, "trades": 34}}
    decision = evaluate_overlay_increment(baseline, overlay)
    assert decision.status == "accepted"

def test_decision_rejects_drawdown_breach():
    baseline = {"metrics": {"pf": 1.10, "expectancy": 1.0, "max_dd": 5.0, "trades": 40}}
    overlay = {"metrics": {"pf": 1.40, "expectancy": 1.2, "max_dd": 6.0, "trades": 34}}
    decision = evaluate_overlay_increment(baseline, overlay)
    assert decision.status == "rejected"
```

- [ ] **Step 2: Implement `StateEdgeEvaluationDecision` and `evaluate_overlay_increment()`**

Implement `accepted` when PF or expectancy improves and max DD does not exceed baseline by more than 10%. Mark `refit` when trade count is too low or no clear gain. Mark `rejected` on DD breach or degraded PF/expectancy.

### Task 2: Threshold Grid Evaluator

**Files:**
- Modify: `src/research/state_edge/evaluation.py`
- Test: `tests/research/state_edge/test_evaluation.py`

- [ ] **Step 1: Write failing test for selecting best threshold**

```python
def test_threshold_report_selects_best_accepted_threshold():
    report = build_threshold_grid_report(
        baseline={"metrics": {"pf": 1.0, "expectancy": 1.0, "max_dd": 5.0, "trades": 40}},
        overlays=[...],
    )
    assert report.best_threshold == 0.55
    assert report.status == "accepted"
```

- [ ] **Step 2: Implement `ThresholdGridReport`**

Rank accepted thresholds by PF improvement, then expectancy improvement, then lower DD.

### Task 3: CLI Orchestrator

**Files:**
- Create: `src/ops/cli/state_edge_sequence_eval.py`
- Test: `tests/research/state_edge/test_sequence_eval_cli.py`

- [ ] **Step 1: Write CLI help/import smoke**

Ensure CLI exposes:
- `--environment`
- `--tf`
- `--start`
- `--end`
- `--backend`
- `--model-kind sequence_mlp`
- `--threshold-grid`
- `--json-output`
- `--include-demo-validation`

- [ ] **Step 2: Implement CLI**

For each TF:
1. train artifact via `StateEdgeLab`
2. run baseline backtest via `_run_single()`
3. run shadow overlay
4. run filter overlay for each threshold
5. build threshold report
6. write JSON

### Task 4: Documentation

**Files:**
- Modify: `docs/research-system.md`
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Document evaluator CLI**

Add command examples and decision gates.

- [ ] **Step 2: Record risk ledger update**

Record that evaluation remains research-only and that accepted TFs still need human review before any runtime work.

### Task 5: Verification

**Commands:**

```powershell
python -m pytest -q tests\research\state_edge tests\backtesting\test_state_edge_overlay.py tests\research\core\test_config.py
python -m src.ops.cli.state_edge_sequence_eval --help
git diff --check
```

Expected:
- all tests pass
- CLI help exits 0
- no whitespace errors
