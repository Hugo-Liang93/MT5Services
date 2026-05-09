from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.backtesting.tick_replay import RecoveryCanaryGate, RecoveryCanaryGatePolicy
from src.backtesting.tick_replay.recovery import TickRecoveryReplayRunner
from src.clients.mt5_market import Tick
from src.trading.recovery import RecoveryExecutionCanaryPolicy, RecoveryPolicy


def _tick(time_msc: int, bid: float, ask: float) -> Tick:
    return Tick(
        symbol="XAUUSD",
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        volume=1.0,
        time=datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc),
        time_msc=time_msc,
        flags=6,
    )


def test_tick_recovery_replay_opens_bounded_step_and_closes_at_target() -> None:
    runner = TickRecoveryReplayRunner()
    policy = RecoveryPolicy(
        enabled=True,
        base_volume=0.01,
        multiplier=2.0,
        max_steps=2,
        max_total_volume=0.05,
        max_next_volume=0.03,
        step_distance_points=80.0,
        recovery_target_points=10.0,
        point=0.01,
        min_step_interval_ms=0,
    )

    report = runner.run(
        symbol="XAUUSD",
        direction="buy",
        ticks=[
            _tick(1_000, 99.98, 100.00),
            _tick(2_000, 98.98, 99.00),
            _tick(3_000, 99.55, 99.57),
        ],
        policy=policy,
        account_key="demo:broker:1001",
        cycle_id="cycle-1",
    )

    assert report.closed is True
    assert report.close_reason == "net_recovery_target_reached"
    assert report.max_step_count == 1
    assert report.max_total_volume == 0.03
    assert [fill.role for fill in report.fills] == ["initial", "recovery"]
    assert report.fills[0].volume == 0.01
    assert report.fills[1].volume == 0.02
    assert report.estimated_pnl > 0


def test_tick_recovery_replay_blocks_missing_bid_ask_before_recovery() -> None:
    runner = TickRecoveryReplayRunner()
    policy = RecoveryPolicy(
        enabled=True,
        base_volume=0.01,
        multiplier=2.0,
        max_steps=1,
        max_total_volume=0.05,
        max_next_volume=0.03,
        step_distance_points=50.0,
        recovery_target_points=10.0,
        point=0.01,
        min_step_interval_ms=0,
    )

    report = runner.run(
        symbol="XAUUSD",
        direction="buy",
        ticks=[
            _tick(1_000, 99.98, 100.00),
            Tick(
                symbol="XAUUSD",
                bid=None,
                ask=99.00,
                last=99.00,
                volume=1.0,
                time=datetime.fromtimestamp(2, tz=timezone.utc),
                time_msc=2_000,
                flags=6,
            ),
        ],
        policy=policy,
        account_key="demo:broker:1001",
        cycle_id="cycle-1",
    )

    assert report.closed is False
    assert report.blocked_count == 1
    assert report.events[-1].reason == "quote_side_missing"


def test_tick_recovery_replay_reports_bid_ask_coverage() -> None:
    runner = TickRecoveryReplayRunner()
    policy = RecoveryPolicy(
        enabled=True,
        base_volume=0.01,
        multiplier=2.0,
        max_steps=1,
        max_total_volume=0.05,
        max_next_volume=0.03,
        step_distance_points=50.0,
        recovery_target_points=10.0,
        point=0.01,
        min_step_interval_ms=0,
    )

    report = runner.run(
        symbol="XAUUSD",
        direction="buy",
        ticks=[
            _tick(1_000, 99.98, 100.00),
            Tick(
                symbol="XAUUSD",
                bid=None,
                ask=99.00,
                last=99.00,
                volume=1.0,
                time=datetime.fromtimestamp(2, tz=timezone.utc),
                time_msc=2_000,
                flags=6,
            ),
            _tick(3_000, 99.55, 99.57),
        ],
        policy=policy,
        account_key="demo:broker:1001",
        cycle_id="cycle-coverage",
    )

    assert report.tick_count == 3
    assert report.tradable_tick_count == 2
    assert report.tick_coverage == pytest.approx(2 / 3)


