from __future__ import annotations

from src.api.trade import trade_precheck
from src.api.schemas import TradeRequest
from src.clients.mt5_trade import MT5TradeError


class _FailingTradeService:
    active_account_alias = "live"

    def precheck_trade(self, **kwargs):
        raise MT5TradeError("calendar unavailable")


def test_trade_precheck_wraps_mt5_errors() -> None:
    response = trade_precheck(
        TradeRequest(symbol="XAUUSD", volume=0.1, side="buy"),
        service=_FailingTradeService(),
    )

    assert response.success is False
    assert response.error is not None
    assert response.error["message"] == "Trade precheck failed: calendar unavailable"
    assert response.error["details"]["account_alias"] == "live"
