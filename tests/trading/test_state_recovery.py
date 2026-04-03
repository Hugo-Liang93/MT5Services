from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.trading.state import TradingStateRecovery
from src.trading.state import TradingStateRecoveryPolicy


class DummyStateStore:
    def __init__(self):
        self.warm_started = False
        self.trade_control_state = None
        self.pending_rows = []
        self.events = []

    def warm_start(self) -> None:
        self.warm_started = True

    def load_trade_control_state(self):
        return self.trade_control_state

    def list_active_pending_orders(self):
        return list(self.pending_rows)

    def mark_pending_order_filled(self, info, *, state=None):
        self.events.append(("filled", info["ticket"], dict(state or {})))

    def mark_pending_order_missing(self, info, *, reason):
        self.events.append(("missing", info["ticket"], reason))

    def mark_pending_order_expired(self, info, *, reason):
        self.events.append(("expired", info["ticket"], reason))

    def mark_pending_order_orphan(self, order_row):
        self.events.append(("orphan", getattr(order_row, "ticket", None)))

    def mark_pending_order_cancelled(self, info, *, reason):
        self.events.append(("cancelled", info["ticket"], reason))


class DummyPendingEntryManager:
    def __init__(self, inspect_result=None):
        self.restored = []
        self.inspect_result = inspect_result or {"status": "missing", "reason": "not_found"}

    def restore_mt5_order(self, info):
        self.restored.append(dict(info))

    def inspect_mt5_order(self, info):
        return dict(self.inspect_result)


class DummyTradingModule:
    def __init__(self, orders=None):
        self.orders = list(orders or [])
        self.applied_control = None
        self.cancelled = []

    def apply_trade_control_state(self, state):
        self.applied_control = dict(state)
        return self.applied_control

    def get_orders(self):
        return list(self.orders)

    def cancel_orders_by_tickets(self, tickets):
        self.cancelled.extend(list(tickets))
        return {"canceled": list(tickets), "failed": []}


def test_trading_state_recovery_restores_trade_control() -> None:
    store = DummyStateStore()
    store.trade_control_state = {
        "auto_entry_enabled": False,
        "close_only_mode": True,
        "updated_at": datetime(2026, 4, 2, 9, 0, tzinfo=timezone.utc),
        "reason": "manual_pause",
    }
    recovery = TradingStateRecovery(store)
    trading = DummyTradingModule()

    result = recovery.restore_trade_control(trading)

    assert result == {"restored": True}
    assert trading.applied_control["close_only_mode"] is True


def test_trading_state_recovery_restores_live_pending_orders_and_marks_orphans() -> None:
    store = DummyStateStore()
    store.pending_rows = [
        {
            "order_ticket": 1001,
            "signal_id": "sig-1",
            "symbol": "XAUUSD",
            "direction": "buy",
            "strategy": "trend_vote",
            "timeframe": "M15",
            "comment": "M15:trend_vote:limit",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "metadata": {"params": {"entry_price": 3000.0, "stop_loss": 2990.0, "take_profit": 3020.0, "position_size": 0.1, "atr_value": 5.0}},
        }
    ]
    trading = DummyTradingModule(
        orders=[
            SimpleNamespace(ticket=1001, symbol="XAUUSD", type=2, comment="M15:trend_vote:limit"),
            SimpleNamespace(ticket=9999, symbol="XAUUSD", type=2, comment="manual"),
        ]
    )
    pending_manager = DummyPendingEntryManager()
    recovery = TradingStateRecovery(store)

    result = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )

    assert result["restored"] == 1
    assert result["orphan"] == 1
    assert pending_manager.restored[0]["ticket"] == 1001
    assert ("orphan", 9999) in store.events


def test_trading_state_recovery_marks_missing_orders_when_not_found() -> None:
    store = DummyStateStore()
    store.pending_rows = [
        {
            "order_ticket": 1002,
            "signal_id": "sig-2",
            "symbol": "XAUUSD",
            "direction": "sell",
            "strategy": "trend_vote",
            "timeframe": "M15",
            "comment": "M15:trend_vote:limit",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "metadata": {},
        }
    ]
    trading = DummyTradingModule(orders=[])
    pending_manager = DummyPendingEntryManager(
        inspect_result={"status": "missing", "reason": "startup_missing"}
    )
    recovery = TradingStateRecovery(store)

    result = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )

    assert result["missing"] == 1
    assert ("missing", 1002, "startup_missing") in store.events


def test_trading_state_recovery_uses_policy_for_orphan_and_missing_actions() -> None:
    store = DummyStateStore()
    store.pending_rows = [
        {
            "order_ticket": 1003,
            "signal_id": "sig-3",
            "symbol": "XAUUSD",
            "direction": "sell",
            "strategy": "trend_vote",
            "timeframe": "M15",
            "comment": "M15:trend_vote:limit",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "metadata": {},
        }
    ]
    trading = DummyTradingModule(
        orders=[SimpleNamespace(ticket=9998, symbol="XAUUSD", type=2, comment="manual")]
    )
    pending_manager = DummyPendingEntryManager(
        inspect_result={"status": "missing", "reason": "startup_missing"}
    )
    policy = TradingStateRecoveryPolicy(
        store,
        orphan_action="cancel",
        missing_action="ignore",
    )
    recovery = TradingStateRecovery(store, policy=policy)

    result = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )

    assert result["ignored_missing"] == 1
    assert result["orphan_cancelled"] == 1
    assert ("missing", 1003, "startup_missing") not in store.events
    assert ("orphan", 9998) in store.events
