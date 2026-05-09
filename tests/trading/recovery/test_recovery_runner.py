from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.market.tick_features.models import TickFeatureSnapshot
from src.trading.recovery import RecoveryCycleState
from src.trading.recovery.runner import (
    DemoBoundedRecoveryRunner,
    RecoveryRuntimeRunnerSettings,
)


class DummyStateStore:
    def __init__(self, open_cycle: RecoveryCycleState | None = None):
        self.open_cycle = open_cycle
        self.records = []
        self.load_calls = []
        self.cycle_rows = []

    def load_open_recovery_cycle(self, *, symbol, strategy=None, cycle_id=None):
        self.load_calls.append(
            {
                "symbol": symbol,
                "strategy": strategy,
                "cycle_id": cycle_id,
            }
        )
        return self.open_cycle

    def record_recovery_cycle_state(self, cycle, **kwargs):
        self.records.append((cycle, kwargs))
        self.open_cycle = cycle if cycle.status == "open" else None

    def list_recovery_cycle_states(self, **kwargs):
        return list(self.cycle_rows)


class DummyTradingPort:
    def __init__(self):
        self.dispatch_calls = []
        self.next_ticket = 7001

    def dispatch_operation(self, operation, payload=None):
        self.dispatch_calls.append((operation, dict(payload or {})))
        if operation == "close":
            return {
                "status": "closed",
                "ticket": (payload or {}).get("ticket"),
                "price": 100.10,
            }
        ticket = self.next_ticket
        self.next_ticket += 1
        return {
            "status": "ok",
            "request_id": (payload or {}).get("request_id"),
            "dry_run": bool((payload or {}).get("dry_run")),
            "ticket": ticket,
        }


class DummySubmittedWithoutTicketPort(DummyTradingPort):
    def dispatch_operation(self, operation, payload=None):
        self.dispatch_calls.append((operation, dict(payload or {})))
        return {
            "status": "ok",
            "request_id": (payload or {}).get("request_id"),
            "dry_run": bool((payload or {}).get("dry_run")),
        }


class DummyRiskSkippedPort(DummyTradingPort):
    def dispatch_operation(self, operation, payload=None):
        self.dispatch_calls.append((operation, dict(payload or {})))
        return {
            "status": "ok",
            "dispatch_precheck": {
                "verdict": "block",
                "blocked": True,
                "executable": False,
                "reason": "Hourly trade limit reached",
            },
        }


class DummyRaisingTradePort(DummyTradingPort):
    def dispatch_operation(self, operation, payload=None):
        self.dispatch_calls.append((operation, dict(payload or {})))
        if operation == "trade":
            raise RuntimeError("Hourly trade limit reached")
        return super().dispatch_operation(operation, payload)


class DummyOperatorClosePort(DummyTradingPort):
    def dispatch_operation(self, operation, payload=None):
        if operation == "close":
            self.dispatch_calls.append((operation, dict(payload or {})))
            return {
                "accepted": True,
                "status": "completed",
                "message": "position close completed",
                "effective_state": {
                    "result": {
                        "ticket": (payload or {}).get("ticket"),
                        "success": True,
                    }
                },
            }
        return super().dispatch_operation(operation, payload)


class DummyPositionNotFoundClosePort(DummyTradingPort):
    def dispatch_operation(self, operation, payload=None):
        if operation == "close":
            self.dispatch_calls.append((operation, dict(payload or {})))
            ticket = (payload or {}).get("ticket")
            return {
                "accepted": False,
                "status": "failed",
                "message": f"Position {ticket} not found",
                "error_message": f"Position {ticket} not found",
                "effective_state": {},
                "details": {"operation": "close_position"},
            }
        return super().dispatch_operation(operation, payload)


class DummyPositionSnapshotProvider:
    def __init__(self, *, positions=None, last_reconcile_at=None):
        self.positions = list(positions or [])
        self.last_reconcile_at = last_reconcile_at

    def active_positions(self):
        return list(self.positions)

    def status(self):
        return {"last_reconcile_at": self.last_reconcile_at}


def _now() -> datetime:
    return datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> RecoveryRuntimeRunnerSettings:
    values = {
        "enabled": True,
        "dry_run": True,
        "demo_only": True,
        "symbol": "XAUUSD",
        "direction": "buy",
        "strategy": "tick_martingale_probe",
        "timeframe": "TICK",
        "base_volume": 0.01,
        "multiplier": 2.0,
        "max_steps": 1,
        "max_total_volume": 0.03,
        "max_next_volume": 0.02,
        "step_distance_points": 80.0,
        "recovery_target_points": 5.0,
        "point": 0.01,
        "min_step_interval_ms": 0,
        "protective_stop_points": 80.0,
        "deviation": 12,
        "magic": 930001,
        "max_cycles_per_session": 1,
        "max_cycles_per_day": 0,
        "snapshot_stale_seconds": 5.0,
        "blocked_dispatch_retry_seconds": 30.0,
        "risk_profile": "recovery_budgeted",
        "max_daily_recovery_loss_amount": 0.0,
        "max_rolling_recovery_loss_amount": 0.0,
        "rolling_loss_window_minutes": 60,
        "max_consecutive_loss_cycles": 0,
        "loss_lockout_minutes": 0,
    }
    values.update(overrides)
    return RecoveryRuntimeRunnerSettings(**values)


def _snapshot(
    *,
    bid: float | None = 100.0,
    ask: float | None = 100.02,
    spread_points: float | None = 2.0,
    price_change_points: float | None = 1.0,
    buy_pressure: float | None = 0.55,
    sell_pressure: float | None = 0.45,
    status: str = "healthy",
    reasons: tuple[str, ...] = (),
    at: datetime | None = None,
) -> TickFeatureSnapshot:
    generated_at = at or _now()
    return TickFeatureSnapshot(
        symbol="XAUUSD",
        window_start_msc=1_000,
        window_end_msc=2_000,
        generated_at=generated_at,
        tick_count=10,
        bid=bid,
        ask=ask,
        last=None if bid is None or ask is None else (bid + ask) / 2,
        mid=None if bid is None or ask is None else (bid + ask) / 2,
        spread_points=spread_points,
        quote_age_ms=100,
        realized_range_points=8.0,
        price_change_points=price_change_points,
        buy_pressure=buy_pressure,
        sell_pressure=sell_pressure,
        status=status,
        reasons=reasons,
    )


