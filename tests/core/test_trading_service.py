from __future__ import annotations

from src.core.trading_service import TradingService


class DummyTradingClient:
    def __init__(self) -> None:
        self.open_trade_details_calls = []
        self.margin_calls = []
        self.close_batch_calls = []
        self.cancel_batch_calls = []

    def connect(self):
        return None

    def side_and_kind_to_order_type(self, side: str, order_kind: str = "market"):
        mapping = {
            ("buy", "market"): 0,
            ("sell", "market"): 1,
            ("buy", "limit"): 2,
            ("sell", "limit"): 3,
        }
        return mapping[(side, order_kind)]

    def open_trade_details(self, **kwargs):
        self.open_trade_details_calls.append(kwargs)
        return {"ticket": 12345, "price": kwargs.get("price") or 2345.6}

    def open_trade(self, **kwargs):
        return 12345

    def estimate_margin(self, **kwargs):
        self.margin_calls.append(kwargs)
        return 512.5

    def close_position(self, ticket: int, deviation: int = 20, comment: str = "", volume=None):
        return True

    def close_positions_by_tickets(self, tickets, deviation=20, comment="close_batch"):
        self.close_batch_calls.append((list(tickets), deviation, comment))
        return {"closed": list(tickets), "failed": []}

    def cancel_orders_by_tickets(self, tickets):
        self.cancel_batch_calls.append(list(tickets))
        return {"canceled": list(tickets), "failed": []}


class DummyRiskService:
    def __init__(self) -> None:
        self.calls = []

    def assess_trade(self, **kwargs):
        self.calls.append(("assess", kwargs))
        return {
            "enabled": True,
            "mode": "warn_only",
            "blocked": False,
            "action": "allow",
            "reason": None,
            "symbol": kwargs["symbol"],
            "active_windows": [],
            "upcoming_windows": [],
            "warnings": [],
            "checks": [],
            "intent": {"symbol": kwargs["symbol"], "volume": kwargs.get("volume")},
        }

    def enforce_trade_allowed(self, **kwargs):
        self.calls.append(("enforce", kwargs))
        return self.assess_trade(**kwargs)


def test_precheck_trade_uses_full_trade_context():
    client = DummyTradingClient()
    risk_service = DummyRiskService()
    service = TradingService(client=client, pre_trade_risk_service=risk_service)

    result = service.precheck_trade(
        symbol="XAUUSD",
        volume=0.4,
        side="buy",
        price=2350.0,
        sl=2340.0,
        tp=2365.0,
    )

    assert result["estimated_margin"] == 512.5
    call_name, call_payload = risk_service.calls[0]
    assert call_name == "assess"
    assert call_payload["volume"] == 0.4
    assert call_payload["side"] == "buy"
    assert call_payload["order_kind"] == "market"
    assert call_payload["sl"] == 2340.0
    assert call_payload["tp"] == 2365.0


def test_execute_trade_returns_structured_execution_details():
    client = DummyTradingClient()
    risk_service = DummyRiskService()
    service = TradingService(client=client, pre_trade_risk_service=risk_service)

    result = service.execute_trade(
        symbol="XAUUSD",
        volume=0.2,
        side="buy",
        price=2350.0,
        sl=2340.0,
        tp=2365.0,
    )

    assert result["ticket"] == 12345
    assert result["estimated_margin"] == 512.5
    assert result["pre_trade_risk"]["action"] == "allow"
    assert client.open_trade_details_calls[0]["sl"] == 2340.0


def test_execute_trade_supports_limit_order_kind():
    client = DummyTradingClient()
    risk_service = DummyRiskService()
    service = TradingService(client=client, pre_trade_risk_service=risk_service)

    result = service.execute_trade(
        symbol="XAUUSD",
        volume=0.2,
        side="buy",
        order_kind="limit",
        price=2348.0,
        sl=2340.0,
        tp=2365.0,
    )

    assert result["order_kind"] == "limit"
    assert client.open_trade_details_calls[0]["order_type"] == 2


def test_execute_trade_batch_collects_success_and_failures():
    client = DummyTradingClient()
    risk_service = DummyRiskService()
    service = TradingService(client=client, pre_trade_risk_service=risk_service)

    result = service.execute_trade_batch(
        trades=[
            {"symbol": "XAUUSD", "volume": 0.1, "side": "buy"},
            {"symbol": "XAUUSD", "volume": 0.1, "side": "hold"},
        ],
        stop_on_error=False,
    )

    assert result["success_count"] == 1
    assert result["failure_count"] == 1
    assert len(result["results"]) == 2


def test_close_positions_by_tickets_uses_client_batch_method():
    client = DummyTradingClient()
    service = TradingService(client=client)

    result = service.close_positions_by_tickets([101, 102], deviation=15, comment="batch_close")

    assert result["closed"] == [101, 102]
    assert client.close_batch_calls == [([101, 102], 15, "batch_close")]


def test_cancel_orders_by_tickets_uses_client_batch_method():
    client = DummyTradingClient()
    service = TradingService(client=client)

    result = service.cancel_orders_by_tickets([201, 202])

    assert result["canceled"] == [201, 202]
    assert client.cancel_batch_calls == [[201, 202]]
