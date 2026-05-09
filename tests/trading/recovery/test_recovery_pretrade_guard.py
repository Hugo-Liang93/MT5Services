from datetime import datetime, timezone

from src.trading.recovery import (
    PositionScalingIntent,
    RecoveryCycleState,
    RecoveryPolicy,
    RecoveryPreTradeGuard,
)


def _policy(**overrides) -> RecoveryPolicy:
    values = {
        "enabled": True,
        "base_volume": 0.01,
        "multiplier": 2.0,
        "max_steps": 3,
        "max_total_volume": 0.08,
        "max_next_volume": 0.04,
        "step_distance_points": 100.0,
        "recovery_target_points": 20.0,
        "point": 0.01,
    }
    values.update(overrides)
    return RecoveryPolicy(**values)


def _cycle(**overrides) -> RecoveryCycleState:
    now = datetime(2026, 5, 6, 1, 2, 3, tzinfo=timezone.utc)
    values = {
        "cycle_id": "cycle-1",
        "account_key": "demo:broker:1001",
        "symbol": "XAUUSD",
        "direction": "buy",
        "status": "open",
        "base_volume": 0.01,
        "total_volume": 0.03,
        "step_count": 1,
        "average_entry_price": 2298.5,
        "last_entry_price": 2297.0,
        "started_at": now,
        "updated_at": now,
        "last_step_at": now,
        "strategy": "tick_recovery_probe",
        "timeframe": "TICK",
        "source_signal_id": "sig-1",
    }
    values.update(overrides)
    return RecoveryCycleState(**values)


def _intent(**overrides) -> PositionScalingIntent:
    values = {
        "cycle_id": "cycle-1",
        "account_key": "demo:broker:1001",
        "symbol": "XAUUSD",
        "direction": "buy",
        "strategy": "tick_recovery_probe",
        "timeframe": "TICK",
        "source_signal_id": "sig-1",
        "step_index": 2,
        "volume": 0.02,
        "entry_price": 2296.0,
        "reason": "adverse_move_triggered",
        "created_at": datetime(2026, 5, 6, 1, 2, 4, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return PositionScalingIntent(**values)


def test_recovery_pretrade_guard_allows_next_step_within_hard_caps():
    decision = RecoveryPreTradeGuard().assess(_policy(), _cycle(), _intent())

    assert decision.allowed is True
    assert decision.reason == "allowed"
    assert decision.details["projected_total_volume"] == 0.05
    assert decision.details["max_total_volume"] == 0.08


def test_recovery_pretrade_guard_blocks_unexpected_step_index():
    decision = RecoveryPreTradeGuard().assess(
        _policy(),
        _cycle(step_count=1),
        _intent(step_index=3),
    )

    assert decision.allowed is False
    assert decision.reason == "step_index_mismatch"
    assert decision.details["expected_step_index"] == 2
    assert decision.details["actual_step_index"] == 3


def test_recovery_pretrade_guard_blocks_volume_hard_cap_breaches():
    by_next_volume = RecoveryPreTradeGuard().assess(
        _policy(max_next_volume=0.015),
        _cycle(),
        _intent(volume=0.02),
    )
    by_total_volume = RecoveryPreTradeGuard().assess(
        _policy(max_total_volume=0.04),
        _cycle(total_volume=0.03),
        _intent(volume=0.02),
    )

    assert by_next_volume.allowed is False
    assert by_next_volume.reason == "max_next_volume_exceeded"
    assert by_total_volume.allowed is False
    assert by_total_volume.reason == "max_total_volume_exceeded"


def test_recovery_pretrade_guard_blocks_cycle_identity_or_status_mismatch():
    status_decision = RecoveryPreTradeGuard().assess(
        _policy(),
        _cycle(status="blocked"),
        _intent(),
    )
    account_decision = RecoveryPreTradeGuard().assess(
        _policy(),
        _cycle(),
        _intent(account_key="other:account"),
    )

    assert status_decision.allowed is False
    assert status_decision.reason == "cycle_not_open"
    assert account_decision.allowed is False
    assert account_decision.reason == "account_key_mismatch"
