from __future__ import annotations

from datetime import datetime, timezone

from src.trading.runtime.trade_frequency import TradeCommandAuditFrequencyProvider


class FakeTradeCommandStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.reservation_counts: list[dict] = []
        self.reservations: list[dict] = []
        self.finalized: list[dict] = []

    def count_successful_trade_commands_since(self, **kwargs) -> int:
        self.calls.append(dict(kwargs))
        return 4

    def count_trade_frequency_reservations_since(self, **kwargs) -> int:
        self.reservation_counts.append(dict(kwargs))
        return 2

    def reserve_trade_frequency_quota(self, **kwargs) -> str:
        self.reservations.append(dict(kwargs))
        return "reservation-db-1"

    def finalize_trade_frequency_reservation(self, **kwargs) -> None:
        self.finalized.append(dict(kwargs))


def test_trade_frequency_provider_counts_successful_execute_trade_by_account_key():
    store = FakeTradeCommandStore()
    provider = TradeCommandAuditFrequencyProvider(
        store,
        account_key="live:broker:123",
    )
    since = datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc)

    count = provider.count_trades_since(since)

    assert count == 6
    assert store.calls == [
        {
            "since": since,
            "account_key": "live:broker:123",
        }
    ]
    assert store.reservation_counts == [
        {
            "since": since,
            "account_key": "live:broker:123",
        }
    ]


def test_trade_frequency_provider_reserves_account_scoped_quota_slot():
    store = FakeTradeCommandStore()
    provider = TradeCommandAuditFrequencyProvider(
        store,
        account_key="live:broker:123",
    )
    at_time = datetime(2026, 5, 5, 1, 2, tzinfo=timezone.utc)

    reservation_id = provider.reserve_trade_slot(
        account_key="live:broker:123",
        at_time=at_time,
        max_trades_per_day=5,
        max_trades_per_hour=2,
    )
    provider.finalize_trade_slot(reservation_id, committed=True)

    assert reservation_id == "reservation-db-1"
    assert store.reservations == [
        {
            "account_key": "live:broker:123",
            "account_alias": None,
            "at_time": at_time,
            "max_trades_per_day": 5,
            "max_trades_per_hour": 2,
        }
    ]
    assert store.finalized == [
        {"reservation_id": "reservation-db-1", "committed": True}
    ]
