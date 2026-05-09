# Risk Profile Rule Composition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each risk profile declare its active pre-trade rule set, so recovery/martingale and ordinary K-line strategies can use different risk controls through configuration.

**Architecture:** `RiskProfileConfig` owns the rule-set contract, `RiskProfileResolver` exposes the effective rule names in profile selection, and `PreTradeRiskService` filters its rule pipeline by public rule identifiers. The individual `RiskRule` classes remain unchanged and continue to own their own evaluation logic.

**Tech Stack:** Python, Pydantic config models, pytest.

---

### Task 1: Config Contract

**Files:**
- Modify: `src/config/models/runtime.py`
- Modify: `src/config/risk.py`
- Modify: `config/risk.ini`
- Test: `tests/config/test_risk_config.py`

- [ ] **Step 1: Write a failing config test**

```python
def test_risk_profile_pre_trade_rules_are_loaded() -> None:
    payload = normalize_risk_config_payload(
        {"enabled": "true"},
        risk_profile_sections={
            "recovery_budgeted": {
                "policy": "recovery_budgeted",
                "pre_trade_rules": "account_snapshot,margin_availability,protection",
            }
        },
    )
    cfg = RiskConfig.model_validate(payload)
    assert cfg.risk_profiles["recovery_budgeted"].pre_trade_rules == [
        "account_snapshot",
        "margin_availability",
        "protection",
    ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests\config\test_risk_config.py::test_risk_profile_pre_trade_rules_are_loaded -q`

- [ ] **Step 3: Add `pre_trade_rules` to `RiskProfileConfig` and config normalization**

Add the fixed rule identifiers:
`account_snapshot`, `daily_loss_limit`, `margin_availability`, `trade_frequency`,
`protection`, `session_window`, `market_structure`, `economic_event`,
`calendar_health`.

- [ ] **Step 4: Run the config test**

Run: `python -m pytest tests\config\test_risk_config.py -q`

### Task 2: Pre-Trade Rule Filtering

**Files:**
- Modify: `src/risk/profiles.py`
- Modify: `src/risk/service.py`
- Test: `tests/core/test_pretrade_risk_service.py`

- [ ] **Step 1: Write a failing risk-service test**

Create a custom profile that excludes `session_window` and `trade_frequency`, bind a strategy to it, then assert the service neither calls the frequency provider nor blocks outside the allowed session.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests\core\test_pretrade_risk_service.py::test_custom_profile_rule_list_controls_pre_trade_pipeline -q`

- [ ] **Step 3: Implement rule-name mapping in `PreTradeRiskService`**

Map rule classes to the fixed identifiers and skip rules not present in the active profile.

- [ ] **Step 4: Run the risk-service tests**

Run: `python -m pytest tests\core\test_pretrade_risk_service.py -q`

### Task 3: Documentation And Verification

**Files:**
- Modify: `docs/codebase-review.md`

- [ ] **Step 1: Document the boundary**

Record that profiles now own the rule-set contract while rule classes remain reusable components.

- [ ] **Step 2: Run focused verification**

Run:
`python -m pytest tests\config\test_risk_config.py tests\core\test_pretrade_risk_service.py tests\app_runtime\test_recovery_runtime_layer.py tests\trading\recovery\test_recovery_runner.py -q`

- [ ] **Step 3: Compile changed modules**

Run: `python -m compileall src\config src\risk src\app_runtime\builder_phases src\trading\recovery`
