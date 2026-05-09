# Recovery Runner Cycle Pacing Design

## Objective

The real demo recovery runner must be able to run for hours without exhausting its cycle quota in a few minutes. The current runner can open a new cycle immediately after the previous cycle closes whenever entry gates pass. That is useful for a narrow execution smoke test, but it is not a stable demo burn-in mode.

This design adds an explicit cycle pacing contract. It does not change the martingale exit model in this step.

## Scope

In scope:

- Gate new initial recovery cycles with a minimum interval.
- Gate new initial recovery cycles with a cooldown after a cycle close.
- Gate new initial recovery cycles with an hourly cycle cap.
- Expose pacing state in `RecoveryRuntimeRunner.status()`.
- Cover the behavior with focused recovery runner tests.

Out of scope for this step:

- Changing net recovery target close behavior.
- Adding max cycle duration or cycle-level loss exits.
- Optimizing profitability or direction selection.

## Current Exit Behavior

The runner exits a cycle in two ways:

- `net_recovery_target_reached`: `RecoveryExitModel` computes net favorable points using the executable close side, subtracts slippage and commission budgets, and asks the runner to close live positions when the net target is reached.
- `submitted_exposure_absent`: if live submitted positions are no longer present, the runner marks the cycle closed and clears active state.

`max_steps_reached` is not an exit. It blocks further recovery steps and leaves the current cycle open until a target close or external/broker close is observed.

## Pacing Contract

Add these runner settings:

- `min_cycle_interval_seconds`: minimum elapsed time between the last opened cycle and the next initial cycle. `0` disables it.
- `cooldown_after_cycle_close_seconds`: minimum elapsed time after the latest closed cycle before opening a new initial cycle. `0` disables it.
- `max_cycles_per_hour`: maximum cycle starts within a rolling one-hour window. `0` disables it.

All three gates apply only when `_active_cycle is None`, before direction selection, cost gate, calibration guard, and dispatch retry logic. This keeps pacing as a cycle admission concern rather than a trading execution concern.

## State Source

Use the existing recovery cycle state store as the durable source for hourly and cross-restart decisions. The session counter remains useful for process-local burn-in limits, but pacing cannot rely only on memory because a restart should not erase hourly limits.

For an initial implementation, the repository can expose recent cycle records for the runner account, symbol, strategy, and timeframe. The runner will derive:

- latest started cycle time
- latest closed cycle time
- cycle count started since `now - 1 hour`

## Status Contract

`RecoveryRuntimeRunner.status()` should include:

- `pacing`: structured snapshot with configured limits, latest cycle timestamps, hourly count, `next_cycle_not_before`, and remaining seconds.
- `last_reason` values:
  - `cycle_min_interval_active`
  - `cycle_close_cooldown_active`
  - `max_cycles_per_hour_reached`

These reasons should be recorded as `hold`, not `block`, because the system is intentionally waiting and remains healthy.

## Testing

Add focused tests under `tests/trading/recovery/`:

- holds initial cycle during minimum cycle interval
- holds initial cycle during close cooldown
- holds initial cycle when hourly cap is reached
- exposes pacing status fields
- disabled pacing settings preserve existing behavior

## Follow-Up Exit Policy Work

After pacing is verified on demo, add a separate `CycleExitPolicy` design for:

- max cycle duration exit
- cycle-level loss cap exit
- explicit behavior after `max_steps_reached`
- close-only semantics for active cycles

