# Recovery Risk Profile Status Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the effective recovery risk profile contract in `recovery_runner.status()` for demo monitoring and auditability.

**Architecture:** `src.risk.profiles` continues to resolve the profile contract, `src.app_runtime.builder_phases.recovery` passes that read-only contract into the recovery runner, and `DemoBoundedRecoveryRunner.status()` publishes it. The recovery runner does not interpret pre-trade rules; it only reports the contract selected at runtime.

**Tech Stack:** Python, pytest, existing runtime builder tests.

---

### Task 1: App Runtime Status Test

**Files:**
- Modify: `tests/app_runtime/test_recovery_runtime_layer.py`

- [ ] **Step 1: Write the failing test**

```python
def test_recovery_runtime_layer_exposes_risk_profile_contract():
    container, _, _ = _container()
    risk_config = _risk_config()
    build_recovery_runtime_layer(container, risk_config=risk_config)
    contract = container.recovery_runner.status()["risk_profile_contract"]
    assert contract["name"] == "recovery_budgeted"
    assert contract["policy"] == "recovery_budgeted"
    assert contract["trade_frequency_enabled"] is False
    assert "trade_frequency" not in contract["pre_trade_rules"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests\app_runtime\test_recovery_runtime_layer.py::test_recovery_runtime_layer_exposes_risk_profile_contract -q`

### Task 2: Runtime Wiring

**Files:**
- Modify: `src/risk/profiles.py`
- Modify: `src/app_runtime/builder_phases/recovery.py`
- Modify: `src/trading/recovery/runner.py`

- [ ] **Step 1: Add a resolver helper returning `RiskProfileSelection.to_dict()` for recovery runner status.**
- [ ] **Step 2: Pass the payload into `DemoBoundedRecoveryRunner` constructor.**
- [ ] **Step 3: Store it as read-only `_risk_profile_contract` and expose it in `status()`.**
- [ ] **Step 4: Run the failing test and confirm it passes.**

### Task 3: Documentation And Verification

**Files:**
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Document that recovery status now exposes profile rule-set contract.**
- [ ] **Step 2: Run focused verification**

Run:
`python -m pytest tests\app_runtime\test_recovery_runtime_layer.py tests\trading\recovery\test_recovery_runner.py tests\core\test_pretrade_risk_service.py tests\config\test_risk_config.py -q`

- [ ] **Step 3: Compile changed modules**

Run: `python -m compileall src\risk src\app_runtime\builder_phases src\trading\recovery src\config`
