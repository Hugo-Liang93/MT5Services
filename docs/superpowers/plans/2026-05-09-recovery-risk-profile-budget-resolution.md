# Recovery Risk Profile Budget Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `risk_profiles.recovery_budgeted` the effective source for resident recovery loss-budget controls.

**Architecture:** `src.config` loads raw profile and runner sections, `src.risk.profiles` validates and resolves named profile contracts, and `src.app_runtime.builder_phases.recovery` injects the resolved recovery budget into `RecoveryRuntimeRunnerSettings`. The recovery runner continues to own cycle-state decisions; it no longer depends on duplicated config interpretation.

**Tech Stack:** Python, Pydantic config models, pytest.

---

### Task 1: Runtime Wiring Test

**Files:**
- Modify: `tests/app_runtime/test_recovery_runtime_layer.py`

- [ ] Add a failing test where `[risk_profiles.recovery_budgeted]` sets `max_daily_recovery_loss_amount=25`, while `[recovery_runtime_runner]` leaves its budget defaults at `0`.
- [ ] Build the recovery runtime layer and assert `container.recovery_runner.status()["risk_budget"]["max_daily_recovery_loss_amount"] == 25`.
- [ ] Run `python -m pytest tests\app_runtime\test_recovery_runtime_layer.py::test_recovery_runtime_layer_applies_recovery_profile_budget -q` and confirm it fails because the runner still sees `0`.

### Task 2: Profile Budget Resolver

**Files:**
- Modify: `src/risk/profiles.py`
- Modify: `src/app_runtime/builder_phases/recovery.py`

- [ ] Add a small resolver function that reads `risk_config.recovery_runtime_runner.risk_profile`, verifies the profile exists and uses `policy=recovery_budgeted`, and returns recovery budget fields from the profile.
- [ ] In `build_recovery_runtime_layer()`, merge the resolved profile budget into the `RecoveryRuntimeRunnerSettings` payload before constructing the runner.
- [ ] Keep execution parameters such as volume, direction, cost gates and pacing in `[recovery_runtime_runner]`; only loss-budget fields come from the risk profile.

### Task 3: Config Contract Cleanup

**Files:**
- Modify: `config/risk.ini`
- Modify: `config/instances/demo-main/risk.local.ini`
- Modify: `docs/codebase-review.md`

- [ ] Update comments so `risk_profiles.recovery_budgeted` is documented as the source of recovery loss-budget controls.
- [ ] Leave current demo budget values at `0` because demo service is stopped and the user paused live testing.
- [ ] Document the module boundary and remaining decision: production budgets still need explicit non-zero values before live usage.

### Task 4: Verification

**Files:**
- Test: `tests/app_runtime/test_recovery_runtime_layer.py`
- Test: `tests/config/test_risk_config.py`
- Test: `tests/core/test_pretrade_risk_service.py`
- Test: `tests/trading/recovery/test_recovery_risk_budget.py`
- Test: `tests/trading/recovery/test_recovery_runner.py`

- [ ] Run the focused new test.
- [ ] Run the recovery/profile test set:
  `python -m pytest tests\app_runtime\test_recovery_runtime_layer.py tests\config\test_risk_config.py tests\core\test_pretrade_risk_service.py tests\trading\recovery\test_recovery_risk_budget.py tests\trading\recovery\test_recovery_runner.py -q`
- [ ] Run `python -m compileall src\risk src\app_runtime\builder_phases src\trading\recovery`.
