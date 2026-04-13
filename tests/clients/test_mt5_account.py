from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.clients.mt5_account import MT5AccountClient


def test_account_info_exposes_optional_daily_risk_fields() -> None:
    client = MT5AccountClient.__new__(MT5AccountClient)
    client._account_info_cache = None
    client._positions_cache = {}
    client.connect = lambda: None

    with patch("src.clients.mt5_account.mt5") as mock_mt5:
        mock_mt5.account_info.return_value = SimpleNamespace(
            login=50256386,
            balance=3.7,
            equity=3.7,
            margin=0.0,
            margin_free=3.7,
            profit=0.0,
            leverage=1000,
            currency="USD",
        )

        info = client.account_info()

    assert info.login == 50256386
    assert info.day_start_balance is None
    assert info.daily_realized_pnl is None
    assert info.daily_pnl is None
