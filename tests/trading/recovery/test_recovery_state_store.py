from datetime import datetime, timezone

import pytest

from src.trading.recovery import RecoveryCycleState
from src.trading.state.store import TradingStateStore


class DummyRecoveryDB:
    def __init__(self):
        self.recovery_writes = []
        self.fetch_kwargs = None
        self.recovery_rows = []

    def write_recovery_cycle_states(self, rows):
        self.recovery_writes.extend(rows)

    def fetch_recovery_cycle_states(self, **kwargs):
        self.fetch_kwargs = kwargs
        return list(self.recovery_rows)


def _cycle(**overrides) -> RecoveryCycleState:
    values = {
        "cycle_id": "cycle-1",
        "account_key": "demo:broker:1001",
        "symbol": "XAUUSD",
        "direction": "buy",
        "status": "open",
        "base_volume": 0.01,
        "total_volume": 0.01,
        "step_count": 1,
        "average_entry_price": 2300.0,
        "last_entry_price": 2300.0,
        "started_at": datetime(2026, 5, 6, 1, 2, 3, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 6, 1, 2, 5, tzinfo=timezone.utc),
        "last_step_at": datetime(2026, 5, 6, 1, 2, 4, tzinfo=timezone.utc),
        "strategy": "tick_martingale_probe",
        "timeframe": "TICK",
        "source_signal_id": "sig-1",
        "metadata": {"probe": True},
    }
    values.update(overrides)
    return RecoveryCycleState(**values)


def test_trading_state_store_records_recovery_cycle_with_account_scope():
    db = DummyRecoveryDB()
    store = TradingStateStore(
        db,
        account_alias_getter=lambda: "demo-main",
        account_key_getter=lambda: "demo:broker:1001",
    )

    store.record_recovery_cycle_state(_cycle(), status_reason="initial_opened")

    assert len(db.recovery_writes) == 1
    row = db.recovery_writes[0]
    assert row[0] == "demo-main"
    assert row[1] == "demo:broker:1001"
    assert row[2] == "cycle-1"
    assert row[3] == "XAUUSD"
    assert row[5] == "tick_martingale_probe"
    assert row[6] == "TICK"
    assert row[7] == "sig-1"
    assert row[8] == "open"
    assert row[9] == "initial_opened"
    assert row[11] == pytest.approx(0.01)
    assert row[12] == 1
    assert row[20] == {"probe": True}


def test_trading_state_store_rejects_recovery_cycle_account_mismatch():
    db = DummyRecoveryDB()
    store = TradingStateStore(
        db,
        account_alias_getter=lambda: "demo-main",
        account_key_getter=lambda: "demo:broker:1001",
    )

    with pytest.raises(ValueError, match="account_key"):
        store.record_recovery_cycle_state(_cycle(account_key="other:account"))


def test_trading_state_store_loads_open_recovery_cycle_for_strategy_symbol():
    db = DummyRecoveryDB()
    db.recovery_rows = [
        {
            "account_alias": "demo-main",
            "account_key": "demo:broker:1001",
            "cycle_id": "cycle-1",
            "symbol": "XAUUSD",
            "direction": "buy",
            "strategy": "tick_martingale_probe",
            "timeframe": "TICK",
            "source_signal_id": "sig-1",
            "status": "open",
            "status_reason": "active",
            "base_volume": 0.01,
            "total_volume": 0.03,
            "step_count": 2,
            "average_entry_price": 2298.5,
            "last_entry_price": 2297.0,
            "started_at": datetime(2026, 5, 6, 1, 2, 3, tzinfo=timezone.utc),
            "last_step_at": datetime(2026, 5, 6, 1, 2, 4, tzinfo=timezone.utc),
            "closed_at": None,
            "close_price": None,
            "realized_pnl": None,
            "metadata": {"probe": True},
            "updated_at": datetime(2026, 5, 6, 1, 2, 5, tzinfo=timezone.utc),
        }
    ]
    store = TradingStateStore(
        db,
        account_alias_getter=lambda: "demo-main",
        account_key_getter=lambda: "demo:broker:1001",
    )

    cycle = store.load_open_recovery_cycle(
        symbol="XAUUSD",
        strategy="tick_martingale_probe",
    )

    assert db.fetch_kwargs == {
        "account_key": "demo:broker:1001",
        "statuses": ["open"],
        "symbol": "XAUUSD",
        "strategy": "tick_martingale_probe",
        "cycle_id": None,
        "source_signal_id": None,
        "limit": 1,
    }
    assert cycle is not None
    assert cycle.cycle_id == "cycle-1"
    assert cycle.account_key == "demo:broker:1001"
    assert cycle.strategy == "tick_martingale_probe"
    assert cycle.step_count == 2
    assert cycle.metadata == {"probe": True}