def _cycle_state(**overrides) -> RecoveryCycleState:
    values = {
        "cycle_id": "cycle-open",
        "account_key": "demo:broker:1001",
        "symbol": "XAUUSD",
        "direction": "buy",
        "status": "open",
        "base_volume": 0.01,
        "total_volume": 0.01,
        "step_count": 0,
        "average_entry_price": 100.0,
        "last_entry_price": 100.0,
        "started_at": _now(),
        "updated_at": _now(),
        "last_step_at": _now(),
        "strategy": "tick_martingale_probe",
        "timeframe": "TICK",
        "source_signal_id": "sig-open",
    }
    values.update(overrides)
    return RecoveryCycleState(**values)


def _runner(
    *,
    settings: RecoveryRuntimeRunnerSettings | None = None,
    store: DummyStateStore | None = None,
    trading: DummyTradingPort | None = None,
    position_snapshot_provider=None,
) -> tuple[DemoBoundedRecoveryRunner, DummyStateStore, DummyTradingPort]:
    state_store = store or DummyStateStore()
    trading_port = trading or DummyTradingPort()
    runner = DemoBoundedRecoveryRunner(
        settings=settings or _settings(),
        account_alias="demo-main",
        account_key="demo:broker:1001",
        state_store=state_store,
        trading_port=trading_port,
        position_snapshot_provider=position_snapshot_provider,
        clock=_now,
        cycle_id_factory=lambda now: "cycle-runner",
        source_signal_id_factory=lambda cycle_id: f"{cycle_id}-signal",
    )
    return runner, state_store, trading_port


def test_runner_opens_initial_dry_run_cycle_from_healthy_tick_feature():
    runner, store, trading = _runner()

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "open_initial"
    assert result["status"] == "dry_run"
    assert len(store.records) == 1
    cycle, kwargs = store.records[-1]
    assert cycle.cycle_id == "cycle-runner"
    assert cycle.source_signal_id == "cycle-runner-signal"
    assert cycle.total_volume == 0.01
    assert cycle.step_count == 0
    assert kwargs["status_reason"] == "resident_recovery_initial_dry_run"

    operation, payload = trading.dispatch_calls[0]
    assert operation == "trade"
    assert payload["request_id"] == "recovery:cycle-runner:initial"
    assert payload["trace_id"] == "cycle-runner-signal"
    assert payload["dry_run"] is True
    assert payload["metadata"]["execution_scope"] == "recovery_initial"
    assert payload["metadata"]["risk_profile"] == "recovery_budgeted"

    status = runner.status()
    assert status["running"] is True
    assert status["risk_profile"] == "recovery_budgeted"
    assert status["risk_budget"]["profile"] == "recovery_budgeted"
    assert status["active_cycle_id"] == "cycle-runner"
    assert status["processed_snapshots"] == 1
    assert status["decision_counts"]["open_initial"] == 1
    assert status["stalled"] is False


