from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.risk.service import PreTradeRiskBlockedError
from src.trading.trading_service import TradingService


class DummyTradingClient:
    def __init__(self) -> None:
        self.open_trade_details_calls = []
        self.margin_calls = []
        self.close_batch_calls = []
        self.cancel_batch_calls = []
        self.fail_open_times = 0

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
        if self.fail_open_times > 0:
            self.fail_open_times -= 1
            raise RuntimeError("transient execution error")
        self.open_trade_details_calls.append(kwargs)
        return {"ticket": 12345, "price": kwargs.get("price") or 2345.6}

    def open_trade(self, **kwargs):
        return 12345

    def estimate_margin(self, **kwargs):
        self.margin_calls.append(kwargs)
        return 512.5

    def validate_trade_request(self, **kwargs):
        if kwargs.get("sl") == -1:
            raise RuntimeError("Stop loss must be below entry price for buy orders")
        if kwargs.get("tp") == -2:
            raise RuntimeError("Take profit must be above entry price for buy orders")
        return {
            "order_type": self.side_and_kind_to_order_type(kwargs["side"], kwargs.get("order_kind", "market")),
            "request_price": kwargs.get("price") or 2345.6,
            "pending": False,
        }

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
            "intent": {
                "symbol": kwargs["symbol"],
                "volume": kwargs.get("volume"),
                "metadata": kwargs.get("metadata") or {},
            },
        }

    def enforce_trade_allowed(self, **kwargs):
        self.calls.append(("enforce", kwargs))
        return self.assess_trade(**kwargs)


class TradeGuardBlockingRiskService(DummyRiskService):
    def assess_trade(self, **kwargs):
        assessment = super().assess_trade(**kwargs)
        assessment["event_blocked"] = True
        assessment["warnings"] = ["calendar_window_active"]
        return assessment


class DummyAccountClient:
    def __init__(self, *, positions=None, orders=None) -> None:
        self._positions = list(positions or [])
        self._orders = list(orders or [])

    def positions(self, symbol=None):
        if symbol is None:
            return list(self._positions)
        return [row for row in self._positions if getattr(row, "symbol", None) == symbol]

    def orders(self, symbol=None):
        if symbol is None:
            return list(self._orders)
        return [row for row in self._orders if getattr(row, "symbol", None) == symbol]


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
        metadata={
            "market_structure": {
                "sweep_confirmation_state": "bullish_sweep_confirmed_previous_day_low",
            }
        },
    )

    assert result["estimated_margin"] == 512.5
    call_name, call_payload = risk_service.calls[0]
    assert call_name == "assess"
    assert call_payload["volume"] == 0.4
    assert call_payload["side"] == "buy"
    assert call_payload["order_kind"] == "market"
    assert call_payload["sl"] == 2340.0
    assert call_payload["tp"] == 2365.0
    assert call_payload["metadata"]["market_structure"]["sweep_confirmation_state"] == (
        "bullish_sweep_confirmed_previous_day_low"
    )


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
        metadata={
            "market_structure": {
                "structure_bias": "bullish_pullback",
            }
        },
    )

    assert result["ticket"] == 12345
    assert result["estimated_margin"] == 512.5
    assert result["pre_trade_risk"]["action"] == "allow"
    assert result["pre_trade_risk"]["intent"]["metadata"]["market_structure"]["structure_bias"] == (
        "bullish_pullback"
    )
    assert client.open_trade_details_calls[0]["sl"] == 2340.0
    assert result["state_consistency"]["positions_count"] == 0
    assert result["state_consistency"]["orders_count"] == 0


def test_injected_trading_client_does_not_create_real_account_client():
    client = DummyTradingClient()
    service = TradingService(client=client)

    assert service.account_client is None
    assert service.get_positions(symbol="XAUUSD") == []
    assert service.get_orders(symbol="XAUUSD") == []


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


