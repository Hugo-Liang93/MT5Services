from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.app_runtime.builder_phases.recovery import build_recovery_runtime_layer
from src.app_runtime.container import AppContainer
from src.config.models import RiskConfig
from src.market.tick_features.models import TickFeatureSnapshot
from src.utils.common import same_listener_reference


class _MarketService:
    def __init__(self) -> None:
        self.listeners = []

    def add_tick_batch_listener(self, listener) -> None:
        self.listeners.append(listener)

    def remove_tick_batch_listener(self, listener) -> None:
        self.listeners = [
            item for item in self.listeners if not same_listener_reference(item, listener)
        ]

    def add_quote_listener(self, listener) -> None:
        self.listeners.append(listener)

    def remove_quote_listener(self, listener) -> None:
        self.listeners = [
            item for item in self.listeners if not same_listener_reference(item, listener)
        ]


class _StateStore:
    def __init__(self):
        self.records = []
        self.cycle_rows = []

    def load_open_recovery_cycle(self, *, symbol, strategy=None, cycle_id=None):
        return None

    def record_recovery_cycle_state(self, cycle, **kwargs):
        self.records.append((cycle, kwargs))

    def list_recovery_cycle_states(self, **kwargs):
        return list(self.cycle_rows)


class _TradingPort:
    active_account_alias = "demo-main"
    active_account_key = "demo:broker:1001"

    def __init__(self):
        self.dispatch_calls = []

    def dispatch_operation(self, operation, payload=None):
        self.dispatch_calls.append((operation, dict(payload or {})))
        return {"status": "ok", "dry_run": bool((payload or {}).get("dry_run"))}


def _risk_config() -> RiskConfig:
    return RiskConfig.model_validate(
        {
            "recovery_runtime_runner": {
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
                "protective_stop_points": 80.0,
                "snapshot_stale_seconds": 5.0,
            }
        }
    )


def _snapshot() -> TickFeatureSnapshot:
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    return TickFeatureSnapshot(
        symbol="XAUUSD",
        window_start_msc=1_000,
        window_end_msc=2_000,
        generated_at=now,
        tick_count=10,
        bid=100.0,
        ask=100.02,
        last=100.01,
        mid=100.01,
        spread_points=2.0,
        quote_age_ms=100,
        realized_range_points=5.0,
        price_change_points=1.0,
        buy_pressure=0.6,
        sell_pressure=0.4,
        status="healthy",
        reasons=(),
    )


def _container() -> tuple[AppContainer, _StateStore, _TradingPort]:
    container = AppContainer()
    state_store = _StateStore()
    trading_port = _TradingPort()
    container.market_service = _MarketService()
    container.trading_state_store = state_store
    container.trade_module = trading_port
    return container, state_store, trading_port


def test_recovery_runtime_layer_creates_runner_and_tick_feature_listener():
    container, state_store, trading_port = _container()

    build_recovery_runtime_layer(container, risk_config=_risk_config())

    assert container.recovery_runner is not None
    assert container.tick_feature_engine is not None
    assert container.tick_feature_bus is not None
    assert container.tick_feature_bus.stats()["listeners"] == 1
    assert len(container.market_service.listeners) == 2

    container.recovery_runner.start()
    container.tick_feature_bus.publish(_snapshot())

    assert len(state_store.records) == 1
    assert trading_port.dispatch_calls[0][0] == "trade"
    assert trading_port.dispatch_calls[0][1]["dry_run"] is True


def test_recovery_runtime_layer_applies_recovery_profile_budget():
    container, _, _ = _container()
    risk_config = _risk_config()
    risk_config.risk_profiles["recovery_budgeted"].max_daily_recovery_loss_amount = 25.0
    risk_config.risk_profiles["recovery_budgeted"].max_rolling_recovery_loss_amount = 10.0
    risk_config.risk_profiles["recovery_budgeted"].rolling_loss_window_minutes = 45
    risk_config.risk_profiles["recovery_budgeted"].max_consecutive_loss_cycles = 2
    risk_config.risk_profiles["recovery_budgeted"].loss_lockout_minutes = 30

    build_recovery_runtime_layer(container, risk_config=risk_config)

    assert container.recovery_runner is not None
    risk_budget = container.recovery_runner.status()["risk_budget"]
    assert risk_budget["profile"] == "recovery_budgeted"
    assert risk_budget["max_daily_recovery_loss_amount"] == 25.0
    assert risk_budget["max_rolling_recovery_loss_amount"] == 10.0
    assert risk_budget["rolling_loss_window_minutes"] == 45
    assert risk_budget["max_consecutive_loss_cycles"] == 2
    assert risk_budget["loss_lockout_minutes"] == 30


def test_recovery_runtime_layer_exposes_risk_profile_contract():
    container, _, _ = _container()
    risk_config = _risk_config()

    build_recovery_runtime_layer(container, risk_config=risk_config)

    assert container.recovery_runner is not None
    contract = container.recovery_runner.status()["risk_profile_contract"]
    assert contract["name"] == "recovery_budgeted"
    assert contract["policy"] == "recovery_budgeted"
    assert contract["trade_frequency_enabled"] is False
    assert contract["source"] == "recovery_runner"
    assert contract["pre_trade_rules"] == [
        "account_snapshot",
        "daily_loss_limit",
        "margin_availability",
        "protection",
        "session_window",
        "economic_event",
        "calendar_health",
    ]


def test_recovery_runtime_layer_rejects_strategy_profile_binding_mismatch():
    container, _, _ = _container()
    risk_config = _risk_config()
    risk_config.risk_profile_bindings["tick_martingale_probe"] = "standard_kline"

    with pytest.raises(ValueError, match="risk_profile_binding_mismatch"):
        build_recovery_runtime_layer(container, risk_config=risk_config)


def test_recovery_runtime_layer_disabled_leaves_tick_feature_runtime_untouched():
    container, _, _ = _container()

    build_recovery_runtime_layer(container, risk_config=RiskConfig())

    assert container.recovery_runner is None
    assert container.tick_feature_engine is None
    assert container.tick_feature_bus is None
