from __future__ import annotations

from datetime import datetime, timezone

from src.api.trade import (
    orders,
    positions,
    trade,
    trade_daily_summary,
    trade_dispatch,
    trade_from_signal,
    trade_precheck,
)
from src.api.schemas import SignalExecuteTradeRequest, TradeDispatchRequest, TradeRequest
from src.clients.mt5_account import Order, Position
from src.clients.base import MT5TradeError
from src.risk.service import PreTradeRiskBlockedError


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
        if operation == "blocked_daily_loss_trade":
            raise PreTradeRiskBlockedError(
                "blocked by risk",
                assessment={"action": "block", "checks": [{"name": "daily_loss_limit"}]},
            )
        raise ValueError("unsupported trading operation")

    def daily_trade_summary(self):
        return {"date": "2026-01-01", "total": 0, "success": 0, "failed": 0}

    def execute_trade(self, **kwargs):
        if kwargs.get("symbol") == "XAUUSD_DAILY_LOSS":
            raise PreTradeRiskBlockedError(
                "blocked by risk",
                assessment={"action": "block", "checks": [{"name": "daily_loss_limit"}]},
            )
        return {
            "ticket": 1,
            "symbol": kwargs["symbol"],
            "request_id": kwargs.get("request_id") or "req_1",
            "trace_id": "trace_1",
            "operation_id": "op_1",
        }

    def account_info(self):
        return {"equity": 10000.0}

    def get_positions(self, symbol=None, magic=None):
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
                magic=magic or 7,
                comment="probe",
            )
        ]

    def get_orders(self, symbol=None, magic=None):
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
                magic=magic or 7,
                comment="probe",
            )
        ]


class _SignalService:
    @staticmethod
    def recent_signals(scope="confirmed", limit=500):
        return [
            {
                "signal_id": "sig_1",
                "symbol": "XAUUSD",
                "strategy": "consensus",
                "action": "buy",
                "indicators_snapshot": {
                    "atr14": {"value": 5.0},
                    "close": {"close": 2350.0},
                },
            }
        ]


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


def test_trade_dispatch_returns_daily_loss_limit_error() -> None:
    response = trade_dispatch(
        TradeDispatchRequest(operation="blocked_daily_loss_trade", payload={}),
        service=_DispatchService(),
    )

    assert response.success is False
    assert response.error["code"] == "daily_loss_limit"


def test_trade_endpoint_exposes_standardized_observability_metadata() -> None:
    response = trade(
        TradeRequest(symbol="XAUUSD", volume=0.1, side="buy", dry_run=True, request_id="req_x"),
        service=_DispatchService(),
    )

    assert response.success is True
    assert response.metadata["request_id"] == "req_x"
    assert response.metadata["trace_id"] == "trace_1"
    assert response.metadata["operation_id"] == "op_1"


def test_trade_endpoint_maps_daily_loss_limit_error_code() -> None:
    response = trade(
        TradeRequest(symbol="XAUUSD_DAILY_LOSS", volume=0.1, side="buy"),
        service=_DispatchService(),
    )

    assert response.success is False
    assert response.error["code"] == "daily_loss_limit"


def test_trade_from_signal_is_executed_by_trade_module_api() -> None:
    response = trade_from_signal(
        SignalExecuteTradeRequest(signal_id="sig_1"),
        signal_service=_SignalService(),
        service=_DispatchService(),
    )

    assert response.success is True
    assert response.metadata["operation"] == "trade_from_signal"
    assert response.data["ticket"] == 1


def test_positions_endpoint_serializes_dataclass_time_without_duplicate_keyword() -> None:
    response = positions(symbol="XAUUSD", magic=7, service=_DispatchService())

    assert response.success is True
    assert response.data[0].time == "2026-01-01T00:00:00+00:00"


def test_orders_endpoint_serializes_dataclass_time_without_duplicate_keyword() -> None:
    response = orders(symbol="XAUUSD", magic=7, service=_DispatchService())

    assert response.success is True
    assert response.data[0].time == "2026-01-01T00:00:00+00:00"
