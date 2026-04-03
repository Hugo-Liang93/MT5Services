from __future__ import annotations

from datetime import datetime, timezone

from src.api.trade import (
    orders,
    positions,
    trade,
    trade_closeout_exposure,
    trade_state_closeout_summary,
    trade_active_pending_state_list,
    trade_pending_execution_context_list,
    trade_runtime_mode_status,
    trade_runtime_mode_update,
    trade_state_alerts_summary,
    trade_control_status,
    trade_control_update,
    trade_daily_summary,
    trade_dispatch,
    trade_from_signal,
    trade_pending_lifecycle_state_list,
    trade_position_state_list,
    trade_precheck,
    trade_reconcile,
    trade_state_summary,
)
from src.api.schemas import (
    ExposureCloseoutRequest,
    SignalExecuteTradeRequest,
    RuntimeModeRequest,
    TradeControlRequest,
    TradeDispatchRequest,
    TradeReconcileRequest,
    TradeRequest,
)
from src.clients.mt5_account import Order, Position
from src.clients.base import MT5TradeError
from src.readmodels.runtime import RuntimeReadModel
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
            raise PreTradeRiskBlockedError("blocked by risk", assessment={"verdict": "block"})
        if operation == "blocked_daily_loss_trade":
            raise PreTradeRiskBlockedError(
                "blocked by risk",
                assessment={"verdict": "block", "checks": [{"name": "daily_loss_limit"}]},
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
                assessment={"verdict": "block", "checks": [{"name": "daily_loss_limit"}]},
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
                price_current=3005.0,
                sl=2990.0,
                tp=3020.0,
                profit=5.0,
                swap=0.0,
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
                "direction": "buy",
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


class _RuntimeModeController:
    def __init__(self) -> None:
        self.mode = "full"
        self.last_reason = None

    def snapshot(self):
        return {
            "current_mode": self.mode,
            "configured_mode": "full",
            "after_eod_action": "ingest_only",
            "components": {"trade_execution": self.mode == "full"},
        }

    def apply_mode(self, mode: str, *, reason: str):
        self.mode = mode
        self.last_reason = reason
        return self.snapshot()


class _ExposureCloseoutController:
    def __init__(self) -> None:
        self.last_reason = None
        self.last_comment = None
        self._status = {
            "status": "idle",
            "last_reason": None,
            "last_comment": None,
            "last_requested_at": None,
            "last_completed_at": None,
            "result": None,
        }

    def execute(self, *, reason: str, comment: str):
        self.last_reason = reason
        self.last_comment = comment
        self._status = {
            "status": "completed",
            "last_reason": reason,
            "last_comment": comment,
            "last_requested_at": "2026-01-01T00:00:00+00:00",
            "last_completed_at": "2026-01-01T00:00:00+00:00",
            "result": {
                "completed": True,
                "positions": {"requested": [1], "completed": [1], "failed": [], "error": None},
                "orders": {"requested": [2], "completed": [2], "failed": [], "error": None},
                "remaining_positions": [],
                "remaining_orders": [],
            },
        }
        return dict(self._status)

    def status(self):
        return dict(self._status)


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
    executor = _ExecutorService()
    response = trade_control_status(
        service=_DispatchService(),
        runtime_views=RuntimeReadModel(trade_executor=executor),
    )

    assert response.success is True
    assert response.data["trade_control"]["auto_entry_enabled"] is True
    assert response.data["persisted_trade_control"] is None
    assert response.data["executor"]["status"] == "disabled"
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
        runtime_views=RuntimeReadModel(trade_executor=executor),
    )

    assert response.success is True
    assert response.data["trade_control"]["auto_entry_enabled"] is False
    assert response.data["trade_control"]["close_only_mode"] is True
    assert executor.reset_calls == 1


def test_trade_reconcile_endpoint_returns_manager_snapshot() -> None:
    manager = _PositionManagerService()
    response = trade_reconcile(
        TradeReconcileRequest(sync_open_positions=True),
        manager=manager,
        runtime_views=RuntimeReadModel(position_manager=manager),
    )

    assert response.success is True
    assert response.data["reconcile"]["recovered"] == 1
    assert response.data["position_manager"]["tracked_positions"] == 1
    assert response.data["tracked_positions"]["items"][0]["symbol"] == "XAUUSD"
    assert "trading_state" in response.data


class _TradingStateStore:
    def load_trade_control_state(self):
        return {"auto_entry_enabled": False, "close_only_mode": True}

    def list_pending_order_states(self, *, statuses=None, limit=100):
        rows = [
            {"order_ticket": 101, "status": "placed", "symbol": "XAUUSD"},
            {"order_ticket": 102, "status": "orphan", "symbol": "XAUUSD"},
            {"order_ticket": 103, "status": "filled", "symbol": "XAUUSD"},
        ]
        if statuses:
            rows = [row for row in rows if row["status"] in set(statuses)]
        return rows[:limit]

    def list_position_runtime_states(self, *, statuses=None, limit=100):
        rows = [
            {"position_ticket": 201, "status": "open", "symbol": "XAUUSD"},
            {"position_ticket": 202, "status": "closed", "symbol": "XAUUSD"},
        ]
        if statuses:
            rows = [row for row in rows if row["status"] in set(statuses)]
        return rows[:limit]


class _TradingStateAlerts:
    def summary(self):
        return {
            "status": "warning",
            "alerts": [{"code": "pending_orphan", "severity": "warning"}],
            "summary": [{"code": "pending_orphan", "status": "failed"}],
            "observed": {"active_pending_count": 2},
        }


