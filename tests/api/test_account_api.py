from __future__ import annotations

from datetime import datetime, timezone

from src.api.account import account_orders, account_positions
from src.clients.mt5_account import Order, Position


class _AccountService:
    active_account_alias = "default"

    def positions(self, symbol=None):
        return [
            Position(
                ticket=1,
                symbol=symbol or "XAUUSD",
                volume=0.01,
                price_open=3000.0,
                sl=2990.0,
                tp=3020.0,
                time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                type=0,
                magic=7,
                comment="probe",
            )
        ]

    def orders(self, symbol=None):
        return [
            Order(
                ticket=2,
                symbol=symbol or "XAUUSD",
                volume=0.01,
                price_open=3001.0,
                price_current=3002.0,
                sl=2991.0,
                tp=3021.0,
                time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                type=0,
                magic=7,
                comment="probe",
            )
        ]


def test_account_positions_serializes_dataclass_time_without_duplicate_keyword() -> None:
    response = account_positions(symbol="XAUUSD", svc=_AccountService())

    assert response.success is True
    assert response.data[0].time == "2026-01-01T00:00:00+00:00"


def test_account_orders_serializes_dataclass_time_without_duplicate_keyword() -> None:
    response = account_orders(symbol="XAUUSD", svc=_AccountService())

    assert response.success is True
    assert response.data[0].time == "2026-01-01T00:00:00+00:00"
