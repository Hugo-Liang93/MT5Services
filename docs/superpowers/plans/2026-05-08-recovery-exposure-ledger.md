# Recovery Exposure Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make demo recovery/martingale runner use an explicit exposure ledger so successful submitted tickets remain authoritative until broker position snapshots confirm or close them.

**Architecture:** Add a focused `RecoveryExposureLedger` under `src/trading/recovery/` and make `DemoBoundedRecoveryRunner` call it instead of directly interpreting submitted tickets and position provider state. The ledger owns pending/confirmed/absent exposure classification; runner remains responsible for cycle orchestration, state persistence, and execution dispatch.

**Tech Stack:** Python dataclasses, existing recovery runner/controller, pytest.

---

### Task 1: Add Exposure Ledger Contract

**Files:**
- Create: `src/trading/recovery/exposure_ledger.py`
- Test: `tests/trading/recovery/test_recovery_exposure_ledger.py`

- [ ] Write tests for:
  - submitted ticket is pending exposure when broker snapshot is stale or missing;
  - submitted ticket is confirmed when broker snapshot contains matching live position;
  - submitted ticket is absent only after a fresh broker snapshot confirms no matching position;
  - ledger status payload is serializable for runner health/status.

- [ ] Implement minimal ledger dataclasses:
  - `RecoveryExposureTicket`
  - `RecoveryExposureSnapshot`
  - `RecoveryExposureLedger`

- [ ] Run:
  - `python -m pytest tests\trading\recovery\test_recovery_exposure_ledger.py -q`

### Task 2: Integrate Ledger Into Runner

**Files:**
- Modify: `src/trading/recovery/runner.py`
- Test: `tests/trading/recovery/test_recovery_runner.py`

- [ ] Add failing tests for:
  - `_reconcile_submitted_cycle_exposure()` does not close a cycle while submitted ticket is still pending and broker snapshot is stale;
  - active status exposes pending submitted tickets even before broker snapshot confirms position;
  - close cleanup uses fresh ledger classification and does not fail on stale snapshot.

- [ ] Inject `RecoveryExposureLedger` into `DemoBoundedRecoveryRunner`.

- [ ] Replace direct fresh/no-match checks in:
  - `_reconcile_submitted_cycle_exposure()`
  - `_close_submitted_live_positions()`
  - `_active_matching_position_tickets()`

- [ ] Run:
  - `python -m pytest tests\trading\recovery\test_recovery_runner.py tests\trading\recovery\test_recovery_exposure_ledger.py -q`

### Task 3: Status And Documentation

**Files:**
- Modify: `src/trading/recovery/runner.py`
- Modify: `docs/codebase-review.md`

- [ ] Add `exposure_ledger` status under `recovery_runner`.

- [ ] Document the new contract:
  - submitted ticket is temporary truth after successful execution;
  - broker snapshot can confirm live exposure;
  - fresh empty broker snapshot can close pending exposure as absent;
  - stale snapshot must not be used to mark exposure absent.

- [ ] Run:
  - `python -m pytest tests\trading\recovery\test_recovery_entry_policies.py tests\trading\recovery\test_recovery_runner.py tests\trading\recovery\test_recovery_exposure_ledger.py tests\config\test_risk_config.py -q`
  - `python -m compileall src\trading\recovery`

### Self-Review

- This plan is scoped to exposure reconciliation only.
- It does not change live/default risk configs.
- It does not redesign entry/exit alpha logic; that becomes the next plan after exposure state is reliable.
