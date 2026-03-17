from __future__ import annotations

from src.api.trade import trade_daily_summary, trade_dispatch, trade_precheck
from src.api.schemas import TradeDispatchRequest, TradeRequest
from src.clients.mt5_trade import MT5TradeError
from src.core.pretrade_risk_service import PreTradeRiskBlockedError


class _FailingTradeService:
    active_account_alias = "live"

    def precheck_trade(self, **kwargs):
        raise MT5TradeError("calendar unavailable")


class _DispatchService:
    active_account_alias = "live"

    def dispatch_operation(self, operation, payload):
        if operation == "trade":
            return {"ticket": 1, "payload": payload}
        if operation == "blocked_trade":
            raise PreTradeRiskBlockedError("blocked by risk", assessment={"action": "block"})
        raise ValueError("unsupported trading operation")

    def daily_trade_summary(self):
        return {"date": "2026-01-01", "total": 0, "success": 0, "failed": 0}


def test_trade_precheck_wraps_mt5_errors() -> None:
    response = trade_precheck(
        TradeRequest(symbol="XAUUSD", volume=0.1, side="buy"),
        service=_FailingTradeService(),
    )

    assert response.success is False
    assert response.error is not None
    assert response.error["message"] == "Trade precheck failed: calendar unavailable"
    assert response.error["details"]["account_alias"] == "live"


def test_trade_dispatch_uses_unified_dispatcher() -> None:
    response = trade_dispatch(
        TradeDispatchRequest(operation="trade", payload={"symbol": "XAUUSD", "volume": 0.1, "side": "buy"}),
        service=_DispatchService(),
    )

    assert response.success is True
    assert response.data["ticket"] == 1


def test_trade_daily_summary_endpoint() -> None:
    response = trade_daily_summary(service=_DispatchService())

    assert response.success is True
    assert response.data["date"] == "2026-01-01"


def test_trade_dispatch_returns_risk_block_error() -> None:
    response = trade_dispatch(
        TradeDispatchRequest(operation="blocked_trade", payload={}),
        service=_DispatchService(),
    )

    assert response.success is False
    assert response.error["code"] == "trade_blocked_by_risk"