def test_runner_auto_direction_opens_sell_initial_when_tick_features_are_bearish():
    runner, store, trading = _runner(
        settings=_settings(
            direction_mode="auto",
            min_directional_move_points=5.0,
            min_pressure_delta=0.2,
            max_entry_spread_points=20.0,
            recovery_target_points=30.0,
            slippage_budget_points=2.0,
            min_net_profit_points=5.0,
        )
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(
        _snapshot(
            bid=100.0,
            ask=100.08,
            price_change_points=-8.0,
            buy_pressure=0.2,
            sell_pressure=0.8,
            spread_points=8.0,
        )
    )

    assert result["action"] == "open_initial"
    cycle, _ = store.records[-1]
    assert cycle.direction == "sell"
    operation, payload = trading.dispatch_calls[0]
    assert operation == "trade"
    assert payload["side"] == "sell"
    assert payload["price"] == 100.0
    assert payload["metadata"]["direction_policy"]["reason"] == "auto_direction_sell"
    assert payload["metadata"]["cost_gate"]["reason"] == "entry_cost_ok"


def test_runner_waits_for_entry_signal_confirmation_before_initial_dispatch():
    runner, store, trading = _runner(
        settings=_settings(
            direction_mode="auto",
            min_directional_move_points=5.0,
            min_pressure_delta=0.2,
            entry_confirmation_snapshots=2,
            entry_confirmation_max_gap_seconds=3.0,
            real_trade_calibration_guard_enabled=False,
        )
    )

    runner.start()
    first = runner.on_tick_feature_snapshot(
        _snapshot(
            price_change_points=10.0,
            buy_pressure=0.7,
            sell_pressure=0.3,
            at=_now(),
        )
    )
    second = runner.on_tick_feature_snapshot(
        _snapshot(
            price_change_points=12.0,
            buy_pressure=0.75,
            sell_pressure=0.25,
            at=_now() + timedelta(seconds=1),
        )
    )

    assert first["action"] == "hold"
    assert first["reason"] == "entry_confirmation_pending"
    assert first["execution"]["entry_confirmation"]["metadata"][
        "confirmation_count"
    ] == 1
    assert second["action"] == "open_initial"
    assert second["reason"] == "initial_dispatched"
    assert len(trading.dispatch_calls) == 1
    assert store.open_cycle is not None


def test_runner_resets_entry_signal_confirmation_when_gap_expires():
    runner, store, trading = _runner(
        settings=_settings(
            direction_mode="auto",
            min_directional_move_points=5.0,
            min_pressure_delta=0.2,
            entry_confirmation_snapshots=2,
            entry_confirmation_max_gap_seconds=1.0,
            real_trade_calibration_guard_enabled=False,
        )
    )

    runner.start()
    first = runner.on_tick_feature_snapshot(
        _snapshot(
            price_change_points=10.0,
            buy_pressure=0.7,
            sell_pressure=0.3,
            at=_now(),
        )
    )
    second = runner.on_tick_feature_snapshot(
        _snapshot(
            price_change_points=12.0,
            buy_pressure=0.75,
            sell_pressure=0.25,
            at=_now() + timedelta(seconds=2),
        )
    )

    assert first["reason"] == "entry_confirmation_pending"
    assert second["reason"] == "entry_confirmation_pending"
    assert second["execution"]["entry_confirmation"]["metadata"][
        "confirmation_count"
    ] == 1
    assert trading.dispatch_calls == []
    assert store.open_cycle is None


def test_runner_auto_direction_holds_when_tick_features_are_not_actionable():
    runner, store, trading = _runner(
        settings=_settings(
            direction_mode="auto",
            min_directional_move_points=5.0,
            min_pressure_delta=0.2,
        )
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(
        _snapshot(price_change_points=1.0, buy_pressure=0.7, sell_pressure=0.3)
    )

    assert result["action"] == "hold"
    assert result["reason"] == "direction_signal_too_weak"
    assert store.records == []
    assert trading.dispatch_calls == []
    status = runner.status()
    assert status["decision_counts"]["hold"] == 1
    analytics = status["decision_analytics"]
    assert analytics["reason_counts"]["direction_signal_too_weak"] == 1
    assert (
        analytics["direction_policy_reason_counts"]["direction_signal_too_weak"]
        == 1
    )


def test_runner_cost_gate_holds_initial_when_spread_is_too_wide():
    runner, store, trading = _runner(
        settings=_settings(
            direction_mode="auto",
            min_directional_move_points=5.0,
            min_pressure_delta=0.2,
            max_entry_spread_points=5.0,
        )
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(
        _snapshot(
            bid=100.0,
            ask=100.08,
            price_change_points=8.0,
            buy_pressure=0.7,
            sell_pressure=0.3,
            spread_points=8.0,
        )
    )

    assert result["action"] == "hold"
    assert result["reason"] == "spread_too_wide"
    assert store.records == []
    assert trading.dispatch_calls == []
    status = runner.status()
    assert status["decision_counts"]["hold"] == 1
    analytics = status["decision_analytics"]
    assert analytics["cost_gate_reason_counts"]["spread_too_wide"] == 1
    assert analytics["cost_gate_allowed_counts"]["blocked"] == 1


def test_runner_opens_initial_submitted_cycle_on_demo_account():
    runner, store, trading = _runner(
        settings=_settings(
            dry_run=False,
            real_trade_calibration_guard_enabled=False,
        )
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "open_initial"
    assert result["status"] == "submitted"
    cycle, kwargs = store.records[-1]
    assert kwargs["status_reason"] == "resident_recovery_initial_submitted"
    assert cycle.metadata["submitted_tickets"] == [
        {"scope": "initial", "step_index": 0, "ticket": 7001}
    ]
    operation, payload = trading.dispatch_calls[0]
    assert operation == "trade"
    assert payload["dry_run"] is False
    assert payload["metadata"]["execution_scope"] == "recovery_initial"
    assert payload["metadata"]["risk_profile"] == "recovery_budgeted"
    assert runner.status()["active_cycle_id"] == "cycle-runner"
    assert runner.status()["last_execution_status"] == "submitted"
    assert runner.status()["active_submitted_tickets"] == [7001]


def test_runner_blocks_real_initial_until_calibration_samples_are_ready():
    runner, store, trading = _runner(
        settings=_settings(
            dry_run=False,
            recovery_target_points=30.0,
            min_net_profit_points=5.0,
            real_trade_calibration_guard_enabled=True,
            real_trade_calibration_min_samples=1,
            real_trade_calibration_max_target_shortfall_p90_points=0.0,
            real_trade_calibration_min_net_margin_p50_points=0.0,
        )
    )

    runner.start()
    first = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))
    assert store.records == []
    assert trading.dispatch_calls == []

    second = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert first["action"] == "hold"
    assert first["reason"] == "calibration_guard_samples_insufficient"
    assert second["action"] == "open_initial"
    assert second["status"] == "submitted"
    assert len(trading.dispatch_calls) == 1
    assert store.records[-1][1]["status_reason"] == "resident_recovery_initial_submitted"
    status = runner.status()
    assert status["calibration_guard"]["reason"] == "calibration_guard_passed"
    assert (
        status["decision_analytics"]["entry_calibration"]["net_margin_points"][
            "sample_count"
        ]
        >= 1
    )


def test_runner_holds_initial_when_daily_cycle_cap_is_reached():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "cycle-earlier",
            "account_key": "demo:broker:1001",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_submitted",
            "metadata": {"initial_result": {"dry_run": False}},
            "started_at": _now().isoformat(),
        }
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(max_cycles_per_day=1),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "hold"
    assert result["reason"] == "max_cycles_per_day_reached"
    assert store.records == []
    assert trading.dispatch_calls == []
    assert runner.status()["decision_counts"]["open_initial"] == 0
    assert runner.status()["decision_counts"]["hold"] == 1


def test_runner_allows_initial_when_session_cycle_cap_is_disabled():
    store = DummyStateStore()
    runner, store, trading = _runner(
        store=store,
        settings=_settings(
            max_cycles_per_session=0,
            real_trade_calibration_guard_enabled=False,
        ),
    )
    runner._session_cycle_count = 5

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "open_initial"
    assert result["status"] == "dry_run"
    assert len(store.records) == 1
    assert len(trading.dispatch_calls) == 1


def test_runner_holds_initial_when_daily_recovery_loss_budget_is_reached():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "loss-cycle",
            "account_key": "demo:broker:1001",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status": "closed",
            "status_reason": "resident_recovery_cycle_loss_limit_submitted",
            "metadata": {"initial_result": {"dry_run": False}},
            "started_at": (_now() - timedelta(minutes=30)).isoformat(),
            "closed_at": (_now() - timedelta(minutes=20)).isoformat(),
            "realized_pnl": -6.25,
        }
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(
            dry_run=False,
            real_trade_calibration_guard_enabled=False,
            max_daily_recovery_loss_amount=5.0,
        ),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "hold"
    assert result["reason"] == "recovery_daily_loss_budget_reached"
    assert store.records == []
    assert trading.dispatch_calls == []
    budget = runner.status()["risk_budget"]
    assert budget["active_reason"] == "recovery_daily_loss_budget_reached"
    assert budget["daily_realized_loss_amount"] == 6.25
    assert budget["max_daily_recovery_loss_amount"] == 5.0
    assert budget["remaining_daily_loss_amount"] == 0.0


def test_runner_risk_budget_ignores_other_account_losses():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "other-account-loss-cycle",
            "account_key": "demo:broker:other",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_cycle_loss_limit_submitted",
            "realized_pnl": -8.0,
            "closed_at": _now().isoformat(),
            "started_at": (_now() - timedelta(minutes=10)).isoformat(),
            "metadata": {"initial_result": {"dry_run": False}},
        }
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(max_daily_recovery_loss_amount=5.0),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "open_initial"
    assert trading.dispatch_calls
    budget = runner.status()["risk_budget"]
    assert budget["account_key"] == "demo:broker:1001"
    assert budget["daily_realized_loss_amount"] == 0.0
    assert budget["remaining_daily_loss_amount"] == 5.0


