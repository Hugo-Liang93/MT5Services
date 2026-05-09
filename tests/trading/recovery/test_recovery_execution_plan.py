from datetime import datetime, timezone

import pytest

from src.trading.recovery import (
    PositionScalingIntent,
    RecoveryCycleState,
    RecoveryDecision,
    RecoveryExecutionPlan,
)


def _cycle() -> RecoveryCycleState:
    now = datetime(2026, 5, 6, 1, 2, 3, tzinfo=timezone.utc)
    return RecoveryCycleState(
        cycle_id="cycle-1",
        account_key="demo:broker:1001",
        symbol="XAUUSD",
        direction="buy",
        status="open",
        base_volume=0.01,
        total_volume=0.03,
        step_count=1,
        average_entry_price=2298.5,
        last_entry_price=2297.0,
        started_at=now,
        updated_at=now,
        last_step_at=now,
        strategy="tick_martingale_probe",
        timeframe="TICK",
        source_signal_id="sig-1",
        metadata={"cycle": "metadata"},
    )


def test_recovery_execution_plan_creates_position_scaling_intent_from_open_step():
    created_at = datetime(2026, 5, 6, 1, 2, 4, tzinfo=timezone.utc)
    decision = RecoveryDecision(
        action="open_step",
        reason="adverse_move_triggered",
        step_index=2,
        volume=0.02,
        entry_price=2296.0,
        metadata={"spread": 0.2},
    )

    plan = RecoveryExecutionPlan.from_decision(
        cycle=_cycle(),
        decision=decision,
        created_at=created_at,
    )

    assert plan.action == "open_step"
    assert plan.cycle_id == "cycle-1"
    assert plan.position_scaling_intent is not None
    intent = plan.position_scaling_intent
    assert intent.account_key == "demo:broker:1001"
    assert intent.symbol == "XAUUSD"
    assert intent.direction == "buy"
    assert intent.strategy == "tick_martingale_probe"
    assert intent.timeframe == "TICK"
    assert intent.source_signal_id == "sig-1"
    assert intent.step_index == 2
    assert intent.volume == pytest.approx(0.02)
    assert intent.entry_price == pytest.approx(2296.0)
    assert intent.reason == "adverse_move_triggered"
    assert intent.metadata == {"spread": 0.2}
    assert intent.created_at == created_at


def test_recovery_execution_plan_does_not_create_scaling_intent_for_hold_or_block():
    for action in ("hold", "block", "close_cycle"):
        plan = RecoveryExecutionPlan.from_decision(
            cycle=_cycle(),
            decision=RecoveryDecision(action=action, reason="not_actionable"),
            created_at=datetime(2026, 5, 6, 1, 2, 4, tzinfo=timezone.utc),
        )

        assert plan.action == action
        assert plan.position_scaling_intent is None


def test_position_scaling_intent_rejects_missing_account_or_invalid_size():
    now = datetime(2026, 5, 6, 1, 2, 4, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="account_key"):
        PositionScalingIntent(
            cycle_id="cycle-1",
            account_key="",
            symbol="XAUUSD",
            direction="buy",
            strategy="tick_martingale_probe",
            timeframe="TICK",
            step_index=1,
            volume=0.01,
            entry_price=2296.0,
            reason="adverse_move_triggered",
            created_at=now,
        )

    with pytest.raises(ValueError, match="volume"):
        PositionScalingIntent(
            cycle_id="cycle-1",
            account_key="demo:broker:1001",
            symbol="XAUUSD",
            direction="buy",
            strategy="tick_martingale_probe",
            timeframe="TICK",
            step_index=1,
            volume=0.0,
            entry_price=2296.0,
            reason="adverse_move_triggered",
            created_at=now,
        )


def test_recovery_execution_plan_rejects_open_step_without_entry_price():
    with pytest.raises(ValueError, match="entry_price"):
        RecoveryExecutionPlan.from_decision(
            cycle=_cycle(),
            decision=RecoveryDecision(
                action="open_step",
                reason="adverse_move_triggered",
                step_index=2,
                volume=0.02,
                entry_price=None,
            ),
            created_at=datetime(2026, 5, 6, 1, 2, 4, tzinfo=timezone.utc),
        )
