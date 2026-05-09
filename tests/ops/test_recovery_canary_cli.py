from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.clients.mt5_market import Quote
from src.ops.cli import recovery_canary


def test_recovery_canary_policy_from_risk_config_requires_enabled_dry_run_and_stop():
    risk = SimpleNamespace(
        recovery_execution_canary=SimpleNamespace(
            enabled=True,
            dry_run=True,
            order_kind="market",
            deviation=12,
            magic=930001,
            comment_prefix="recovery-demo",
            protective_stop_points=120,
        )
    )

    policy = recovery_canary._canary_policy_from_risk_config(risk)
    errors = recovery_canary._validate_demo_canary_policy(policy)

    assert errors == []
    assert policy.enabled is True
    assert policy.dry_run is True
    assert policy.protective_stop_points == 120


def test_recovery_canary_policy_validation_blocks_disabled_or_real_order():
    disabled = recovery_canary._canary_policy_from_risk_config(
        SimpleNamespace(
            recovery_execution_canary=SimpleNamespace(
                enabled=False,
                dry_run=True,
                order_kind="market",
                protective_stop_points=120,
            )
        )
    )
    real_order = recovery_canary._canary_policy_from_risk_config(
        SimpleNamespace(
            recovery_execution_canary=SimpleNamespace(
                enabled=True,
                dry_run=False,
                order_kind="market",
                protective_stop_points=120,
            )
        )
    )

    assert (
        "recovery_canary_not_enabled"
        in recovery_canary._validate_demo_canary_policy(disabled)
    )
    assert (
        "recovery_canary_cli_requires_dry_run"
        in recovery_canary._validate_demo_canary_policy(real_order)
    )


def test_recovery_canary_policy_validation_requires_market_protective_stop():
    policy = recovery_canary._canary_policy_from_risk_config(
        SimpleNamespace(
            recovery_execution_canary=SimpleNamespace(
                enabled=True,
                dry_run=True,
                order_kind="market",
                protective_stop_points=None,
            )
        )
    )

    assert recovery_canary._validate_demo_canary_policy(policy) == [
        "protective_stop_points_required"
    ]


def test_recovery_policy_from_args_caps_total_volume_by_risk_limit():
    args = SimpleNamespace(
        base_volume=0.01,
        multiplier=2.0,
        max_steps=2,
        max_total_volume=None,
        max_next_volume=None,
        step_distance_points=80.0,
        recovery_target_points=10.0,
        min_step_interval_ms=0,
    )
    risk = SimpleNamespace(max_volume_per_symbol=0.03, max_volume_per_order=0.02)
    symbol_info = SimpleNamespace(
        point=0.01, volume_step=0.01, trade_contract_size=100.0
    )

    policy = recovery_canary._recovery_policy_from_args(args, risk, symbol_info)

    assert policy.base_volume == 0.01
    assert policy.max_total_volume == 0.03
    assert policy.max_next_volume == 0.02
    assert policy.point == 0.01


def test_recovery_policy_from_args_rejects_volume_above_risk_limit():
    args = SimpleNamespace(
        base_volume=0.01,
        multiplier=2.0,
        max_steps=2,
        max_total_volume=0.05,
        max_next_volume=0.02,
        step_distance_points=80.0,
        recovery_target_points=10.0,
        min_step_interval_ms=0,
    )
    risk = SimpleNamespace(max_volume_per_symbol=0.03, max_volume_per_order=0.02)
    symbol_info = SimpleNamespace(
        point=0.01, volume_step=0.01, trade_contract_size=100.0
    )

    with pytest.raises(ValueError, match="max_total_volume"):
        recovery_canary._recovery_policy_from_args(args, risk, symbol_info)


def test_initial_policy_for_live_cycle_only_turns_off_dry_run_when_explicit():
    base = recovery_canary._canary_policy_from_risk_config(
        SimpleNamespace(
            recovery_execution_canary=SimpleNamespace(
                enabled=True,
                dry_run=True,
                order_kind="market",
                deviation=12,
                magic=930001,
                comment_prefix="recovery-demo",
                protective_stop_points=120,
            )
        )
    )

    dry_run_initial = recovery_canary._initial_policy_for_live_cycle(
        base,
        submit_initial_order=False,
    )
    submitted_initial = recovery_canary._initial_policy_for_live_cycle(
        base,
        submit_initial_order=True,
    )

    assert dry_run_initial.dry_run is True
    assert submitted_initial.dry_run is False
    assert submitted_initial.enabled is True
    assert submitted_initial.protective_stop_points == 120


def test_recovery_step_policy_for_live_cycle_requires_explicit_recovery_submit():
    base = recovery_canary._canary_policy_from_risk_config(
        SimpleNamespace(
            recovery_execution_canary=SimpleNamespace(
                enabled=True,
                dry_run=True,
                order_kind="market",
                deviation=12,
                magic=930001,
                comment_prefix="recovery-demo",
                protective_stop_points=120,
            )
        )
    )

    dry_run_step = recovery_canary._recovery_step_policy_for_live_cycle(
        base,
        submit_recovery_order=False,
    )
    submitted_step = recovery_canary._recovery_step_policy_for_live_cycle(
        base,
        submit_recovery_order=True,
    )

    assert dry_run_step.dry_run is True
    assert submitted_step.dry_run is False
    assert submitted_step.enabled is True
    assert submitted_step.protective_stop_points == 120