def test_runner_holds_initial_when_rolling_recovery_loss_budget_is_reached():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "recent-loss-cycle",
            "account_key": "demo:broker:1001",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status": "closed",
            "status_reason": "resident_recovery_cycle_loss_limit_dry_run",
            "metadata": {"initial_result": {"dry_run": True}},
            "started_at": (_now() - timedelta(minutes=20)).isoformat(),
            "closed_at": (_now() - timedelta(minutes=10)).isoformat(),
            "realized_pnl": -4.1,
        }
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(
            max_rolling_recovery_loss_amount=4.0,
            rolling_loss_window_minutes=60,
        ),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "hold"
    assert result["reason"] == "recovery_rolling_loss_budget_reached"
    assert store.records == []
    assert trading.dispatch_calls == []
    budget = runner.status()["risk_budget"]
    assert budget["rolling_realized_loss_amount"] == 4.1
    assert budget["max_rolling_recovery_loss_amount"] == 4.0


def test_runner_daily_cycle_cap_counts_unique_cycle_ids():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "cycle-earlier",
            "account_key": "demo:broker:1001",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_step_submitted",
            "metadata": {"initial_result": {"dry_run": False}},
            "started_at": _now().isoformat(),
        },
        {
            "cycle_id": "cycle-earlier",
            "account_key": "demo:broker:1001",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_submitted",
            "metadata": {"initial_result": {"dry_run": False}},
            "started_at": _now().isoformat(),
        },
        {
            "cycle_id": "cycle-dry-run-earlier",
            "account_key": "demo:broker:1001",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_dry_run",
            "metadata": {"initial_result": {"dry_run": True}},
            "started_at": _now().isoformat(),
        },
        {
            "cycle_id": "demo-recovery-canary-earlier",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "canary_recovery_cycle_cleanup_closed",
            "started_at": _now().isoformat(),
        },
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(
            dry_run=False,
            max_cycles_per_day=2,
            real_trade_calibration_guard_enabled=False,
        ),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "open_initial"
    assert len(trading.dispatch_calls) == 1
    assert runner.status()["decision_counts"]["open_initial"] == 1


def test_runner_daily_cycle_cap_ignores_other_account_cycles():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "other-cycle-a",
            "account_key": "demo:broker:other",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_submitted",
            "metadata": {"initial_result": {"dry_run": False}},
            "started_at": _now().isoformat(),
        },
        {
            "cycle_id": "other-cycle-b",
            "account_key": "demo:broker:other",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_submitted",
            "metadata": {"initial_result": {"dry_run": False}},
            "started_at": _now().isoformat(),
        },
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(
            dry_run=False,
            max_cycles_per_day=2,
            real_trade_calibration_guard_enabled=False,
        ),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "open_initial"
    assert len(trading.dispatch_calls) == 1
    assert runner.status()["pacing"]["account_key"] == "demo:broker:1001"


def test_runner_holds_initial_during_min_cycle_interval():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "recent-cycle",
            "account_key": "demo:broker:1001",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_dry_run",
            "metadata": {"initial_result": {"dry_run": True}},
            "started_at": (_now() - timedelta(seconds=60)).isoformat(),
            "closed_at": (_now() - timedelta(seconds=30)).isoformat(),
        }
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(min_cycle_interval_seconds=120.0),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "hold"
    assert result["reason"] == "cycle_min_interval_active"
    assert store.records == []
    assert trading.dispatch_calls == []
    status = runner.status()
    assert status["decision_counts"]["hold"] == 1
    assert status["pacing"]["remaining_seconds"] == 60.0
    assert status["pacing"]["next_cycle_not_before"] == (
        _now() + timedelta(seconds=60)
    ).isoformat()


def test_runner_holds_initial_during_close_cooldown():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "closed-cycle",
            "account_key": "demo:broker:1001",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_dry_run",
            "metadata": {"initial_result": {"dry_run": True}},
            "started_at": (_now() - timedelta(minutes=5)).isoformat(),
            "closed_at": (_now() - timedelta(seconds=30)).isoformat(),
        }
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(cooldown_after_cycle_close_seconds=90.0),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "hold"
    assert result["reason"] == "cycle_close_cooldown_active"
    assert store.records == []
    assert trading.dispatch_calls == []
    status = runner.status()
    assert status["decision_counts"]["hold"] == 1
    assert status["pacing"]["remaining_seconds"] == 60.0
    assert status["pacing"]["latest_closed_at"] == (
        _now() - timedelta(seconds=30)
    ).isoformat()


def test_runner_close_cooldown_ignores_other_account_cycle():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "other-closed-cycle",
            "account_key": "demo:broker:other",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_dry_run",
            "metadata": {"initial_result": {"dry_run": True}},
            "started_at": (_now() - timedelta(minutes=5)).isoformat(),
            "closed_at": (_now() - timedelta(seconds=30)).isoformat(),
        }
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(cooldown_after_cycle_close_seconds=90.0),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "open_initial"
    assert len(trading.dispatch_calls) == 1
    status = runner.status()
    assert status["pacing"]["latest_closed_at"] is None
    assert status["pacing"]["account_key"] == "demo:broker:1001"


def test_runner_holds_initial_when_hourly_cycle_cap_is_reached():
    store = DummyStateStore()
    store.cycle_rows = [
        {
            "cycle_id": "cycle-a",
            "account_key": "demo:broker:1001",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_dry_run",
            "metadata": {"initial_result": {"dry_run": True}},
            "started_at": (_now() - timedelta(minutes=45)).isoformat(),
        },
        {
            "cycle_id": "cycle-b",
            "account_key": "demo:broker:1001",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_dry_run",
            "metadata": {"initial_result": {"dry_run": True}},
            "started_at": (_now() - timedelta(minutes=15)).isoformat(),
        },
        {
            "cycle_id": "cycle-old",
            "account_key": "demo:broker:1001",
            "symbol": "XAUUSD",
            "strategy": "tick_martingale_probe",
            "status_reason": "resident_recovery_target_reached_dry_run",
            "metadata": {"initial_result": {"dry_run": True}},
            "started_at": (_now() - timedelta(hours=2)).isoformat(),
        },
    ]
    runner, store, trading = _runner(
        store=store,
        settings=_settings(max_cycles_per_hour=2),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "hold"
    assert result["reason"] == "max_cycles_per_hour_reached"
    assert store.records == []
    assert trading.dispatch_calls == []
    status = runner.status()
    assert status["decision_counts"]["hold"] == 1
    assert status["pacing"]["hourly_cycle_count"] == 2
    assert status["pacing"]["next_cycle_not_before"] == (
        _now() + timedelta(minutes=15)
    ).isoformat()


def test_runner_rejects_real_orders_outside_demo_account():
    runner = DemoBoundedRecoveryRunner(
        settings=_settings(dry_run=False),
        account_alias="live-main",
        account_key="live:broker:1001",
        state_store=DummyStateStore(),
        trading_port=DummyTradingPort(),
        clock=_now,
    )

    try:
        runner.start()
    except RuntimeError as exc:
        assert "demo account" in str(exc)
    else:
        raise AssertionError("real recovery runner must reject non-demo accounts")


def test_runner_blocks_submitted_initial_when_ticket_is_missing():
    runner, store, _ = _runner(
        settings=_settings(
            dry_run=False,
            real_trade_calibration_guard_enabled=False,
        ),
        trading=DummySubmittedWithoutTicketPort(),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))

    assert result["action"] == "block"
    assert result["reason"] == "submitted_ticket_missing"
    assert runner.status()["active_cycle_id"] is None
    assert store.open_cycle is None


