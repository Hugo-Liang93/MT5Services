from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.trading.recovery import (
    BoundedRecoveryController,
    RecoveryCycleState,
    RecoveryMarketSnapshot,
    RecoveryPolicy,
)


def _state(**overrides) -> RecoveryCycleState:
    base_time = datetime(2026, 5, 6, 1, 0, tzinfo=timezone.utc)
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
        "started_at": base_time,
        "updated_at": base_time,
        "last_step_at": base_time,
    }
    values.update(overrides)
    return RecoveryCycleState(**values)


def _policy(**overrides) -> RecoveryPolicy:
    values = {
        "enabled": True,
        "base_volume": 0.01,
        "multiplier": 2.0,
        "max_steps": 3,
        "max_total_volume": 0.15,
        "max_next_volume": 0.08,
        "step_distance_points": 100.0,
        "recovery_target_points": 20.0,
        "point": 0.01,
        "min_step_interval_ms": 0,
    }
    values.update(overrides)
    return RecoveryPolicy(**values)


def _snapshot(**overrides) -> RecoveryMarketSnapshot:
    values = {
        "symbol": "XAUUSD",
        "bid": 98.9,
        "ask": 99.0,
        "time": datetime(2026, 5, 6, 1, 0, 1, tzinfo=timezone.utc),
        "time_msc": 1_000,
    }
    values.update(overrides)
    return RecoveryMarketSnapshot(**values)


def test_controller_opens_next_step_when_adverse_move_crosses_trigger() -> None:
    controller = BoundedRecoveryController()

    decision = controller.evaluate_next_step(
        _policy(),
        _state(),
        _snapshot(),
        now=datetime(2026, 5, 6, 1, 0, 1, tzinfo=timezone.utc),
    )

    assert decision.action == "open_step"
    assert decision.step_index == 1
    assert decision.volume == 0.02
    assert decision.entry_price == 99.0
    assert decision.reason == "adverse_move_triggered"
    assert decision.metadata["adverse_move_points"] == 110.0


def test_controller_holds_before_trigger_or_cooldown() -> None:
    controller = BoundedRecoveryController()
    state = _state(last_step_at=datetime(2026, 5, 6, 1, 0, tzinfo=timezone.utc))

    before_trigger = controller.evaluate_next_step(
        _policy(),
        state,
        _snapshot(bid=99.4, ask=99.5),
        now=datetime(2026, 5, 6, 1, 0, 1, tzinfo=timezone.utc),
    )
    assert before_trigger.action == "hold"
    assert before_trigger.reason == "adverse_move_below_trigger"

    cooldown = controller.evaluate_next_step(
        _policy(min_step_interval_ms=2_000),
        state,
        _snapshot(),
        now=datetime(2026, 5, 6, 1, 0, 1, tzinfo=timezone.utc),
    )
    assert cooldown.action == "hold"
    assert cooldown.reason == "step_cooldown_active"


def test_controller_holds_step_when_adverse_move_is_overextended() -> None:
    controller = BoundedRecoveryController()

    decision = controller.evaluate_next_step(
        _policy(
            step_distance_points=80.0,
            max_step_adverse_move_points=120.0,
        ),
        _state(last_step_at=datetime(2026, 5, 6, 1, 0, tzinfo=timezone.utc)),
        _snapshot(bid=98.5, ask=98.6),
        now=datetime(2026, 5, 6, 1, 0, 1, tzinfo=timezone.utc),
    )

    assert decision.action == "hold"
    assert decision.reason == "step_adverse_move_overextended"
    assert decision.metadata["adverse_move_points"] == 150.0
    assert decision.metadata["max_step_adverse_move_points"] == 120.0


def test_controller_blocks_when_quote_side_missing_or_limits_exceeded() -> None:
    controller = BoundedRecoveryController()
    now = datetime(2026, 5, 6, 1, 0, 2, tzinfo=timezone.utc)

    missing_side = controller.evaluate_next_step(
        _policy(),
        _state(),
        _snapshot(bid=None),
        now=now,
    )
    assert missing_side.action == "block"
    assert missing_side.reason == "quote_side_missing"

    max_steps = controller.evaluate_next_step(
        _policy(max_steps=2),
        _state(step_count=2, last_step_at=now - timedelta(seconds=10)),
        _snapshot(),
        now=now,
    )
    assert max_steps.action == "block"
    assert max_steps.reason == "max_steps_reached"

    max_volume = controller.evaluate_next_step(
        _policy(max_total_volume=0.02),
        _state(total_volume=0.01, last_step_at=now - timedelta(seconds=10)),
        _snapshot(),
        now=now,
    )
    assert max_volume.action == "block"
    assert max_volume.reason == "max_total_volume_exceeded"
