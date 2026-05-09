# Recovery Step Overextension Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent recovery/martingale scaling from adding exposure after the adverse move has already overextended beyond a configured step-quality boundary.

**Architecture:** Keep the decision in `BoundedRecoveryController` because it decides whether a cycle should open the next step. Expose the threshold through `RecoveryPolicy` and `RecoveryRuntimeRunnerSettings`, then load it from `risk.ini[recovery_runtime_runner]`. The runner remains an application coordinator and does not hand-code adverse-move formulas.

**Tech Stack:** Python dataclasses, Pydantic config models, pytest.

---

### Task 1: Controller Policy Gate

**Files:**
- Modify: `src/trading/recovery/models.py`
- Modify: `src/trading/recovery/controller.py`
- Test: `tests/trading/recovery/test_bounded_recovery_controller.py`

- [ ] **Step 1: Write failing controller test**

Add a test where `step_distance_points=80`, `max_step_adverse_move_points=120`, and adverse move is 150 points. Expected: `hold`, reason `step_adverse_move_overextended`.

- [ ] **Step 2: Implement `RecoveryPolicy.max_step_adverse_move_points`**

Default to `0.0`; validate `>= 0`.

- [ ] **Step 3: Implement controller gate**

After adverse move crosses trigger, hold if `max_step_adverse_move_points > 0` and adverse move is greater than that threshold.

### Task 2: Runtime and Config Wiring

**Files:**
- Modify: `src/trading/recovery/runner.py`
- Modify: `src/config/models/runtime.py`
- Modify: `src/config/risk.py`
- Modify: `config/risk.ini`
- Test: `tests/config/test_risk_config.py`
- Test: `tests/trading/recovery/test_recovery_runner.py`

- [ ] **Step 1: Add setting to runner settings and `to_recovery_policy()`**

Expose `max_step_adverse_move_points` in status under `step_policy`.

- [ ] **Step 2: Add config parser/model support**

Add the field to `RecoveryRuntimeRunnerConfig` and `_RECOVERY_RUNNER_FLOAT_KEYS`.

- [ ] **Step 3: Document default config**

Add `max_step_adverse_move_points = 0` with comments in `config/risk.ini`.

### Task 3: Regression and Documentation

**Files:**
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Run targeted tests**

Run: `python -m pytest tests\trading\recovery\test_bounded_recovery_controller.py tests\trading\recovery\test_recovery_runner.py tests\config\test_risk_config.py -q`

- [ ] **Step 2: Run recovery suite**

Run: `python -m pytest tests\trading\recovery\test_bounded_recovery_controller.py tests\trading\recovery\test_recovery_entry_policies.py tests\trading\recovery\test_recovery_exposure_ledger.py tests\trading\recovery\test_recovery_risk_budget.py tests\trading\recovery\test_recovery_runner.py tests\config\test_risk_config.py -q`

- [ ] **Step 3: Compile and document**

Run: `python -m compileall src\trading\recovery`

Append the architecture note to `docs/codebase-review.md`.