def test_runner_ignores_blocked_tick_feature_without_state_or_trade():
    runner, store, trading = _runner()

    runner.start()
    result = runner.on_tick_feature_snapshot(
        _snapshot(
            bid=None,
            ask=None,
            status="blocked",
            reasons=("quote_side_missing",),
        )
    )

    assert result == {
        "status": "ignored",
        "reason": "tick_feature_blocked",
        "reasons": ["quote_side_missing"],
    }
    assert store.records == []
    assert trading.dispatch_calls == []
    status = runner.status()
    assert status["ignored_snapshots"] == 1
    assert status["last_reason"] == "tick_feature_blocked"


def test_runner_applies_dry_run_recovery_step_to_loaded_cycle():
    store = DummyStateStore(open_cycle=_cycle_state())
    runner, store, trading = _runner(store=store)

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=99.0, ask=99.02))

    assert result["action"] == "open_step"
    assert result["status"] == "dry_run"
    operation, payload = trading.dispatch_calls[0]
    assert operation == "trade"
    assert payload["request_id"] == "recovery:cycle-open:step:1"
    assert payload["dry_run"] is True
    assert payload["volume"] == 0.02
    assert payload["metadata"]["execution_scope"] == "recovery_scaling"
    assert payload["metadata"]["risk_profile"] == "recovery_budgeted"

    updated_cycle, kwargs = store.records[-1]
    assert updated_cycle.step_count == 1
    assert updated_cycle.total_volume == 0.03
    assert kwargs["status_reason"] == "resident_recovery_step_dry_run"
    assert runner.status()["decision_counts"]["open_step"] == 1


def test_runner_holds_recovery_step_when_spread_is_too_wide():
    store = DummyStateStore(open_cycle=_cycle_state())
    runner, store, trading = _runner(
        store=store,
        settings=_settings(max_entry_spread_points=5.0),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(
        _snapshot(bid=99.0, ask=99.08, spread_points=8.0)
    )

    assert result["action"] == "hold"
    assert result["reason"] == "step_spread_too_wide"
    assert result["execution"]["cost_gate"]["reason"] == "step_spread_too_wide"
    assert store.records == []
    assert trading.dispatch_calls == []
    status = runner.status()
    assert status["decision_counts"]["hold"] == 1
    assert status["decision_counts"]["open_step"] == 0
    assert (
        status["decision_analytics"]["cost_gate_reason_counts"][
            "step_spread_too_wide"
        ]
        == 1
    )
    assert status["decision_analytics"]["cost_gate_allowed_counts"]["blocked"] == 1


def test_runner_applies_submitted_recovery_step_and_records_ticket():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001}
                ]
            }
        )
    )
    trading = DummyTradingPort()
    trading.next_ticket = 7002
    runner, store, trading = _runner(
        store=store,
        trading=trading,
        settings=_settings(dry_run=False),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=99.0, ask=99.02))

    assert result["action"] == "open_step"
    assert result["status"] == "submitted"
    operation, payload = trading.dispatch_calls[0]
    assert operation == "trade"
    assert payload["dry_run"] is False
    assert payload["request_id"] == "recovery:cycle-open:step:1"
    updated_cycle, kwargs = store.records[-1]
    assert kwargs["status_reason"] == "resident_recovery_step_submitted"
    assert updated_cycle.step_count == 1
    assert updated_cycle.metadata["submitted_tickets"] == [
        {"scope": "initial", "step_index": 0, "ticket": 7001},
        {"scope": "step_1", "step_index": 1, "ticket": 7002},
    ]
    assert runner.status()["active_submitted_tickets"] == [7001, 7002]


def test_runner_blocks_submitted_recovery_step_when_ticket_is_missing():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001}
                ]
            }
        )
    )
    runner, store, _ = _runner(
        store=store,
        trading=DummySubmittedWithoutTicketPort(),
        settings=_settings(dry_run=False),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=99.0, ask=99.02))

    assert result["action"] == "block"
    assert result["reason"] == "submitted_ticket_missing"
    assert store.records == []
    assert runner.status()["active_submitted_tickets"] == [7001]
    assert runner.status()["decision_counts"]["open_step"] == 0
    assert runner.status()["decision_counts"]["block"] == 1


def test_runner_blocks_and_throttles_risk_skipped_recovery_step():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001}
                ]
            }
        )
    )
    trading = DummyRiskSkippedPort()
    runner, store, trading = _runner(
        store=store,
        trading=trading,
        settings=_settings(dry_run=False, blocked_dispatch_retry_seconds=30.0),
    )

    runner.start()
    first = runner.on_tick_feature_snapshot(_snapshot(bid=99.0, ask=99.02))
    second = runner.on_tick_feature_snapshot(_snapshot(bid=98.9, ask=98.92))

    assert first["action"] == "block"
    assert first["status"] == "skipped"
    assert first["reason"] == "Hourly trade limit reached"
    assert second["action"] == "hold"
    assert second["reason"] == "blocked_step_retry_wait"
    assert len(trading.dispatch_calls) == 1
    assert store.records == []
    status = runner.status()
    assert status["decision_counts"]["open_step"] == 0
    assert status["decision_counts"]["block"] == 1
    assert status["decision_counts"]["hold"] == 1


def test_runner_blocks_and_throttles_raising_recovery_step_dispatch():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001}
                ]
            }
        )
    )
    trading = DummyRaisingTradePort()
    runner, store, trading = _runner(
        store=store,
        trading=trading,
        settings=_settings(dry_run=False, blocked_dispatch_retry_seconds=30.0),
    )

    runner.start()
    first = runner.on_tick_feature_snapshot(_snapshot(bid=99.0, ask=99.02))
    second = runner.on_tick_feature_snapshot(_snapshot(bid=98.9, ask=98.92))

    assert first["action"] == "block"
    assert first["status"] == "skipped"
    assert first["reason"] == "Hourly trade limit reached"
    assert first["execution"]["category"] == "trading_port_failure"
    assert second["action"] == "hold"
    assert second["reason"] == "blocked_step_retry_wait"
    assert len(trading.dispatch_calls) == 1
    assert store.records == []
    status = runner.status()
    assert status["last_error"] is None
    assert status["consecutive_errors"] == 0
    assert status["decision_counts"]["block"] == 1
    assert status["decision_counts"]["hold"] == 1


