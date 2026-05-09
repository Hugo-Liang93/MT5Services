from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.market.tick_features.models import TickFeatureSnapshot
from src.trading.recovery.entry_policies import (
    RecoveryCostGate,
    RecoveryDirectionPolicy,
    RecoveryEntrySignalConfirmer,
    RecoveryExitModel,
)
from src.trading.recovery.models import (
    RecoveryCycleState,
    RecoveryDecision,
    RecoveryMarketSnapshot,
    RecoveryPolicy,
)


def _policy(**overrides) -> RecoveryPolicy:
    values = {
        "enabled": True,
        "base_volume": 0.01,
        "multiplier": 2.0,
        "max_steps": 1,
        "max_total_volume": 0.03,
        "max_next_volume": 0.02,
        "step_distance_points": 80.0,
        "recovery_target_points": 30.0,
        "point": 0.01,
        "direction_mode": "auto",
        "min_directional_move_points": 5.0,
        "min_pressure_delta": 0.2,
        "max_entry_spread_points": 25.0,
        "slippage_budget_points": 2.0,
        "commission_points": 1.0,
        "min_net_profit_points": 5.0,
    }
    values.update(overrides)
    return RecoveryPolicy(**values)


def _feature(**overrides) -> TickFeatureSnapshot:
    values = {
        "symbol": "XAUUSD",
        "window_start_msc": 1_000,
        "window_end_msc": 2_000,
        "generated_at": datetime(2026, 5, 6, 12, tzinfo=timezone.utc),
        "tick_count": 10,
        "bid": 100.00,
        "ask": 100.08,
        "last": 100.04,
        "mid": 100.04,
        "spread_points": 8.0,
        "quote_age_ms": 100,
        "realized_range_points": 18.0,
        "price_change_points": 8.0,
        "buy_pressure": 0.65,
        "sell_pressure": 0.35,
        "status": "healthy",
        "reasons": (),
    }
    values.update(overrides)
    return TickFeatureSnapshot(**values)


def _snapshot(**overrides) -> RecoveryMarketSnapshot:
    values = {
        "symbol": "XAUUSD",
        "bid": 100.0,
        "ask": 100.08,
        "time": datetime(2026, 5, 6, 12, tzinfo=timezone.utc),
        "time_msc": 2_000,
    }
    values.update(overrides)
    return RecoveryMarketSnapshot(**values)


