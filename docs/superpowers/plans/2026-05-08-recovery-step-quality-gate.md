# Recovery Step Quality Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure recovery/martingale scaling steps use the same explicit cost-quality contract as initial entries, so the runner cannot add exposure during wide-spread or net-negative execution conditions.

**Architecture:** Keep strategy-quality decisions in `src/trading/recovery/entry_policies.py`, not in the runner. The runner remains an application coordinator: it asks the controller for a step decision, asks the cost gate whether that step is tradable, then either dispatches or records a structured hold. Existing `max_entry_spread_points`, `slippage_budget_points`, `commission_points`, and `min_net_profit_points` apply to every market entry, including recovery scaling steps.

**Tech Stack:** Python dataclasses, pytest, existing recovery runner/controller tests.

---

### Task 1: Extend RecoveryCostGate to Step Entries

**Files:**
- Modify: `src/trading/recovery/entry_policies.py`
- Test: `tests/trading/recovery/test_recovery_entry_policies.py`

- [ ] **Step 1: Write failing tests**

Add tests asserting `RecoveryCostGate.assess_step()` rejects wide spread and accepts normal spread using the existing cost metadata contract.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests\trading\recovery\test_recovery_entry_policies.py::test_cost_gate_blocks_wide_spread_for_recovery_step -q`

Expected: FAIL because `RecoveryCostGate` has no `assess_step`.

- [ ] **Step 3: Implement minimal policy method**

Add `assess_step(policy, cycle, decision, snapshot)` that reuses the same cost calculation as initial entries, adds `scope=recovery_step`, `cycle_id`, `step_index`, and `current_step_count` metadata, and returns `RecoveryCostDecision`.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests\trading\recovery\test_recovery_entry_policies.py -q`

Expected: all entry policy tests pass.

### Task 2: Wire Step Quality Gate into Runner

**Files:**
- Modify: `src/trading/recovery/runner.py`
- Test: `tests/trading/recovery/test_recovery_runner.py`

- [ ] **Step 1: Write failing runner test**

Add a test with an open recovery cycle and a tick snapshot whose adverse move triggers a step, but whose spread exceeds `max_entry_spread_points`. Expected result: `action=hold`, `reason=step_spread_too_wide`, no trade dispatch, no state mutation, analytics records a blocked cost-gate decision.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests\trading\recovery\test_recovery_runner.py::test_runner_holds_recovery_step_when_spread_is_too_wide -q`

Expected: FAIL because the runner currently dispatches the step.

- [ ] **Step 3: Implement runner wiring**

After `evaluate_next_step()` returns `open_step`, call `RecoveryCostGate.assess_step()`. If blocked, record a structured hold with cost-gate payload and do not call `_open_recovery_step()`.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests\trading\recovery\test_recovery_runner.py::test_runner_holds_recovery_step_when_spread_is_too_wide tests\trading\recovery\test_recovery_runner.py::test_runner_applies_dry_run_recovery_step_to_loaded_cycle -q`

Expected: both pass.

### Task 3: Regression and Documentation

**Files:**
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Run recovery regression**

Run: `python -m pytest tests\trading\recovery\test_recovery_entry_policies.py tests\trading\recovery\test_recovery_runner.py -q`

Expected: all pass.

- [ ] **Step 2: Compile recovery module**

Run: `python -m compileall src\trading\recovery`

Expected: exit code 0.

- [ ] **Step 3: Document architecture change**

Append a dated note to `docs/codebase-review.md` describing the new step-quality gate and its module boundaries.