def test_runner_blocks_and_throttles_risk_blocked_initial_dispatch():
    trading = DummyRiskSkippedPort()
    runner, store, trading = _runner(
        trading=trading,
        settings=_settings(
            dry_run=False,
            blocked_dispatch_retry_seconds=30.0,
            real_trade_calibration_guard_enabled=False,
        ),
    )

    runner.start()
    first = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))
    second = runner.on_tick_feature_snapshot(_snapshot(bid=100.1, ask=100.12))

    assert first["action"] == "block"
    assert first["status"] == "blocked"
    assert first["reason"] == "Hourly trade limit reached"
    assert second["action"] == "hold"
    assert second["reason"] == "blocked_initial_retry_wait"
    assert len(trading.dispatch_calls) == 1
    assert store.records == []
    status = runner.status()
    assert status["active_cycle_id"] is None
    assert status["decision_counts"]["open_initial"] == 0
    assert status["decision_counts"]["block"] == 1
    assert status["decision_counts"]["hold"] == 1


def test_runner_blocks_and_throttles_raising_initial_dispatch():
    trading = DummyRaisingTradePort()
    runner, store, trading = _runner(
        trading=trading,
        settings=_settings(
            dry_run=False,
            blocked_dispatch_retry_seconds=30.0,
            real_trade_calibration_guard_enabled=False,
        ),
    )

    runner.start()
    first = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.02))
    second = runner.on_tick_feature_snapshot(_snapshot(bid=100.1, ask=100.12))

    assert first["action"] == "block"
    assert first["status"] == "blocked"
    assert first["reason"] == "Hourly trade limit reached"
    assert first["execution"]["category"] == "trading_port_failure"
    assert second["action"] == "hold"
    assert second["reason"] == "blocked_initial_retry_wait"
    assert len(trading.dispatch_calls) == 1
    assert store.records == []
    status = runner.status()
    assert status["last_error"] is None
    assert status["consecutive_errors"] == 0
    assert status["decision_counts"]["block"] == 1
    assert status["decision_counts"]["hold"] == 1


def test_runner_closes_dry_run_cycle_when_target_is_reached():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            total_volume=0.03,
            step_count=1,
            average_entry_price=100.0,
            last_entry_price=99.02,
        )
    )
    runner, store, trading = _runner(store=store)

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.06, ask=100.08))

    assert result["action"] == "close_cycle"
    assert result["status"] == "closed"
    assert trading.dispatch_calls == []
    closed_cycle, kwargs = store.records[-1]
    assert closed_cycle.status == "closed"
    assert kwargs["status_reason"] == "resident_recovery_target_reached_dry_run"
    assert kwargs["close_price"] == 100.06
    assert closed_cycle.metadata["close_decision"]["metadata"]["net_profit_points"] == 6.0
    assert closed_cycle.metadata["close_decision"]["metadata"]["target_net_points"] == 5.0
    assert runner.status()["active_cycle_id"] is None
    status = runner.status()
    assert status["decision_counts"]["close_cycle"] == 1
    assert status["decision_analytics"]["net_exit"]["sample_count"] == 1


def test_runner_closes_dry_run_cycle_when_loss_limit_is_reached():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            total_volume=0.03,
            step_count=1,
            average_entry_price=100.0,
            last_entry_price=99.02,
        )
    )
    runner, store, trading = _runner(
        store=store,
        settings=_settings(
            recovery_target_points=100.0,
            max_cycle_loss_points=20.0,
            slippage_budget_points=0.0,
            commission_points=0.0,
        ),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=99.80, ask=99.88))

    assert result["action"] == "close_cycle"
    assert result["reason"] == "cycle_loss_limit_reached"
    assert trading.dispatch_calls == []
    closed_cycle, kwargs = store.records[-1]
    assert closed_cycle.status == "closed"
    assert kwargs["status_reason"] == "resident_recovery_cycle_loss_limit_dry_run"


def test_runner_closes_dry_run_cycle_when_duration_limit_is_reached():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            started_at=_now() - timedelta(seconds=301),
            updated_at=_now() - timedelta(seconds=301),
        )
    )
    runner, store, trading = _runner(
        store=store,
        settings=_settings(
            recovery_target_points=100.0,
            max_cycle_duration_seconds=300.0,
        ),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.0, ask=100.08))

    assert result["action"] == "close_cycle"
    assert result["reason"] == "max_cycle_duration_reached"
    assert trading.dispatch_calls == []
    closed_cycle, kwargs = store.records[-1]
    assert closed_cycle.status == "closed"
    assert kwargs["status_reason"] == "resident_recovery_max_cycle_duration_dry_run"


def test_runner_can_close_dry_run_cycle_after_max_steps_reached():
    store = DummyStateStore(open_cycle=_cycle_state(step_count=1, total_volume=0.03))
    runner, store, trading = _runner(
        store=store,
        settings=_settings(
            recovery_target_points=100.0,
            max_steps=1,
            max_steps_exit_mode="close_cycle",
        ),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=99.95, ask=100.03))

    assert result["action"] == "close_cycle"
    assert result["reason"] == "max_steps_exit_reached"
    assert trading.dispatch_calls == []
    closed_cycle, kwargs = store.records[-1]
    assert closed_cycle.status == "closed"
    assert kwargs["status_reason"] == "resident_recovery_max_steps_exit_dry_run"


def test_runner_records_status_reason_for_max_steps_hold_time_exit():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            step_count=1,
            total_volume=0.03,
            last_step_at=_now() - timedelta(seconds=21),
        )
    )
    runner, store, trading = _runner(
        store=store,
        settings=_settings(
            recovery_target_points=100.0,
            max_steps=1,
            max_steps_exit_mode="hold",
            max_steps_hold_seconds=20.0,
        ),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=99.95, ask=100.03))

    assert result["action"] == "close_cycle"
    assert result["reason"] == "max_steps_hold_time_reached"
    assert trading.dispatch_calls == []
    closed_cycle, kwargs = store.records[-1]
    assert closed_cycle.status == "closed"
    assert kwargs["status_reason"] == "resident_recovery_max_steps_hold_time_dry_run"