def _cycle(**overrides) -> RecoveryCycleState:
    values = {
        "cycle_id": "cycle-1",
        "account_key": "demo:broker:1001",
        "symbol": "XAUUSD",
        "direction": "buy",
        "status": "open",
        "base_volume": 0.01,
        "total_volume": 0.01,
        "step_count": 0,
        "average_entry_price": 100.0,
        "last_entry_price": 100.0,
        "started_at": datetime(2026, 5, 6, 12, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 6, 12, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return RecoveryCycleState(**values)


def test_direction_policy_selects_buy_or_sell_from_tick_features() -> None:
    policy = _policy()
    direction_policy = RecoveryDirectionPolicy()

    buy = direction_policy.choose_direction(
        policy,
        _feature(price_change_points=8.0, buy_pressure=0.7, sell_pressure=0.3),
        fallback_direction="buy",
    )
    sell = direction_policy.choose_direction(
        policy,
        _feature(price_change_points=-9.0, buy_pressure=0.2, sell_pressure=0.8),
        fallback_direction="buy",
    )

    assert buy.direction == "buy"
    assert buy.reason == "auto_direction_buy"
    assert sell.direction == "sell"
    assert sell.reason == "auto_direction_sell"


def test_direction_policy_skips_when_signal_is_weak_or_pressure_conflicts() -> None:
    policy = _policy()
    direction_policy = RecoveryDirectionPolicy()

    weak = direction_policy.choose_direction(
        policy,
        _feature(price_change_points=2.0, buy_pressure=0.8, sell_pressure=0.2),
        fallback_direction="buy",
    )
    conflict = direction_policy.choose_direction(
        policy,
        _feature(price_change_points=9.0, buy_pressure=0.45, sell_pressure=0.55),
        fallback_direction="buy",
    )

    assert weak.direction is None
    assert weak.reason == "direction_signal_too_weak"
    assert conflict.direction is None
    assert conflict.reason == "direction_pressure_mismatch"


def test_direction_policy_skips_when_signal_is_overextended() -> None:
    direction_policy = RecoveryDirectionPolicy()

    overextended = direction_policy.choose_direction(
        _policy(max_directional_move_points=150.0),
        _feature(price_change_points=-180.0, buy_pressure=0.2, sell_pressure=0.8),
        fallback_direction="buy",
    )

    assert overextended.direction is None
    assert overextended.reason == "direction_signal_overextended"
    assert overextended.metadata["max_directional_move_points"] == 150.0


def test_entry_signal_confirmer_requires_consecutive_matching_snapshots() -> None:
    confirmer = RecoveryEntrySignalConfirmer()
    now = datetime(2026, 5, 6, 12, tzinfo=timezone.utc)

    first = confirmer.assess(
        direction="buy",
        observed_at=now,
        required_snapshots=2,
        max_gap_seconds=3.0,
    )
    second = confirmer.assess(
        direction="buy",
        observed_at=now + timedelta(seconds=1),
        required_snapshots=2,
        max_gap_seconds=3.0,
    )

    assert first.allowed is False
    assert first.reason == "entry_confirmation_pending"
    assert first.metadata["confirmation_count"] == 1
    assert second.allowed is True
    assert second.reason == "entry_confirmation_confirmed"
    assert second.metadata["confirmation_count"] == 2


def test_entry_signal_confirmer_resets_on_direction_change_or_gap() -> None:
    confirmer = RecoveryEntrySignalConfirmer()
    now = datetime(2026, 5, 6, 12, tzinfo=timezone.utc)

    first = confirmer.assess(
        direction="buy",
        observed_at=now,
        required_snapshots=2,
        max_gap_seconds=1.0,
    )
    direction_change = confirmer.assess(
        direction="sell",
        observed_at=now + timedelta(milliseconds=500),
        required_snapshots=2,
        max_gap_seconds=1.0,
    )
    gap_reset = confirmer.assess(
        direction="sell",
        observed_at=now + timedelta(seconds=3),
        required_snapshots=2,
        max_gap_seconds=1.0,
    )

    assert first.metadata["confirmation_count"] == 1
    assert direction_change.allowed is False
    assert direction_change.metadata["confirmation_count"] == 1
    assert gap_reset.allowed is False
    assert gap_reset.metadata["confirmation_count"] == 1


def test_cost_gate_blocks_wide_spread_or_insufficient_net_target() -> None:
    gate = RecoveryCostGate()

    wide = gate.assess_entry(
        _policy(max_entry_spread_points=5.0),
        direction="buy",
        snapshot=_feature(spread_points=8.0),
    )
    insufficient = gate.assess_entry(
        _policy(recovery_target_points=12.0, min_net_profit_points=5.0),
        direction="buy",
        snapshot=_feature(spread_points=8.0),
    )
    allowed = gate.assess_entry(_policy(), direction="sell", snapshot=_feature())

    assert wide.allowed is False
    assert wide.reason == "spread_too_wide"
    assert wide.metadata["required_points"] == 16.0
    assert wide.metadata["expected_net_points"] == 19.0
    assert wide.metadata["net_margin_points"] == 14.0
    assert wide.metadata["target_shortfall_points"] == 0.0
    assert insufficient.allowed is False
    assert insufficient.reason == "net_target_below_cost"
    assert insufficient.metadata["net_margin_points"] == -4.0
    assert insufficient.metadata["target_shortfall_points"] == 4.0
    assert allowed.allowed is True
    assert allowed.reason == "entry_cost_ok"


def test_cost_gate_blocks_wide_spread_for_recovery_step() -> None:
    gate = RecoveryCostGate()
    cycle = _cycle()
    decision = RecoveryDecision(
        action="open_step",
        reason="adverse_move_triggered",
        step_index=1,
        volume=0.02,
        entry_price=99.08,
    )

    blocked = gate.assess_step(
        _policy(max_entry_spread_points=5.0),
        cycle=cycle,
        decision=decision,
        snapshot=_feature(spread_points=8.0),
    )
    allowed = gate.assess_step(
        _policy(max_entry_spread_points=10.0),
        cycle=cycle,
        decision=decision,
        snapshot=_feature(spread_points=8.0),
    )

    assert blocked.allowed is False
    assert blocked.reason == "step_spread_too_wide"
    assert blocked.metadata["scope"] == "recovery_step"
    assert blocked.metadata["cycle_id"] == "cycle-1"
    assert blocked.metadata["step_index"] == 1
    assert blocked.metadata["spread_points"] == 8.0
    assert allowed.allowed is True
    assert allowed.reason == "step_cost_ok"


def test_exit_model_closes_only_after_cost_adjusted_net_target() -> None:
    exit_model = RecoveryExitModel()
    policy = _policy(
        recovery_target_points=30.0,
        slippage_budget_points=2.0,
        commission_points=0.0,
    )
    cycle = _cycle(average_entry_price=100.0, total_volume=0.01)

    hold = exit_model.evaluate_exit(policy, cycle, _snapshot(bid=100.31, ask=100.39))
    close = exit_model.evaluate_exit(policy, cycle, _snapshot(bid=100.32, ask=100.40))

    assert hold.action == "hold"
    assert hold.reason == "net_recovery_target_not_reached"
    assert close.action == "close_cycle"
    assert close.reason == "net_recovery_target_reached"
    assert close.metadata["net_profit_points"] == 30.0


def test_exit_model_closes_when_cycle_loss_limit_is_reached() -> None:
    exit_model = RecoveryExitModel()
    policy = _policy(
        recovery_target_points=100.0,
        slippage_budget_points=0.0,
        commission_points=0.0,
        max_cycle_loss_points=20.0,
    )
    cycle = _cycle(average_entry_price=100.0, total_volume=0.03)

    hold = exit_model.evaluate_exit(policy, cycle, _snapshot(bid=99.81, ask=99.89))
    close = exit_model.evaluate_exit(policy, cycle, _snapshot(bid=99.80, ask=99.88))

    assert hold.action == "hold"
    assert hold.reason == "net_recovery_target_not_reached"
    assert close.action == "close_cycle"
    assert close.reason == "cycle_loss_limit_reached"
    assert close.metadata["loss_points"] == 20.0
    assert close.metadata["max_cycle_loss_points"] == 20.0


def test_exit_model_closes_when_cycle_duration_is_reached() -> None:
    exit_model = RecoveryExitModel()
    now = datetime(2026, 5, 6, 12, tzinfo=timezone.utc)
    policy = _policy(
        recovery_target_points=100.0,
        max_cycle_duration_seconds=300.0,
    )
    cycle = _cycle(started_at=now - timedelta(seconds=301), updated_at=now)

    close = exit_model.evaluate_exit(policy, cycle, _snapshot(time=now))

    assert close.action == "close_cycle"
    assert close.reason == "max_cycle_duration_reached"
    assert close.metadata["cycle_age_seconds"] == 301.0
    assert close.metadata["max_cycle_duration_seconds"] == 300.0


def test_exit_model_can_close_after_max_steps_reached() -> None:
    exit_model = RecoveryExitModel()
    policy = _policy(
        recovery_target_points=100.0,
        max_steps=1,
        max_steps_exit_mode="close_cycle",
    )
    cycle = _cycle(step_count=1)

    close = exit_model.evaluate_exit(policy, cycle, _snapshot(bid=99.95, ask=100.03))

    assert close.action == "close_cycle"
    assert close.reason == "max_steps_exit_reached"
    assert close.metadata["step_count"] == 1
    assert close.metadata["max_steps"] == 1


def test_exit_model_closes_when_max_steps_hold_time_is_reached() -> None:
    exit_model = RecoveryExitModel()
    now = datetime(2026, 5, 6, 12, tzinfo=timezone.utc)
    policy = _policy(
        recovery_target_points=100.0,
        max_steps=1,
        max_steps_exit_mode="hold",
        max_steps_hold_seconds=10.0,
    )
    cycle = _cycle(
        step_count=1,
        last_step_at=now - timedelta(seconds=11),
    )

    close = exit_model.evaluate_exit(policy, cycle, _snapshot(time=now))

    assert close.action == "close_cycle"
    assert close.reason == "max_steps_hold_time_reached"
    assert close.metadata["max_steps_hold_seconds"] == 10.0
    assert close.metadata["max_steps_hold_elapsed_seconds"] == 11.0
