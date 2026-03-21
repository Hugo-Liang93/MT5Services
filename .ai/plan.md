# XAUUSD Trading Next-Step Optimization Plan

Last updated: 2026-03-21

## Current Baseline

The current XAUUSD single-symbol trading module has completed the following:

- idempotent trade replay by `request_id`
- MT5-state recovery after retry / timeout
- startup position rehydrate via `PositionManager.sync_open_positions()`
- real `fill_price` and `close_price` backfill
- manual trade control via `/trade/control`
- manual reconcile via `/trade/reconcile`
- stricter defaults for economic trade guard and required SL
- **broker-aware precheck**: trade_mode / stops_level / freeze_level validation
- **close-result completeness**: unresolved close 终态记录 + close_source 审计

Current test baseline:

- `pytest -q`
- result: `448 passed`

---

## Completed

### 1. Broker-Aware Precheck ✅

Priority: `P0` — Completed 2026-03-21

Implementation:

- `SymbolInfo` extended with `stops_level`, `freeze_level`, `trade_mode`
- `MT5TradingClient.check_broker_constraints()` — structured check results
- `validate_trade_request()` raises on broker constraint violations
- `TradingService.precheck_trade()` integrated with structured broker checks + warnings
- Fixed `0 or default` falsy bug for `trade_mode=0` (DISABLED)
- Fixed `precheck_trade` warnings/checks not merged into risk assessment

Tests: `tests/clients/test_broker_constraints.py` (26 tests)

---

### 2. Close-Result Completeness ✅

Priority: `P0` — Completed 2026-03-21

Implementation:

- `TradeOutcomeTracker.on_position_closed()`: `close_price=None` now records unresolved terminal state (was silently dropped)
- `close_source` metadata: `"history_deals"` / `"mt5_missing"` / `"manual_reconcile"` / `"position_closed"`
- `PositionManager` sets `pos._close_source` based on close price recovery result
- `summary()` exposes `unresolved_closes` count
- Unresolved trades still written to DB for post-hoc audit

Tests: `tests/trading/test_trade_outcome_tracker.py` (12 tests)

---

## Priority Summary

### P1: Next Production Hardening

1. Recovery and control-path audit completeness (downgraded from P0 — current system already has startup sync and reconcile, this is observability improvement)
2. Execution-quality reporting
3. Richer operator controls

### P2: Position Management

1. Partial close / scale-out management

### P3: Broader architecture cleanup

1. Signal/trading plan documentation consolidation
2. Historical replay and audit analytics
3. Future multi-symbol abstraction only after XAUUSD is stable

---

## Backlog (Detailed)

### 3. Recovery and Audit Completeness

Priority: `P1` (downgraded from P0 — existing system handles recovery; this adds structured metadata)

Goal:

- make restart/retry recovery explainable and auditable

Work:

- persist recovery events with structured metadata:
  - `recovered_from_state`
  - `state_source`
  - `restored_by_startup_sync`
- enrich `resolve_position_context()` output with:
  - original entry source
  - strategy metadata completeness flag
- include reconcile summary in monitoring API

---

### 4. Execution Quality Reporting

Priority: `P1`

Goal:

- distinguish signal quality from execution quality

Work:

- add metrics for:
  - average slippage
  - max adverse slippage
  - recovered-trade count
  - risk-block count
  - order rejection count
- expose via:
  - `TradeExecutor.status()`
  - `TradingModule.monitoring_summary()`

---

### 5. Operator Controls

Priority: `P1`

Goal:

- make live intervention safer and simpler

Work:

- add optional control flags for:
  - pause by symbol
  - disable trailing only
  - disable new entries but keep existing management on
  - forced rescan of current MT5 state
- document operator playbooks:
  - NFP mode
  - CPI mode
  - emergency close-only mode

---

### 6. Partial Close and Scale-Out

Priority: `P2`

Goal:

- improve real intraday XAUUSD position management

Work:

- define staged exit policy:
  - partial take-profit
  - breakeven move after first scale-out
  - trailing only on runner
- add explicit tracked-position state for:
  - scaled_out
  - runner_volume
  - first_target_hit

---

### 7. Test Coverage Improvement Plan

Priority: `P1`

Status review:

- covered well:
  - idempotent replay
  - auto-entry pause
  - startup sync for managed positions
  - manual positions skipped on recovery
  - close price backfill
  - `/trade/control`
  - `/trade/reconcile`
  - `/trade/from-signal` auto-entry semantics
  - **broker precheck rejection matrix** ✅
  - **unresolved close state reporting** ✅
- still weaker:
  - audit-based idempotent replay path
  - partial-close lifecycle
  - startup sync observability beyond local state
  - operator-mode matrix tests

---

## Exit Criteria For Next Iteration

The next optimization iteration is complete when:

- ~~broker rejection causes are mostly caught pre-send~~ ✅
- ~~every tracked trade has a terminal observable outcome~~ ✅
- restart/reconcile behavior is fully auditable
- operator controls cover normal XAUUSD live intervention cases
- new tests are added for all newly introduced control/recovery branches
