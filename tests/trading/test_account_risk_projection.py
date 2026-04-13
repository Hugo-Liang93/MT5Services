from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.config.runtime_identity import RuntimeIdentity, build_account_key
from src.trading.state.risk_projection import AccountRiskStateProjector


def _runtime_identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        instance_name="live-exec-a",
        environment="live",
        instance_id="executor-live_exec_a",
        instance_role="executor",
        live_topology_mode="multi_account",
        account_alias="live_exec_a",
        account_label="Live Exec A",
        account_key=build_account_key("live", "Broker-Live", 1002),
        mt5_server="Broker-Live",
        mt5_login=1002,
        mt5_path="C:/MT5/live_exec_a/terminal64.exe",
    )


class _TradeModule:
    def trade_control_status(self) -> dict[str, object]:
        return {
            "auto_entry_enabled": False,
            "close_only_mode": True,
            "reason": "manual_close_only",
            "actor": "operator",
            "updated_at": "2026-04-12T12:00:00+00:00",
        }

    def account_info(self) -> dict[str, object]:
        return {"equity": 12000.0, "margin": 100.0, "margin_level": 12000.0}


class _TradeExecutor:
    def status(self) -> dict[str, object]:
        return {
            "last_risk_block": "margin_block_new_trades",
            "circuit_breaker": {
                "open": True,
                "consecutive_failures": 3,
            },
        }


class _PositionManager:
    def status(self) -> dict[str, object]:
        return {
            "tracked_positions": 2,
            "reconcile_count": 11,
            "last_reconcile_at": "2026-04-12T12:00:05+00:00",
            "margin_guard": {
                "margin_level": 145.0,
                "state": "danger",
                "should_block_new_trades": True,
                "should_tighten_stops": True,
                "should_emergency_close": False,
            },
        }

    def active_positions(self) -> list[dict[str, object]]:
        return [{"symbol": "XAUUSD", "ticket": 11}]


class _PendingEntryManager:
    def status(self) -> dict[str, object]:
        return {"active_count": 1}

    def active_execution_contexts(self) -> list[dict[str, object]]:
        return [{"symbol": "XAUUSD", "signal_id": "sig-1"}]


class _RuntimeModeController:
    def snapshot(self) -> dict[str, object]:
        return {"current_mode": "risk_off"}


class _MarketService:
    def __init__(self, *, quote_age_seconds: float = 5.0) -> None:
        self.market_settings = SimpleNamespace(
            quote_stale_seconds=1.0,
            stream_interval_seconds=1.0,
        )
        self._quote_age_seconds = quote_age_seconds

    def get_quote(self, symbol: str):
        assert symbol == "XAUUSD"
        return SimpleNamespace(
            bid=3200.0,
            ask=3200.5,
            time=datetime.now(timezone.utc) - timedelta(seconds=self._quote_age_seconds),
        )


def test_account_risk_state_projector_builds_local_risk_snapshot() -> None:
    rows: list[tuple] = []
    projector = AccountRiskStateProjector(
        write_fn=lambda batch: rows.extend(batch),
        runtime_identity=_runtime_identity(),
        trade_module=_TradeModule(),
        trade_executor=_TradeExecutor(),
        position_manager=_PositionManager(),
        pending_entry_manager=_PendingEntryManager(),
        runtime_mode_controller=_RuntimeModeController(),
        market_service=_MarketService(quote_age_seconds=5.0),
    )

    snapshot = projector.build_snapshot()
    persisted = projector.project_now()

    assert snapshot["account_alias"] == "live_exec_a"
    assert snapshot["runtime_mode"] == "risk_off"
    assert snapshot["should_block_new_trades"] is True
    assert snapshot["open_positions_count"] == 2
    assert snapshot["pending_orders_count"] == 1
    assert snapshot["quote_stale"] is True
    assert "runtime_mode:risk_off" in snapshot["active_risk_flags"]
    assert "close_only_mode" in snapshot["active_risk_flags"]
    assert "execution_circuit_open" in snapshot["active_risk_flags"]
    assert "margin_block_new_trades" in snapshot["active_risk_flags"]
    assert rows and rows[0][0] == _runtime_identity().account_key
    assert rows[0][10] == 145.0
    assert rows[0][12] is True
    assert persisted is not None
    assert projector.status()["db_degraded"] is False


def test_account_risk_state_projector_does_not_flag_open_market_quote_as_stale() -> None:
    projector = AccountRiskStateProjector(
        write_fn=lambda batch: None,
        runtime_identity=_runtime_identity(),
        trade_module=_TradeModule(),
        trade_executor=_TradeExecutor(),
        position_manager=_PositionManager(),
        pending_entry_manager=_PendingEntryManager(),
        runtime_mode_controller=_RuntimeModeController(),
        market_service=_MarketService(quote_age_seconds=1.6),
    )

    snapshot = projector.build_snapshot()

    assert snapshot["quote_stale"] is False
    assert "quote_stale" not in snapshot["active_risk_flags"]
    assert snapshot["metadata"]["quote_health"]["age_seconds"] is not None
    assert snapshot["metadata"]["quote_health"]["stale_threshold_seconds"] == 3.0
