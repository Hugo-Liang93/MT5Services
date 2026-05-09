from __future__ import annotations

from datetime import datetime, timezone

from src.market.tick_features.health import TickFeatureHealth
from src.readmodels.runtime import RuntimeReadModel


class _SignalRuntime:
    def __init__(self, lanes: tuple[str, ...]) -> None:
        self._lanes = lanes

    def required_market_data_lanes(self):
        return self._lanes


class _Bus:
    def stats(self):
        return {"queue_depth": 2, "dropped_snapshots": 1}


class _HealthStore:
    def __init__(self, status: str) -> None:
        self.status = status

    def health_for(self, symbol, *, now=None, bus_stats=None):
        return TickFeatureHealth(
            symbol=symbol,
            status=self.status,
            last_snapshot_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_window_end_msc=1_767_225_600_000,
            snapshot_age_seconds=5.0,
            queue_depth=int((bus_stats or {}).get("queue_depth") or 0),
            dropped_snapshots=int((bus_stats or {}).get("dropped_snapshots") or 0),
            last_reasons=("snapshot_stale",) if self.status == "stale" else (),
        )


class _HealthStoreWithForbiddenPrivateProjection(_HealthStore):
    def health_payload(self, symbol, *, now=None, bus_stats=None):
        return self.health_for(symbol, now=now, bus_stats=bus_stats).to_dict()

    def _health_to_dict(self, health):  # pragma: no cover - must not be called
        raise AssertionError("private health projection must not be used")


def test_tick_feature_health_summary_reports_required_symbol_health() -> None:
    read_model = RuntimeReadModel(
        signal_runtime=_SignalRuntime(("tick:XAUUSD",)),
        tick_feature_health_store=_HealthStore("healthy"),
        tick_feature_bus=_Bus(),
    )

    payload = read_model.tick_feature_health_summary()

    assert payload["status"] == "healthy"
    assert payload["blocking"] is False
    assert payload["symbols"]["XAUUSD"]["queue_depth"] == 2


def test_tick_feature_health_summary_uses_public_projection_port() -> None:
    read_model = RuntimeReadModel(
        signal_runtime=_SignalRuntime(("tick:XAUUSD",)),
        tick_feature_health_store=_HealthStoreWithForbiddenPrivateProjection("healthy"),
        tick_feature_bus=_Bus(),
    )

    payload = read_model.tick_feature_health_summary()

    assert payload["symbols"]["XAUUSD"]["status"] == "healthy"


def test_tradability_exposes_stale_tick_feature_without_blocking_confirmed_route() -> None:
    read_model = RuntimeReadModel(
        trade_executor=object(),
        signal_runtime=_SignalRuntime(("tick:XAUUSD",)),
        tick_feature_health_store=_HealthStore("stale"),
        tick_feature_bus=_Bus(),
    )

    payload = read_model.tradability_state_summary()

    assert payload["verdict"] == "tradable"
    assert payload["tick_derived_tradable"] is False
    assert payload["tick_feature_health"]["blocking"] is True
    assert payload["tick_feature_health"]["blocked_symbols"] == ["XAUUSD"]


def test_tick_feature_health_is_not_required_when_runtime_has_no_tick_lanes() -> None:
    read_model = RuntimeReadModel(
        trade_executor=object(),
        signal_runtime=_SignalRuntime(("ohlc:XAUUSD:M5",)),
        tick_feature_health_store=_HealthStore("stale"),
        tick_feature_bus=_Bus(),
    )

    payload = read_model.tick_feature_health_summary()

    assert payload["status"] == "not_required"
    assert payload["blocking"] is False