class _PendingEntryManager:
    def active_execution_contexts(self):
        return [
            {
                "signal_id": "sig_1",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "sma_trend",
                "direction": "buy",
                "source": "pending_entry",
            },
            {
                "signal_id": "sig_2",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "sma_trend",
                "direction": "buy",
                "source": "mt5_order",
            },
        ]


def test_trade_state_summary_endpoint_returns_persisted_state() -> None:
    response = trade_state_summary(
        runtime_views=RuntimeReadModel(
            trading_state_store=_TradingStateStore(),
            trading_state_alerts=_TradingStateAlerts(),
            exposure_closeout_controller=_ExposureCloseoutController(),
            runtime_mode_controller=_RuntimeModeController(),
        ),
    )

    assert response.success is True
    assert response.data["trade_control"]["close_only_mode"] is True
    assert response.data["runtime_mode"]["current_mode"] == "full"
    assert response.data["closeout"]["status"] == "idle"
    assert response.data["pending"]["active"]["status_counts"]["placed"] == 1
    assert response.data["pending"]["lifecycle"]["status_counts"]["filled"] == 1
    assert response.data["positions"]["status_counts"]["open"] == 1
    assert response.data["alerts"]["status"] == "warning"


def test_trade_state_alerts_summary_endpoint_returns_alert_projection() -> None:
    response = trade_state_alerts_summary(
        runtime_views=RuntimeReadModel(trading_state_alerts=_TradingStateAlerts()),
    )

    assert response.success is True
    assert response.data["status"] == "warning"


def test_trade_state_closeout_summary_endpoint_returns_projection() -> None:
    controller = _ExposureCloseoutController()
    controller.execute(reason="manual_risk_off", comment="manual_exposure_closeout")
    response = trade_state_closeout_summary(
        runtime_views=RuntimeReadModel(exposure_closeout_controller=controller),
    )

    assert response.success is True
    assert response.data["status"] == "completed"
    assert response.data["result"]["orders"]["completed"] == [2]


def test_trade_runtime_mode_status_endpoint_returns_projection() -> None:
    response = trade_runtime_mode_status(
        runtime_views=RuntimeReadModel(
            runtime_mode_controller=_RuntimeModeController(),
        )
    )

    assert response.success is True
    assert response.data["current_mode"] == "full"


def test_trade_runtime_mode_update_endpoint_applies_mode() -> None:
    controller = _RuntimeModeController()
    runtime_views = RuntimeReadModel(
        runtime_mode_controller=controller,
        trading_state_alerts=_TradingStateAlerts(),
    )

    response = trade_runtime_mode_update(
        RuntimeModeRequest(mode="risk_off", reason="after_hours"),
        controller=controller,
        runtime_views=runtime_views,
    )

    assert response.success is True
    assert response.data["runtime_mode"]["current_mode"] == "risk_off"
    assert controller.last_reason == "after_hours"
    assert response.data["trading_state"]["alerts"]["alerts"][0]["code"] == "pending_orphan"
    assert response.metadata["operation"] == "trade_runtime_mode_update"


def test_trade_closeout_exposure_endpoint_executes_unified_controller() -> None:
    controller = _ExposureCloseoutController()
    runtime_views = RuntimeReadModel(
        trading_state_store=_TradingStateStore(),
        trading_state_alerts=_TradingStateAlerts(),
        exposure_closeout_controller=controller,
        runtime_mode_controller=_RuntimeModeController(),
    )

    response = trade_closeout_exposure(
        ExposureCloseoutRequest(
            reason="manual_risk_off",
            comment="manual_exposure_closeout",
        ),
        controller=controller,
        runtime_views=runtime_views,
    )

    assert response.success is True
    assert response.data["closeout"]["status"] == "completed"
    assert response.data["closeout"]["result"]["positions"]["completed"] == [1]
    assert response.data["trading_state"]["closeout"]["last_reason"] == "manual_risk_off"
    assert response.metadata["operation"] == "trade_closeout_exposure"


def test_trade_pending_lifecycle_state_list_endpoint_filters_status() -> None:
    response = trade_pending_lifecycle_state_list(
        status="orphan",
        limit=20,
        runtime_views=RuntimeReadModel(trading_state_store=_TradingStateStore()),
    )

    assert response.success is True
    assert response.data["count"] == 1
    assert response.data["items"][0]["status"] == "orphan"
    assert response.metadata["operation"] == "trade_pending_lifecycle_state_list"


def test_trade_active_pending_state_list_endpoint_returns_active_projection() -> None:
    response = trade_active_pending_state_list(
        limit=20,
        runtime_views=RuntimeReadModel(trading_state_store=_TradingStateStore()),
    )

    assert response.success is True
    assert response.data["view"] == "active"
    assert response.data["count"] == 2
    assert response.metadata["operation"] == "trade_active_pending_state_list"


def test_trade_pending_execution_context_list_endpoint_returns_runtime_projection() -> None:
    response = trade_pending_execution_context_list(
        runtime_views=RuntimeReadModel(pending_entry_manager=_PendingEntryManager()),
    )

    assert response.success is True
    assert response.data["view"] == "execution_contexts"
    assert response.data["source_counts"]["pending_entry"] == 1
    assert response.data["source_counts"]["mt5_order"] == 1
    assert response.metadata["operation"] == "trade_pending_execution_context_list"


def test_trade_position_state_list_endpoint_filters_status() -> None:
    response = trade_position_state_list(
        status="open",
        limit=20,
        runtime_views=RuntimeReadModel(trading_state_store=_TradingStateStore()),
    )

    assert response.success is True
    assert response.data["count"] == 1
    assert response.data["items"][0]["status"] == "open"
    assert response.metadata["operation"] == "trade_position_state_list"


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
        command_service=service,
        query_service=service,
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
