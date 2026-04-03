from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.trading.positions import TrackedPosition
from src.trading.state import TradingStateStore


class DummyDB:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.pending_rows = [
            {
                "account_alias": "default",
                "order_ticket": 7001,
                "signal_id": "sig-1",
                "request_id": "sig-1",
                "symbol": "XAUUSD",
                "direction": "buy",
                "strategy": "htf_h4_pullback",
                "timeframe": "M30",
                "category": "",
                "order_kind": "limit",
                "comment": "M30:htf_h4_pullba_rsig1",
                "entry_low": 4649.15,
                "entry_high": 4672.97,
                "trigger_price": 4649.15,
                "entry_price_requested": 4661.06,
                "stop_loss": 4583.65,
                "take_profit": 4731.02,
                "volume": 0.01,
                "atr_at_entry": 29.77,
                "confidence": 0.52,
                "regime": None,
                "created_at": now - timedelta(minutes=5),
                "expires_at": now + timedelta(minutes=30),
                "filled_at": None,
                "cancelled_at": None,
                "position_ticket": None,
                "deal_id": None,
                "fill_price": None,
                "status": "missing",
                "status_reason": "order_missing_without_position",
                "last_seen_at": now - timedelta(minutes=1),
                "metadata": {"order_kind": "limit", "params": {"atr_value": 29.77}},
                "updated_at": now - timedelta(minutes=1),
            }
        ]
        self.position_rows = []
        self.pending_writes = []
        self.position_writes = []

    def fetch_position_runtime_states(self, **kwargs):
        return list(self.position_rows)

    def fetch_pending_order_states(self, **kwargs):
        statuses = set(kwargs.get("statuses") or [])
        rows = list(self.pending_rows)
        if statuses:
            rows = [row for row in rows if row.get("status") in statuses]
        return rows[: kwargs.get("limit", 5000)]

    def write_pending_order_states(self, rows):
        batch = list(rows)
        self.pending_writes.extend(batch)

    def write_position_runtime_states(self, rows):
        batch = list(rows)
        self.position_writes.extend(batch)

    def fetch_trade_control_state(self, **kwargs):
        return None


def test_trading_state_store_closes_missing_pending_state_when_position_is_recovered() -> None:
    db = DummyDB()
    store = TradingStateStore(db, account_alias_getter=lambda: "default")
    store.warm_start()

    pos = TrackedPosition(
        ticket=237986634,
        signal_id="sig-1",
        symbol="XAUUSD",
        action="buy",
        entry_price=4649.15,
        stop_loss=4583.65,
        take_profit=4731.02,
        volume=0.01,
        atr_at_entry=32.75,
        timeframe="M30",
        strategy="htf_h4_pullback",
        confidence=0.52,
        comment="M30:htf_h4_pullback:limit",
        opened_at=datetime.now(timezone.utc),
    )

    store.record_position_tracked(pos, reason="recovered")

    assert len(db.pending_writes) == 1
    pending_row = db.pending_writes[0]
    assert pending_row[1] == 7001
    assert pending_row[25] == 237986634
    assert pending_row[28] == "filled"

    assert len(db.position_writes) == 1
    position_row = db.position_writes[0]
    assert position_row[1] == 237986634
    assert position_row[3] == 7001