def test_recovery_canary_gate_allows_only_clean_dry_run_candidate() -> None:
    runner = TickRecoveryReplayRunner()
    policy = RecoveryPolicy(
        enabled=True,
        base_volume=0.01,
        multiplier=2.0,
        max_steps=2,
        max_total_volume=0.05,
        max_next_volume=0.03,
        step_distance_points=80.0,
        recovery_target_points=10.0,
        point=0.01,
        min_step_interval_ms=0,
    )
    report = runner.run(
        symbol="XAUUSD",
        direction="buy",
        ticks=[
            _tick(1_000, 99.98, 100.00),
            _tick(2_000, 98.98, 99.00),
            _tick(3_000, 99.55, 99.57),
        ],
        policy=policy,
        account_key="demo:broker:1001",
        cycle_id="cycle-clean",
    )

    decision = RecoveryCanaryGate(
        RecoveryCanaryGatePolicy(min_tick_count=3, min_tick_coverage=1.0)
    ).assess(
        report=report,
        recovery_policy=policy,
        canary_policy=RecoveryExecutionCanaryPolicy(enabled=True, dry_run=True),
    )

    assert decision.allowed is True
    assert decision.stage == "dry_run_canary"
    assert decision.reasons == ()


def test_recovery_canary_gate_blocks_low_coverage_and_real_orders() -> None:
    runner = TickRecoveryReplayRunner()
    policy = RecoveryPolicy(
        enabled=True,
        base_volume=0.01,
        multiplier=2.0,
        max_steps=1,
        max_total_volume=0.05,
        max_next_volume=0.03,
        step_distance_points=50.0,
        recovery_target_points=10.0,
        point=0.01,
        min_step_interval_ms=0,
    )
    report = runner.run(
        symbol="XAUUSD",
        direction="buy",
        ticks=[
            _tick(1_000, 99.98, 100.00),
            Tick(
                symbol="XAUUSD",
                bid=None,
                ask=99.00,
                last=99.00,
                volume=1.0,
                time=datetime.fromtimestamp(2, tz=timezone.utc),
                time_msc=2_000,
                flags=6,
            ),
        ],
        policy=policy,
        account_key="demo:broker:1001",
        cycle_id="cycle-blocked",
    )

    decision = RecoveryCanaryGate(
        RecoveryCanaryGatePolicy(min_tick_count=2, min_tick_coverage=0.95)
    ).assess(
        report=report,
        recovery_policy=policy,
        canary_policy=RecoveryExecutionCanaryPolicy(enabled=True, dry_run=False),
    )

    assert decision.allowed is False
    assert "tick_coverage_below_minimum" in decision.reasons
    assert "real_order_requires_reviewed_dry_run" in decision.reasons


def test_recovery_canary_gate_ignores_expected_exposure_cap_blocks_in_ratio() -> None:
    runner = TickRecoveryReplayRunner()
    policy = RecoveryPolicy(
        enabled=True,
        base_volume=0.01,
        multiplier=2.0,
        max_steps=1,
        max_total_volume=0.03,
        max_next_volume=0.02,
        step_distance_points=50.0,
        recovery_target_points=10.0,
        point=0.01,
        min_step_interval_ms=0,
    )
    report = runner.run(
        symbol="XAUUSD",
        direction="buy",
        ticks=[
            _tick(1_000, 99.98, 100.00),
            _tick(2_000, 99.38, 99.40),
            _tick(3_000, 98.98, 99.00),
            _tick(4_000, 98.88, 98.90),
            _tick(5_000, 99.74, 99.76),
        ],
        policy=policy,
        account_key="demo:broker:1001",
        cycle_id="cycle-cap-wait",
    )

    decision = RecoveryCanaryGate(
        RecoveryCanaryGatePolicy(
            min_tick_count=5,
            min_tick_coverage=1.0,
            max_blocked_ratio=0.05,
        )
    ).assess(
        report=report,
        recovery_policy=policy,
        canary_policy=RecoveryExecutionCanaryPolicy(enabled=True, dry_run=True),
    )

    assert report.closed is True
    assert report.blocked_count >= 1
    assert decision.allowed is True
    assert decision.reasons == ()
    assert decision.details["ignored_blocked_count"] == report.blocked_count
    assert decision.details["effective_blocked_count"] == 0
