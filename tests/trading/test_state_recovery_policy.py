from __future__ import annotations

from types import SimpleNamespace

from src.trading.state_recovery_policy import TradingStateRecoveryPolicy


class DummyStateStore:
    def __init__(self):
        self.events = []

    def mark_pending_order_missing(self, info, *, reason):
        self.events.append(("missing", info["ticket"], reason))

    def mark_pending_order_orphan(self, order_row):
        self.events.append(("orphan", getattr(order_row, "ticket", None)))

    def mark_pending_order_cancelled(self, info, *, reason):
        self.events.append(("cancelled", info["ticket"], reason))


class DummyTradingModule:
    def __init__(self):
        self.cancelled = []

    def cancel_orders_by_tickets(self, tickets):
        self.cancelled.extend(list(tickets))
        return {"canceled": list(tickets), "failed": []}


def test_missing_policy_can_ignore_state_write() -> None:
    store = DummyStateStore()
    policy = TradingStateRecoveryPolicy(store, missing_action="ignore")

    outcome = policy.handle_missing_pending_order(
        {"ticket": 1001, "symbol": "XAUUSD", "direction": "buy"},
        state={"reason": "startup_missing"},
    )

    assert outcome == "ignored_missing"
    assert store.events == []


def test_orphan_policy_can_cancel_live_order_after_recording_orphan() -> None:
    store = DummyStateStore()
    trading = DummyTradingModule()
    policy = TradingStateRecoveryPolicy(store, orphan_action="cancel")
    order = SimpleNamespace(
        ticket=2001,
        symbol="XAUUSD",
        type=2,
        comment="manual",
        price_open=3000.0,
        sl=2990.0,
        tp=3020.0,
        volume_current=0.1,
        time_setup=None,
    )

    outcome = policy.handle_orphan_pending_order(order, trading_module=trading)

    assert outcome == "orphan_cancelled"
    assert trading.cancelled == [2001]
    assert store.events == [
        ("orphan", 2001),
        ("cancelled", 2001, "startup_orphan_cancelled"),
    ]