def test_runner_status_exposes_exit_policy_contract():
    runner, _, _ = _runner(
        settings=_settings(
            step_distance_points=70.0,
            max_step_adverse_move_points=130.0,
            max_cycle_loss_points=120.0,
            max_cycle_duration_seconds=900.0,
            max_steps_exit_mode="close_cycle",
            max_steps_hold_seconds=20.0,
        )
    )

    status = runner.status()

    assert status["exit_policy"] == {
        "recovery_target_points": 5.0,
        "max_cycle_loss_points": 120.0,
        "max_cycle_duration_seconds": 900.0,
        "max_steps": 1,
        "max_steps_exit_mode": "close_cycle",
        "max_steps_hold_seconds": 20.0,
    }
    assert status["step_policy"] == {
        "step_distance_points": 70.0,
        "max_step_adverse_move_points": 130.0,
        "min_step_interval_ms": 0,
        "max_steps": 1,
        "max_next_volume": 0.02,
        "max_total_volume": 0.03,
    }


def test_runner_holds_recovery_step_when_adverse_move_is_overextended():
    store = DummyStateStore(open_cycle=_cycle_state())
    runner, store, trading = _runner(
        store=store,
        settings=_settings(
            step_distance_points=80.0,
            max_step_adverse_move_points=120.0,
        ),
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=98.5, ask=98.52))

    assert result["action"] == "hold"
    assert result["reason"] == "step_adverse_move_overextended"
    assert store.records == []
    assert trading.dispatch_calls == []
    assert result["decision"]["metadata"]["adverse_move_points"] == 150.0
    assert (
        result["decision"]["metadata"]["max_step_adverse_move_points"]
        == 120.0
    )


def test_runner_closes_submitted_cycle_positions_when_target_is_reached():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            total_volume=0.03,
            step_count=1,
            average_entry_price=100.0,
            last_entry_price=99.02,
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001},
                    {"scope": "step_1", "step_index": 1, "ticket": 7002},
                ]
            },
        )
    )
    positions = DummyPositionSnapshotProvider(
        positions=[
            {
                "ticket": 7001,
                "account_key": "demo:broker:1001",
                "symbol": "XAUUSD",
                "signal_id": "recovery:cycle-open:initial",
            },
            {
                "ticket": 7002,
                "account_key": "demo:broker:1001",
                "symbol": "XAUUSD",
                "signal_id": "recovery:cycle-open:step:1",
            },
        ],
        last_reconcile_at=_now().isoformat(),
    )
    runner, store, trading = _runner(
        store=store,
        settings=_settings(dry_run=False),
        position_snapshot_provider=positions,
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.06, ask=100.08))

    assert result["action"] == "close_cycle"
    assert result["status"] == "closed"
    assert [call[0] for call in trading.dispatch_calls] == ["close", "close"]
    assert [call[1]["ticket"] for call in trading.dispatch_calls] == [7001, 7002]
    assert all(
        call[1]["request_context"]["execution_scope"] == "recovery_cycle_cleanup"
        for call in trading.dispatch_calls
    )
    closed_cycle, kwargs = store.records[-1]
    assert closed_cycle.status == "closed"
    assert kwargs["status_reason"] == "resident_recovery_target_reached_submitted"
    assert kwargs["close_price"] == 100.06
    assert kwargs["realized_pnl"] is not None
    assert closed_cycle.metadata["close_decision"]["metadata"]["net_profit_points"] == 6.0
    assert closed_cycle.metadata["close_decision"]["metadata"]["target_net_points"] == 5.0
    assert result["close_result"]["status"] == "closed"
    assert runner.status()["active_cycle_id"] is None


def test_runner_closes_submitted_cycle_by_live_position_ticket_on_netting_account():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            total_volume=0.03,
            step_count=1,
            average_entry_price=100.0,
            last_entry_price=99.02,
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001},
                    {"scope": "step_1", "step_index": 1, "ticket": 7002},
                ]
            },
        )
    )
    positions = DummyPositionSnapshotProvider(
        positions=[
            {
                "ticket": 9001,
                "account_key": "demo:broker:1001",
                "symbol": "XAUUSD",
                "strategy": "tick_martingale_probe",
                "timeframe": "TICK",
                "signal_id": "recovery:cycle-open:step:1",
                "comment": "recovery-runner:cycle-open:s1",
            }
        ],
        last_reconcile_at=_now().isoformat(),
    )
    runner, store, trading = _runner(
        store=store,
        settings=_settings(dry_run=False),
        position_snapshot_provider=positions,
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.06, ask=100.08))

    assert result["action"] == "close_cycle"
    assert result["status"] == "closed"
    assert [call[0] for call in trading.dispatch_calls] == ["close"]
    assert [call[1]["ticket"] for call in trading.dispatch_calls] == [9001]
    assert trading.dispatch_calls[0][1]["request_context"][
        "submitted_tickets"
    ] == [7001, 7002]
    assert result["close_result"]["position_snapshot"]["matching_position_tickets"] == [
        9001
    ]
    assert runner.status()["active_cycle_id"] is None


def test_runner_accepts_operator_close_completed_result():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            total_volume=0.03,
            step_count=1,
            average_entry_price=100.0,
            last_entry_price=99.02,
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001},
                    {"scope": "step_1", "step_index": 1, "ticket": 7002},
                ]
            },
        )
    )
    trading = DummyOperatorClosePort()
    positions = DummyPositionSnapshotProvider(
        positions=[
            {
                "ticket": 7001,
                "account_key": "demo:broker:1001",
                "symbol": "XAUUSD",
                "signal_id": "recovery:cycle-open:initial",
            },
            {
                "ticket": 7002,
                "account_key": "demo:broker:1001",
                "symbol": "XAUUSD",
                "signal_id": "recovery:cycle-open:step:1",
            },
        ],
        last_reconcile_at=_now().isoformat(),
    )
    runner, store, trading = _runner(
        store=store,
        trading=trading,
        settings=_settings(dry_run=False),
        position_snapshot_provider=positions,
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.06, ask=100.08))

    assert result["action"] == "close_cycle"
    assert result["status"] == "closed"
    assert [call[1]["ticket"] for call in trading.dispatch_calls] == [7001, 7002]
    assert store.records[-1][0].status == "closed"
    assert runner.status()["active_cycle_id"] is None


