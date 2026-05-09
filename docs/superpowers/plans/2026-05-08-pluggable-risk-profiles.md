# Pluggable Risk Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add configurable risk profiles so ordinary K-line strategies and tick-derived recovery/martingale flows can use different risk controls.

**Architecture:** `src/config` owns the profile contract, `src/risk` resolves the active profile and controls common pre-trade rule composition, and `src/trading/recovery` owns recovery-cycle loss-budget facts. Recovery trades carry an explicit `risk_profile` in metadata; no generic risk rule special-cases a strategy name.

**Tech Stack:** Python, Pydantic config models, pytest, existing FastAPI/runtime wiring.

---

### Task 1: Risk Profile Config Contract

**Files:**
- Modify: `src/config/models/runtime.py`
- Modify: `src/config/risk.py`
- Modify: `src/config/centralized.py`
- Test: `tests/config/test_risk_config.py`

- [ ] Add failing tests proving `[risk_profiles.<name>]` and `[risk_profile_bindings]` normalize into `RiskConfig`.
- [ ] Add `RiskProfileConfig` with `policy`, `trade_frequency_enabled`, and recovery budget fields.
- [ ] Add `risk_profiles` and `risk_profile_bindings` to `RiskConfig`.
- [ ] Parse all `risk_profiles.*` sections from the merged risk config.
- [ ] Run `python -m pytest tests\config\test_risk_config.py -q`.

### Task 2: Pre-Trade Risk Profile Selection

**Files:**
- Create: `src/risk/profiles.py`
- Modify: `src/risk/service.py`
- Test: `tests/core/test_pretrade_risk_service.py`

- [ ] Add failing tests proving `risk_profile=recovery_budgeted` skips generic trade-frequency checks and reservations while `standard_kline` keeps them.
- [ ] Implement `RiskProfileResolver` that reads explicit intent metadata first, then configured strategy binding, then `standard_kline`.
- [ ] Add profile info to every assessment payload.
- [ ] Make `PreTradeRiskService` run `TradeFrequencyRule` and reservation only when the active profile enables trade frequency.
- [ ] Run `python -m pytest tests\core\test_pretrade_risk_service.py -q`.

### Task 3: Recovery Loss-Budget Guard

**Files:**
- Create: `src/trading/recovery/risk_budget.py`
- Modify: `src/trading/recovery/runner.py`
- Test: `tests/trading/recovery/test_recovery_runner.py`

- [ ] Add failing tests proving closed negative recovery cycles block new initial entries when daily or rolling budget is exhausted.
- [ ] Add failing tests proving runner status exposes the active risk profile and budget snapshot.
- [ ] Implement a recovery budget guard that reads only public `list_recovery_cycle_states()` rows and fails closed when state is unavailable.
- [ ] Gate new initial cycles before dispatch and record `recovery_daily_loss_budget_reached`, `recovery_rolling_loss_budget_reached`, or `recovery_consecutive_loss_lockout`.
- [ ] Run `python -m pytest tests\trading\recovery\test_recovery_runner.py -q`.

### Task 4: Recovery Execution Metadata

**Files:**
- Modify: `src/trading/recovery/canary.py`
- Modify: `src/trading/recovery/execution.py`
- Test: `tests/trading/recovery/test_recovery_runner.py`

- [ ] Add failing tests proving initial and step payload metadata include `risk_profile`.
- [ ] Pass `risk_profile` from runner settings into recovery initial metadata and step intent metadata.
- [ ] Run `python -m pytest tests\trading\recovery\test_recovery_runner.py -q`.

### Task 5: Defaults, Demo Config, Docs

**Files:**
- Modify: `config/risk.ini`
- Modify: `config/instances/demo-main/risk.local.ini`
- Modify: `docs/codebase-review.md`

- [ ] Add default `standard_kline` and `recovery_budgeted` profile sections.
- [ ] Bind `tick_martingale_probe = recovery_budgeted` in demo config.
- [ ] Document the changed module boundaries and remaining risks in `docs/codebase-review.md`.
- [ ] Run targeted pytest and compile checks.