def test_execute_trade_dry_run_returns_precheck_without_sending_order():
    client = DummyTradingClient()
    risk_service = DummyRiskService()
    service = TradingService(client=client, pre_trade_risk_service=risk_service)

    result = service.execute_trade(
        symbol="XAUUSD",
        volume=0.2,
        side="buy",
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["execution_attempts"] == 0
    assert client.open_trade_details_calls == []
    assert result["precheck"]["action"] == "allow"


def test_execute_trade_retries_on_transient_failure():
    client = DummyTradingClient()
    client.fail_open_times = 1
    risk_service = DummyRiskService()
    service = TradingService(client=client, pre_trade_risk_service=risk_service)

    result = service.execute_trade(
        symbol="XAUUSD",
        volume=0.2,
        side="buy",
        retry_attempts=2,
        retry_backoff_ms=0,
    )

    assert result["ticket"] == 12345
    assert result["execution_attempts"] == 2


def test_execute_trade_tags_comment_with_request_id() -> None:
    client = DummyTradingClient()
    service = TradingService(client=client, pre_trade_risk_service=DummyRiskService())

    result = service.execute_trade(
        symbol="XAUUSD",
        volume=0.2,
        side="buy",
        comment="auto:sma:buy",
        request_id="REQ-ABC12345-TAIL",
    )

    assert client.open_trade_details_calls[0]["comment"] == "auto:sma:buy_rreqabc12"
    assert result["comment"] == "auto:sma:buy_rreqabc12"


def test_execute_trade_recovers_from_existing_position_state() -> None:
    client = DummyTradingClient()
    client.fail_open_times = 1
    account_client = DummyAccountClient(
        positions=[
            SimpleNamespace(
                ticket=9876,
                symbol="XAUUSD",
                volume=0.2,
                price_open=2351.5,
                comment="auto:sma:buy_rrecover1",
            )
        ]
    )
    service = TradingService(
        client=client,
        account_client=account_client,
        pre_trade_risk_service=DummyRiskService(),
    )

    result = service.execute_trade(
        symbol="XAUUSD",
        volume=0.2,
        side="buy",
        comment="auto:sma:buy",
        request_id="recover-1",
        retry_attempts=2,
        retry_backoff_ms=0,
    )

    assert result["ticket"] == 9876
    assert result["recovered_from_state"] is True
    assert result["state_source"] == "positions"
    assert result["execution_attempts"] == 1
    assert client.open_trade_details_calls == []


def test_execute_trade_blocks_xauusd_when_trade_guard_detects_active_window() -> None:
    client = DummyTradingClient()
    service = TradingService(
        client=client,
        pre_trade_risk_service=TradeGuardBlockingRiskService(),
    )

    with pytest.raises(PreTradeRiskBlockedError) as exc_info:
        service.execute_trade(
            symbol="XAUUSD",
            volume=0.2,
            side="buy",
        )

    assert exc_info.value.assessment["reason"] == "xauusd_trade_guard_blocked"
    assert exc_info.value.assessment["blocked"] is True
    assert client.open_trade_details_calls == []


def test_precheck_trade_blocks_non_positive_volume():
    client = DummyTradingClient()
    service = TradingService(client=client, pre_trade_risk_service=DummyRiskService())

    result = service.precheck_trade(symbol="XAUUSD", volume=0, side="buy")

    assert result["action"] == "block"
    assert result["executable"] is False
    assert result["suggested_adjustment"] == {"volume": 0.01}


def test_precheck_trade_blocks_invalid_trade_parameters_before_execution():
    client = DummyTradingClient()
    service = TradingService(client=client, pre_trade_risk_service=DummyRiskService())

    result = service.precheck_trade(
        symbol="XAUUSD",
        volume=0.1,
        side="buy",
        price=2350.0,
        sl=-1,
        tp=2365.0,
    )

    assert result["action"] == "block"
    assert result["executable"] is False
    assert result["reason"] == "Stop loss must be below entry price for buy orders"
    assert result["checks"][0]["name"] == "trade_parameters"
    assert result["suggested_adjustment"] == {"action": "review_trade_parameters"}


def test_execute_trade_stops_when_precheck_is_not_executable():
    client = DummyTradingClient()
    service = TradingService(client=client, pre_trade_risk_service=DummyRiskService())

    try:
        service.execute_trade(
            symbol="XAUUSD",
            volume=0.1,
            side="buy",
            price=2350.0,
            sl=2340.0,
            tp=-2,
        )
    except RuntimeError as exc:
        assert str(exc) == "Take profit must be above entry price for buy orders"
    else:
        raise AssertionError("expected execute_trade to stop on failed precheck")

    assert client.open_trade_details_calls == []


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
