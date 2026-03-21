from __future__ import annotations

from datetime import datetime, timezone

from src.api.trade import (
    orders,
    positions,
    trade,
    trade_control_status,
    trade_control_update,
    trade_daily_summary,
    trade_dispatch,
    trade_from_signal,
    trade_precheck,
    trade_reconcile,
)
from src.api.schemas import (
    SignalExecuteTradeRequest,
    TradeControlRequest,
    TradeDispatchRequest,
    TradeReconcileRequest,
    TradeRequest,
)
from src.clients.mt5_account import Order, Position
from src.clients.base import MT5TradeError
from src.risk.service import PreTradeRiskBlockedError


class _FailingTradeService:
    active_account_alias = "live"

    def precheck_trade(self, **kwargs):
        raise MT5TradeError("calendar unavailable")


class _DispatchService:
    active_account_alias = "live"

    def __init__(self) -> None:
        self._control = {
            "auto_entry_enabled": True,
            "close_only_mode": False,
            "updated_at": None,
            "reason": None,
        }
        self.last_dispatch = None

    def dispatch_operation(self, operation, payload):
        self.last_dispatch = (operation, payload)
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

    def trade_control_status(self):
        return dict(self._control)

    def update_trade_control(self, **kwargs):
        if kwargs.get("auto_entry_enabled") is not None:
            self._control["auto_entry_enabled"] = bool(kwargs["auto_entry_enabled"])
        if kwargs.get("close_only_mode") is not None:
            self._control["close_only_mode"] = bool(kwargs["close_only_mode"])
        self._control["reason"] = kwargs.get("reason") or None
        self._control["updated_at"] = "2026-01-01T00:00:00+00:00"
        return dict(self._control)

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
                "timeframe": "M5",
                "strategy": "consensus",
                "action": "buy",
                "confidence": 0.88,
                "metadata": {"regime": "trend"},
                "indicators_snapshot": {
                    "atr14": {"value": 5.0},
                    "close": {"close": 2350.0},
                },
            }
        ]


class _ExecutorService:
    def __init__(self) -> None:
        self.reset_calls = 0

    def status(self):
        return {
            "circuit_open": False,
            "execution_quality": {"risk_blocks": 0, "recovered_from_state": 0},
        }

    def reset_circuit(self):
        self.reset_calls += 1


class _PositionManagerService:
    def sync_open_positions(self):
        return {"synced": 1, "recovered": 1, "skipped": 0}

    def status(self):
        return {"tracked_positions": 1, "running": True}

    def active_positions(self):
        return [{"ticket": 1, "signal_id": "sig_1", "symbol": "XAUUSD"}]


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


def test_trade_control_status_endpoint() -> None:
    response = trade_control_status(
        service=_DispatchService(),
        executor=_ExecutorService(),
    )

    assert response.success is True
    assert response.data["trade_control"]["auto_entry_enabled"] is True
    assert response.metadata["operation"] == "trade_control_status"


def test_trade_control_update_endpoint_can_reset_circuit() -> None:
    service = _DispatchService()
    executor = _ExecutorService()

    response = trade_control_update(
        TradeControlRequest(
            auto_entry_enabled=False,
            close_only_mode=True,
            reason="nfp_window",
            reset_circuit=True,
        ),
        service=service,
        executor=executor,
    )

    assert response.success is True
    assert response.data["trade_control"]["auto_entry_enabled"] is False
    assert response.data["trade_control"]["close_only_mode"] is True
    assert executor.reset_calls == 1


def test_trade_reconcile_endpoint_returns_manager_snapshot() -> None:
    response = trade_reconcile(
        TradeReconcileRequest(sync_open_positions=True),
        manager=_PositionManagerService(),
    )

    assert response.success is True
    assert response.data["reconcile"]["recovered"] == 1
    assert response.data["position_manager"]["tracked_positions"] == 1
    assert response.data["tracked_positions"][0]["symbol"] == "XAUUSD"


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
    service = _DispatchService()
    response = trade_from_signal(
        SignalExecuteTradeRequest(signal_id="sig_1"),
        signal_service=_SignalService(),
        service=service,
    )

    assert response.success is True
    assert response.metadata["operation"] == "trade_from_signal"
    assert response.data["ticket"] == 1
    assert service.last_dispatch[1]["request_id"] == "sig_1"
    assert service.last_dispatch[1]["metadata"]["entry_origin"] == "auto"
    assert service.last_dispatch[1]["metadata"]["signal"]["timeframe"] == "M5"


def test_positions_endpoint_serializes_dataclass_time_without_duplicate_keyword() -> None:
    response = positions(symbol="XAUUSD", magic=7, service=_DispatchService())

    assert response.success is True
    assert response.data[0].time == "2026-01-01T00:00:00+00:00"


def test_orders_endpoint_serializes_dataclass_time_without_duplicate_keyword() -> None:
    response = orders(symbol="XAUUSD", magic=7, service=_DispatchService())

    assert response.success is True
    assert response.data[0].time == "2026-01-01T00:00:00+00:00"
