# Recovery Risk Budget Account Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make recovery/martingale loss budgets account-scoped so one account's cycle losses cannot block or mask another account's admission decisions.

**Architecture:** `RecoveryRiskBudgetGuard` remains the domain service for loss-budget admission. The runner passes its public `account_key` into the guard, and the guard filters persisted recovery cycle rows by account key before computing daily, rolling, and consecutive loss metrics.

**Tech Stack:** Python dataclasses, pytest, existing recovery state-store port.

---

### Task 1: Add Account-Scope Regression Tests

**Files:**
- Create: `tests/trading/recovery/test_recovery_risk_budget.py`
- Modify: `tests/trading/recovery/test_recovery_runner.py`

- [ ] **Step 1: Write failing guard test**

Add a test where another account has a loss above budget and the current account has no losses. Expected: guard allows the current account and reports zero daily loss.

- [ ] **Step 2: Write failing runner test**

Add a test where `DummyStateStore` returns another account's loss row. Expected: runner opens or continues admission instead of holding with `recovery_daily_loss_budget_reached`.

### Task 2: Implement Account Key Contract

**Files:**
- Modify: `src/trading/recovery/risk_budget.py`
- Modify: `src/trading/recovery/runner.py`

- [ ] **Step 1: Add `account_key` to `RecoveryRiskBudgetGuard.assess()`**

Pass `account_key` through `_load_rows()` and `_row_matches_resident_recovery()`.

- [ ] **Step 2: Include account key in risk-budget snapshots**

Add `account_key` to the returned snapshot so monitoring can confirm the budget scope.

- [ ] **Step 3: Wire runner calls**

Both `_assess_risk_budget()` and `_risk_budget_status()` pass `self._account_key`.

### Task 3: Verify and Document

**Files:**
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Run targeted tests**

Run: `python -m pytest tests\trading\recovery\test_recovery_risk_budget.py tests\trading\recovery\test_recovery_runner.py -q`

- [ ] **Step 2: Run broader recovery suite**

Run: `python -m pytest tests\trading\recovery\test_recovery_entry_policies.py tests\trading\recovery\test_recovery_exposure_ledger.py tests\trading\recovery\test_recovery_risk_budget.py tests\trading\recovery\test_recovery_runner.py tests\config\test_risk_config.py -q`

- [ ] **Step 3: Append architecture note to codebase review**

Document that recovery loss budget is now strategy + symbol + account scoped.