def test_runner_treats_position_not_found_during_cleanup_as_already_closed():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            total_volume=0.03,
            step_count=1,
            average_entry_price=100.0,
            last_entry_price=99.02,
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001},
                    {"scope": "step_1", "step_index": 1, "ticket": 7002},
                ]
            },
        )
    )
    trading = DummyPositionNotFoundClosePort()
    positions = DummyPositionSnapshotProvider(
        positions=[
            {
                "ticket": 7001,
                "account_key": "demo:broker:1001",
                "symbol": "XAUUSD",
                "signal_id": "recovery:cycle-open:initial",
            },
            {
                "ticket": 7002,
                "account_key": "demo:broker:1001",
                "symbol": "XAUUSD",
                "signal_id": "recovery:cycle-open:step:1",
            },
        ],
        last_reconcile_at=_now().isoformat(),
    )
    runner, store, trading = _runner(
        store=store,
        trading=trading,
        settings=_settings(dry_run=False),
        position_snapshot_provider=positions,
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=100.06, ask=100.08))

    assert result["action"] == "close_cycle"
    assert result["status"] == "closed"
    assert [item["status"] for item in result["close_result"]["results"]] == [
        "already_closed",
        "already_closed",
    ]
    assert result["close_result"]["reason"] == "cleanup_positions_already_absent"
    closed_cycle, kwargs = store.records[-1]
    assert closed_cycle.status == "closed"
    assert kwargs["status_reason"] == "resident_recovery_target_reached_submitted"
    assert closed_cycle.metadata["cleanup_result"]["already_closed_tickets"] == [
        7001,
        7002,
    ]
    assert runner.status()["active_cycle_id"] is None


def test_runner_closes_submitted_cycle_when_position_snapshot_confirms_no_exposure():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            total_volume=0.03,
            step_count=1,
            average_entry_price=100.0,
            last_entry_price=99.02,
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001},
                    {"scope": "step_1", "step_index": 1, "ticket": 7002},
                ]
            },
        )
    )
    positions = DummyPositionSnapshotProvider(
        positions=[],
        last_reconcile_at=_now().isoformat(),
    )
    runner, store, trading = _runner(
        store=store,
        settings=_settings(dry_run=False),
        position_snapshot_provider=positions,
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=99.0, ask=99.02))

    assert result["action"] == "close_cycle"
    assert result["reason"] == "submitted_exposure_absent"
    assert trading.dispatch_calls == []
    closed_cycle, kwargs = store.records[-1]
    assert closed_cycle.status == "closed"
    assert kwargs["status_reason"] == "resident_recovery_submitted_exposure_absent"
    assert closed_cycle.metadata["exposure_absent_confirmation"]["submitted_tickets"] == [
        7001,
        7002,
    ]
    assert runner.status()["active_cycle_id"] is None


def test_runner_uses_cycle_direction_when_submitted_exposure_is_absent():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            direction="sell",
            total_volume=0.03,
            step_count=1,
            average_entry_price=100.0,
            last_entry_price=100.98,
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001},
                    {"scope": "step_1", "step_index": 1, "ticket": 7002},
                ]
            },
        )
    )
    positions = DummyPositionSnapshotProvider(
        positions=[],
        last_reconcile_at=_now().isoformat(),
    )
    runner, store, _ = _runner(
        store=store,
        settings=_settings(dry_run=False, direction="buy"),
        position_snapshot_provider=positions,
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=101.0, ask=101.02))

    assert result["reason"] == "submitted_exposure_absent"
    _, kwargs = store.records[-1]
    assert kwargs["close_price"] == 101.02
    assert kwargs["realized_pnl"] == -3.06


def test_runner_keeps_submitted_cycle_open_until_position_reconcile_is_fresh():
    stale_reconcile = datetime(2026, 5, 6, 11, 59, 59, tzinfo=timezone.utc).isoformat()
    store = DummyStateStore(
        open_cycle=_cycle_state(
            total_volume=0.03,
            step_count=1,
            average_entry_price=100.0,
            last_entry_price=99.02,
            updated_at=_now(),
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001},
                ]
            },
        )
    )
    positions = DummyPositionSnapshotProvider(
        positions=[],
        last_reconcile_at=stale_reconcile,
    )
    runner, store, _ = _runner(
        store=store,
        settings=_settings(dry_run=False),
        position_snapshot_provider=positions,
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=99.0, ask=99.02))

    assert result["action"] == "block"
    assert result["reason"] == "max_steps_reached"
    assert runner.status()["active_cycle_id"] == "cycle-open"
    assert runner.status()["exposure_ledger"]["status"] == "pending"
    assert runner.status()["exposure_ledger"]["pending_submitted_tickets"] == [7001]


def test_runner_keeps_submitted_cycle_open_when_matching_position_is_active():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            total_volume=0.03,
            step_count=1,
            average_entry_price=100.0,
            last_entry_price=99.02,
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001},
                ]
            },
        )
    )
    positions = DummyPositionSnapshotProvider(
        positions=[
            {
                "ticket": 7001,
                "symbol": "XAUUSD",
                "strategy": "tick_martingale_probe",
                "timeframe": "TICK",
                "signal_id": "sig-open",
            }
        ],
        last_reconcile_at=_now().isoformat(),
    )
    runner, store, _ = _runner(
        store=store,
        settings=_settings(dry_run=False),
        position_snapshot_provider=positions,
    )

    runner.start()
    result = runner.on_tick_feature_snapshot(_snapshot(bid=99.0, ask=99.02))

    assert result["action"] == "block"
    assert result["reason"] == "max_steps_reached"
    status = runner.status()
    assert status["active_cycle_id"] == "cycle-open"
    assert status["active_live_position_tickets"] == [7001]
    assert status["exposure_ledger"]["status"] == "confirmed"
    assert status["exposure_ledger"]["live_position_tickets"] == [7001]


def test_runner_status_exposes_pending_submitted_ticket_before_snapshot_confirms():
    store = DummyStateStore(
        open_cycle=_cycle_state(
            metadata={
                "submitted_tickets": [
                    {"scope": "initial", "step_index": 0, "ticket": 7001}
                ]
            }
        )
    )
    runner, _, _ = _runner(
        store=store,
        settings=_settings(dry_run=False),
        position_snapshot_provider=None,
    )

    runner.start()
    status = runner.status()

    assert status["active_submitted_tickets"] == [7001]
    assert status["active_live_position_tickets"] == []
    assert status["exposure_ledger"] == {
        "status": "pending",
        "fresh": False,
        "submitted_tickets": [7001],
        "live_position_tickets": [],
        "pending_submitted_tickets": [7001],
        "position_matches": [],
        "last_reconcile_at": None,
        "absent_confirmed": False,
    }