def test_validate_live_cycle_submit_request_blocks_unsafe_recovery_submit():
    args = SimpleNamespace(
        live_cycle=True,
        submit_initial_order=False,
        submit_recovery_order=True,
        keep_initial_open=False,
        max_steps=1,
    )
    policy = SimpleNamespace(
        base_volume=0.01,
        multiplier=2.0,
        max_steps=1,
        max_total_volume=0.03,
        max_next_volume=0.02,
    )

    assert recovery_canary._validate_live_cycle_submit_request(args, policy) == [
        "submit_recovery_order_requires_submit_initial_order"
    ]

    args.submit_initial_order = True
    args.keep_initial_open = True
    assert recovery_canary._validate_live_cycle_submit_request(args, policy) == [
        "submit_recovery_order_requires_auto_cleanup"
    ]

    args.keep_initial_open = False
    args.max_steps = 2
    policy.max_steps = 2
    assert recovery_canary._validate_live_cycle_submit_request(args, policy) == [
        "submit_recovery_order_requires_max_steps_1"
    ]


def test_live_cycle_validation_blocks_submit_when_base_policy_not_dry_run():
    policy = recovery_canary._canary_policy_from_risk_config(
        SimpleNamespace(
            recovery_execution_canary=SimpleNamespace(
                enabled=True,
                dry_run=False,
                order_kind="market",
                protective_stop_points=120,
            )
        )
    )

    assert (
        "recovery_canary_cli_requires_dry_run"
        in recovery_canary._validate_demo_canary_policy(policy)
    )


def test_quote_helpers_use_tradeable_side_for_live_cycle():
    quote = Quote(
        symbol="XAUUSD",
        bid=100.10,
        ask=100.13,
        last=100.12,
        volume=3.0,
        time=datetime.fromtimestamp(1_000, tz=timezone.utc),
    )

    tick = recovery_canary._tick_from_quote(quote)

    assert recovery_canary._entry_price_from_quote(quote, "buy") == 100.13
    assert recovery_canary._entry_price_from_quote(quote, "sell") == 100.10
    assert tick.symbol == "XAUUSD"
    assert tick.bid == 100.10
    assert tick.ask == 100.13
    assert tick.last == 100.12
    assert tick.time_msc == 1_000_000


def test_live_cycle_ok_accepts_hold_or_dry_run_current_step_after_initial_ok():
    assert recovery_canary._live_cycle_ok(
        {
            "status": "submitted",
            "initial": {"status": "submitted"},
            "current_step": {"status": "dry_run"},
        }
    )
    assert recovery_canary._live_cycle_ok(
        {
            "status": "submitted",
            "cleanup_required": True,
            "initial": {"status": "submitted"},
            "current_step": {"status": "submitted"},
            "recovery_cleanup": {"status": "closed"},
        }
    )
    assert recovery_canary._live_cycle_ok(
        {
            "status": "evaluated",
            "initial": {"status": "dry_run"},
            "current_step": {"status": "hold", "reason": "adverse_move_below_trigger"},
            "dry_run_cycle_finalization": {"status": "closed"},
        }
    )
    assert not recovery_canary._live_cycle_ok(
        {
            "status": "evaluated",
            "initial": {"status": "dry_run"},
            "current_step": {"status": "hold", "reason": "adverse_move_below_trigger"},
            "dry_run_cycle_finalization": {"status": "skipped"},
        }
    )
    assert not recovery_canary._live_cycle_ok(
        {
            "status": "evaluated",
            "initial": {"status": "submitted"},
            "current_step": {"status": "block", "reason": "quote_side_missing"},
        }
    )


def test_live_cycle_ok_requires_cleanup_when_submitted_initial_requires_cleanup():
    base = {
        "status": "submitted",
        "cleanup_required": True,
        "initial": {"status": "submitted"},
        "current_step": {"status": "dry_run"},
    }

    assert recovery_canary._live_cycle_ok(
        {
            **base,
            "initial_cleanup": {"status": "closed"},
        }
    )
    assert not recovery_canary._live_cycle_ok(
        {
            **base,
            "initial_cleanup": {"status": "failed"},
        }
    )


def test_live_cycle_ok_rejects_critical_cleanup_alerts():
    assert not recovery_canary._live_cycle_ok(
        {
            "status": "evaluated",
            "cleanup_required": False,
            "initial": {"status": "dry_run"},
            "current_step": {"status": "dry_run"},
            "dry_run_cycle_finalization": {"status": "closed"},
            "critical_alerts": [
                {
                    "severity": "critical",
                    "code": "recovery_initial_cleanup_unresolved",
                }
            ],
        }
    )


def test_public_initial_result_serializes_cycle_without_mutating_payload():
    cycle = SimpleNamespace(
        cycle_id="cycle-1",
        account_key="demo:key",
        symbol="XAUUSD",
        direction="buy",
        status="open",
        base_volume=0.01,
        total_volume=0.01,
        step_count=0,
        average_entry_price=100.0,
        last_entry_price=100.0,
        started_at=datetime.fromtimestamp(1_000, tz=timezone.utc),
        updated_at=datetime.fromtimestamp(1_001, tz=timezone.utc),
        last_step_at=datetime.fromtimestamp(1_000, tz=timezone.utc),
        strategy="tick_recovery_probe",
        timeframe="TICK",
        source_signal_id="sig-1",
        metadata={"canary": True},
    )
    original = {"status": "dry_run", "cycle": cycle}

    public = recovery_canary._public_initial_result(original)

    assert public["cycle"]["cycle_id"] == "cycle-1"
    assert public["cycle"]["metadata"] == {"canary": True}
    assert original["cycle"] is cycle
