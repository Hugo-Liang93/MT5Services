from __future__ import annotations

from src.trading.state import TradingStateAlerts


class DummyStateStore:
    def list_pending_order_states(self, *, statuses=None, limit=100):
        rows = [
            {"order_ticket": 101, "status": "placed"},
            {"order_ticket": 102, "status": "orphan"},
            {"order_ticket": 103, "status": "missing"},
        ]
        if statuses:
            rows = [row for row in rows if row["status"] in set(statuses)]
        return rows[:limit]

    def list_position_runtime_states(self, *, statuses=None, limit=100):
        rows = [
            {"position_ticket": 201, "status": "open"},
        ]
        if statuses:
            rows = [row for row in rows if row["status"] in set(statuses)]
        return rows[:limit]


class DummyTradingModule:
    active_account_alias = "default"

    def get_orders(self):
        return [{"ticket": 501}]

    def get_positions(self):
        return [{"ticket": 601}, {"ticket": 602}]


def test_trading_state_alerts_reports_state_gaps_and_count_mismatches() -> None:
    alerts = TradingStateAlerts(
        state_store=DummyStateStore(),
        trading_module=DummyTradingModule(),
        account_alias_getter=lambda: "default",
    )

    summary = alerts.summary()

    assert summary["status"] == "critical"
    codes = {item["code"] for item in summary["alerts"]}
    assert "pending_missing" in codes
    assert "pending_orphan" in codes
    assert "pending_active_mismatch" in codes
    assert "unmanaged_live_positions" in codes
    assert summary["observed"]["active_pending_count"] == 2
    assert summary["observed"]["live_position_count"] == 2
