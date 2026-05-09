# Recovery Runner Cycle Pacing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the real demo recovery runner from opening too many martingale cycles too quickly while keeping the existing exit model unchanged.

**Architecture:** Add cycle pacing as a recovery runner admission concern before direction, cost, calibration, and dispatch checks. The runner derives pacing facts from the existing recovery cycle state store, exposes a structured status payload, and records pacing holds as healthy `hold` decisions.

**Tech Stack:** Python dataclasses, FastAPI runtime config models, existing recovery cycle state repository, pytest.

---

## File Structure

- Modify `src/trading/recovery/runner.py`: add settings, pacing snapshot helpers, initial-cycle pacing gate, and status projection.
- Modify `src/config/risk.py`: normalize new runner settings from INI.
- Modify `src/config/models/runtime.py`: add Pydantic config fields.
- Modify `config/risk.ini`: document default disabled pacing fields.
- Modify `config/instances/demo-main/risk.local.ini`: set bounded demo pacing values while keeping `dry_run=true`.
- Modify `tests/trading/recovery/test_recovery_runner.py`: add unit tests for all pacing gates and status.
- Modify `docs/codebase-review.md`: record the cycle pacing improvement and remaining exit-policy work.

## Task 1: Add Pacing Settings and Tests

- [ ] **Step 1: Write failing tests**

Add tests to `tests/trading/recovery/test_recovery_runner.py`:

```python
def test_runner_holds_initial_during_min_cycle_interval():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "recent-cycle",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_dry_run",
            "metadata": {"initial_result": {"dry_run": True}},
            "started_at": _now().replace(minute=59).isoformat(),
            "closed_at": _now().replace(minute=59, second=30).isoformat(),
        }
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(min_cycle_interval_seconds=120.0),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot())

    assert result["action"] == "hold"
    assert result["reason"] == "cycle_min_interval_active"
    assert trading.dispatch_calls == []
    assert runner.status()["pacing"]["remaining_seconds"] == 60.0


def test_runner_holds_initial_during_close_cooldown():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "closed-cycle",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_dry_run",
            "metadata": {"initial_result": {"dry_run": True}},
            "started_at": _now().replace(minute=55).isoformat(),
            "closed_at": _now().replace(minute=59, second=30).isoformat(),
        }
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(cooldown_after_cycle_close_seconds=90.0),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot())

    assert result["action"] == "hold"
    assert result["reason"] == "cycle_close_cooldown_active"
    assert trading.dispatch_calls == []
    assert runner.status()["pacing"]["remaining_seconds"] == 60.0


def test_runner_holds_initial_when_hourly_cycle_cap_is_reached():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "cycle-a",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_dry_run",
            "metadata": {"initial_result": {"dry_run": True}},
            "started_at": _now().replace(minute=15).isoformat(),
        },
        {
            "cycle_id": "cycle-b",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_dry_run",
            "metadata": {"initial_result": {"dry_run": True}},
            "started_at": _now().replace(minute=45).isoformat(),
        },
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(max_cycles_per_hour=2),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot())

    assert result["action"] == "hold"
    assert result["reason"] == "max_cycles_per_hour_reached"
    assert trading.dispatch_calls == []
    assert runner.status()["pacing"]["hourly_cycle_count"] == 2
```

- [ ] **Step 2: Run failing tests**

Run:

```powershell
python -m pytest tests\trading\recovery\test_recovery_runner.py -q
```

Expected before implementation: failures because `RecoveryRuntimeRunnerSettings` does not accept the new pacing fields and status has no `pacing`.

## Task 2: Implement Runner Pacing Contract

- [ ] **Step 1: Add settings**

Modify `RecoveryRuntimeRunnerSettings` in `src/trading/recovery/runner.py`:

```python
min_cycle_interval_seconds: float = 0.0
cooldown_after_cycle_close_seconds: float = 0.0
max_cycles_per_hour: int = 0
```

Validate all three are non-negative in `__post_init__`.

- [ ] **Step 2: Add pacing helper**

Add a private helper that reads `list_recovery_cycle_states()`, filters resident recovery rows for the current symbol and strategy, excludes dry-run rows when the runner is real, and returns:

- latest started time
- latest closed time
- unique cycle count since `now - 1 hour`
- next allowed time and reason

If the state store raises, fail closed for configured pacing by returning a hold reason and storing `_last_error`.

- [ ] **Step 3: Gate initial cycles**

Call the pacing helper at the start of `_open_initial_cycle()`, immediately after session/day caps and before direction selection. If a pacing hold is active, return `_record_decision(action="hold", reason=<reason>, status="hold", execution={"pacing": <snapshot>})`.

- [ ] **Step 4: Expose status**

Add `pacing` to `RecoveryRuntimeRunner.status()` with configuration, latest timestamps, hourly count, next-cycle time, and remaining seconds.

- [ ] **Step 5: Verify tests pass**

Run:

```powershell
python -m pytest tests\trading\recovery\test_recovery_runner.py -q
```

Expected: all recovery runner tests pass.

## Task 3: Wire Config

- [ ] **Step 1: Add Pydantic fields**

Modify `RecoveryRuntimeRunnerConfig` in `src/config/models/runtime.py` with the same three fields and defaults.

- [ ] **Step 2: Normalize INI keys**

Add:

```python
"max_cycles_per_hour"
```

to `_RECOVERY_RUNNER_INT_KEYS` and:

```python
"min_cycle_interval_seconds",
"cooldown_after_cycle_close_seconds",
```

to `_RECOVERY_RUNNER_FLOAT_KEYS` in `src/config/risk.py`.

- [ ] **Step 3: Update config defaults**

Add documented disabled defaults to `config/risk.ini`:

```ini
min_cycle_interval_seconds = 0
cooldown_after_cycle_close_seconds = 0
max_cycles_per_hour = 0
```

Set safer demo-main local values in `config/instances/demo-main/risk.local.ini` while keeping `dry_run = true`:

```ini
min_cycle_interval_seconds = 300
cooldown_after_cycle_close_seconds = 300
max_cycles_per_hour = 2
```

- [ ] **Step 4: Verify config tests**

Run:

```powershell
python -m pytest tests\config\test_risk_config.py tests\trading\recovery\test_recovery_runner.py -q
```

Expected: tests pass.

## Task 4: Document Review Ledger and Demo Verification

- [ ] **Step 1: Update review ledger**

Append to `docs/codebase-review.md` that recovery runner pacing is implemented and cycle-level exit policy remains separate follow-up work.

- [ ] **Step 2: Restart demo-main in safe mode**

Keep `dry_run=true`, restart the demo-main process, and verify:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8811/v1/monitoring/health/ready
Invoke-RestMethod -Uri http://127.0.0.1:8811/v1/trade/state
```

Expected:

- ready status remains `ready`
- recovery runner is running
- `dry_run=true`
- `pacing` appears in runner status
- no positions or orders are opened

