from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from src.trading.application import TradingCommandService, TradingQueryService
from src.trading.application.idempotency import TradeOperatorActionReplayConflictError
from src.risk.service import PreTradeRiskBlockedError
from src.trading.application import TradingModule


@dataclass
class DummyAccountInfo:
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    profit: float
    leverage: int
    currency: str


class DummyTradingService:
    class Client:
        @staticmethod
        def health():
            return {"connected": True, "login": 1001, "server": "Broker-Live"}

    client = Client()

    def __init__(self):
        self.execute_calls = []
        self.modify_calls = []

    def health(self):
        return {"connected": True, "login": 1001, "server": "Broker-Live"}

    def execute_trade(self, **kwargs):
        self.execute_calls.append(dict(kwargs))
        return {
            "ticket": 123,
            "symbol": kwargs["symbol"],
            "order_kind": kwargs.get("order_kind", "market"),
            "request_id": kwargs.get("request_id"),
        }

    def precheck_trade(self, **kwargs):
        if kwargs.get("symbol") == "BLOCKED":
            return {"verdict": "block", "symbol": kwargs["symbol"], "checks": [], "warnings": [], "blocked": True, "reason": "blocked"}
        return {"verdict": "allow", "symbol": kwargs["symbol"], "checks": [], "warnings": [], "blocked": False}

    def execute_trade_batch(self, trades, stop_on_error=False):
        return {
            "results": [{"index": idx, "success": True, "result": {"ticket": idx + 1}} for idx, _ in enumerate(trades)],
            "success_count": len(trades),
            "failure_count": 0,
            "stop_on_error": stop_on_error,
        }

    def close_position(self, **kwargs):
        return {"ticket": kwargs["ticket"], "success": True}

    def close_all_positions(self, **kwargs):
        return {"closed": [1], "failed": []}

    def close_positions_by_tickets(self, tickets, deviation=20, comment="close_batch"):
        return {"closed": list(tickets), "failed": []}

    def cancel_orders(self, **kwargs):
        return {"canceled": [11], "failed": []}

    def cancel_orders_by_tickets(self, tickets):
        return {"canceled": list(tickets), "failed": []}

    def estimate_margin(self, **kwargs):
        return 123.4

    def modify_orders(self, **kwargs):
        return {"modified": [7], "failed": []}

    def modify_positions(self, **kwargs):
        self.modify_calls.append(dict(kwargs))
        return {"modified": [8], "failed": []}

    def get_positions(self, symbol=None, magic=None):
        return []

    def get_orders(self, symbol=None, magic=None):
        return []


class DummyAccountService:
    def account_info(self):
        return DummyAccountInfo(1001, 1000.0, 1010.0, 50.0, 960.0, 10.0, 100, "USD")

    def positions(self, symbol=None):
        return []

    def orders(self, symbol=None):
        return []


class DummyRegistry:
    def __init__(self):
        self.aliases = ["live", "demo"]
        self.trading_service = DummyTradingService()
        self.account_service = DummyAccountService()

    def resolve_alias(self, account_alias=None):
        return account_alias or "live"

    def default_account_alias(self):
        return "live"

    @contextmanager
    def operation_scope(self, account_alias=None):
        alias = self.resolve_alias(account_alias)
        yield alias, self.trading_service, self.account_service

    def list_accounts(self):
        return [
            {
                "alias": "live",
                "label": "Live",
                "account_key": "live:broker-live:1001",
                "login": 1001,
                "server": "Broker-Live",
                "environment": "live",
                "timezone": "UTC",
                "enabled": True,
                "default": True,
            },
            {
                "alias": "demo",
                "label": "Demo",
                "account_key": "demo:broker-demo:2002",
                "login": 2002,
                "server": "Broker-Demo",
                "environment": "demo",
                "timezone": "UTC",
                "enabled": True,
                "default": False,
            },
        ]


class DummyDBWriter:
    def __init__(self):
        self.rows = []

    def write_trade_command_audits(self, rows):
        self.rows.extend(list(rows))

    def fetch_trade_command_audits(self, **kwargs):
        if not self.rows:
            return []
        return [
            (
                self.rows[-1][0],
                self.rows[-1][1],
                self.rows[-1][2],
                self.rows[-1][3],
                self.rows[-1][4],
                self.rows[-1][5],
                self.rows[-1][6],
                self.rows[-1][7],
                self.rows[-1][8],
                self.rows[-1][9],
                self.rows[-1][10],
                self.rows[-1][11],
                self.rows[-1][12],
                self.rows[-1][13],
                self.rows[-1][14],
                self.rows[-1][15],
                self.rows[-1][16],
                self.rows[-1][17],
            )
        ]

    def summarize_trade_command_audits(self, **kwargs):
        return [("live", "execute_trade", "success", 1, 12.0, self.rows[-1][0])]


def test_trading_module_records_account_aware_trade_operations():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    result = module.execute_trade(symbol="XAUUSD", volume=0.2, side="buy", order_kind="limit")

    assert result["ticket"] == 123
    assert result["trace_id"]
    assert result["operation_id"]
    assert module.db_writer.rows[-1][2] == "live"
    assert module.db_writer.rows[-1][3] == "execute_trade"
    assert module.db_writer.rows[-1][17] == "live:broker-live:1001"


def test_trading_module_account_info_does_not_write_command_audit():
    db = DummyDBWriter()
    module = TradingModule(registry=DummyRegistry(), db_writer=db)

    info = module.account_info()

    assert info.login == 1001
    assert db.rows == []


def test_trading_module_monitoring_summary_reads_audit_rows():
    db = DummyDBWriter()
    module = TradingModule(registry=DummyRegistry(), db_writer=db)
    module.execute_trade(symbol="XAUUSD", volume=0.1, side="buy")

    summary = module.monitoring_summary(hours=24)

    assert summary["summary"][0]["command_type"] == "execute_trade"
    assert summary["active_account_alias"] == "live"
    assert summary["accounts"][0]["alias"] == "live"
    assert summary["accounts"][0]["active"] is True
    assert summary["recent"][0]["account_alias"] == "live"


def test_trading_module_exposes_only_active_account_profile():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    accounts = module.list_accounts()

    assert len(accounts) == 1
    assert accounts[0]["alias"] == "live"
    assert accounts[0]["active"] is True


def test_trading_module_health_uses_active_account_client():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    health = module.health()

    assert health["account_alias"] == "live"
    assert health["connected"] is True
    assert health["login"] == 1001


def test_trading_module_generates_daily_summary_after_trade():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    module.execute_trade(symbol="XAUUSD", volume=0.1, side="buy")
    summary = module.daily_trade_summary()

    assert summary["total"] == 1
    assert summary["success"] == 1
    assert summary["failed"] == 0
    assert summary["symbols"]["XAUUSD"]["total"] == 1


def test_trading_module_dispatch_operation_routes_to_handler():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    result = module.dispatch_operation("trade", {"symbol": "XAUUSD", "volume": 0.2, "side": "buy"})

    assert result["ticket"] == 123


def test_trading_module_dispatch_trade_filters_execute_only_fields_from_precheck():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    result = module.dispatch_operation(
        "trade",
        {
            "symbol": "XAUUSD",
            "volume": 0.2,
            "side": "buy",
            "dry_run": False,
            "request_id": "req_dispatch",
        },
    )

    assert result["ticket"] == 123


def test_trading_module_replays_same_request_id_from_memory_cache() -> None:
    registry = DummyRegistry()
    module = TradingModule(registry=registry, db_writer=DummyDBWriter())

    first = module.execute_trade(
        symbol="XAUUSD",
        volume=0.2,
        side="buy",
        request_id="req_same_trade",
    )
    second = module.execute_trade(
        symbol="XAUUSD",
        volume=0.2,
        side="buy",
        request_id="req_same_trade",
    )

    assert first["ticket"] == 123
    assert second["ticket"] == 123
    assert second["idempotent_replay"] is True
    assert second["idempotent_source"] == "memory"
    assert len(registry.trading_service.execute_calls) == 1


def test_trading_module_dispatch_operation_rejects_unknown():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    try:
        module.dispatch_operation("unknown_op", {})
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "unsupported trading operation" in str(exc)


def test_trading_module_dispatch_operation_rejects_query_operations() -> None:
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    with pytest.raises(ValueError) as exc_info:
        module.dispatch_operation("daily_summary", {})

    assert "unsupported trading operation" in str(exc_info.value)


def test_trading_module_entry_to_order_status_ready():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    status = module.entry_to_order_status(symbol="XAUUSD", volume=0.1, side="buy")

    assert status["ready_for_order"] is True
    assert status["stages"]["order"] == "ready"


def test_trading_module_exposes_command_and_query_services() -> None:
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    assert isinstance(module.commands, TradingCommandService)
    assert isinstance(module.queries, TradingQueryService)
    assert module.commands.active_account_alias == "live"
    assert module.queries.active_account_alias == "live"


def test_trading_module_dispatch_trade_respects_risk_block():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    try:
        module.dispatch_operation("trade", {"symbol": "BLOCKED", "volume": 0.1, "side": "buy"})
        assert False, "expected risk block"
    except Exception as exc:
        assert "blocked" in str(exc)


def test_trading_module_can_pause_auto_entries_without_blocking_manual_trade() -> None:
    registry = DummyRegistry()
    module = TradingModule(registry=registry, db_writer=DummyDBWriter())
    module.update_trade_control(auto_entry_enabled=False, reason="manual_review")

    with pytest.raises(PreTradeRiskBlockedError) as exc_info:
        module.dispatch_operation(
            "trade",
            {
                "symbol": "XAUUSD",
                "volume": 0.1,
                "side": "buy",
                "metadata": {"entry_origin": "auto"},
            },
        )

    assert exc_info.value.assessment["reason"] == "auto_entry_paused"

    manual = module.execute_trade(symbol="XAUUSD", volume=0.1, side="buy")

    assert manual["ticket"] == 123
    assert len(registry.trading_service.execute_calls) == 1


def test_trading_module_can_apply_persisted_trade_control_state() -> None:
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    state = module.apply_trade_control_state(
        {
            "auto_entry_enabled": False,
            "close_only_mode": True,
            "updated_at": "2026-04-02T09:00:00+00:00",
            "reason": "restored",
        }
    )

    assert state["auto_entry_enabled"] is False
    assert state["close_only_mode"] is True
    assert state["reason"] == "restored"


def test_trading_module_replays_successful_trade_when_request_id_reused() -> None:
    registry = DummyRegistry()
    module = TradingModule(registry=registry, db_writer=DummyDBWriter())

    first = module.execute_trade(
        symbol="XAUUSD",
        volume=0.1,
        side="buy",
        request_id="req-repeat",
    )
    second = module.execute_trade(
        symbol="XAUUSD",
        volume=0.1,
        side="buy",
        request_id="req-repeat",
    )

    assert first["request_id"] == "req-repeat"
    assert second["ticket"] == 123
    assert second["idempotent_replay"] is True
    assert second["idempotent_source"] == "memory"
    assert len(registry.trading_service.execute_calls) == 1


def test_trading_module_replays_operator_action_by_idempotency_key_after_restart() -> None:
    db = DummyDBWriter()
    module = TradingModule(registry=DummyRegistry(), db_writer=db)
    request_payload = {
        "auto_entry_enabled": False,
        "close_only_mode": True,
        "reason": "nfp_window",
        "actor": "operator",
        "idempotency_key": "idem_trade_control_restart",
        "request_context": {"panel": "execution"},
    }
    response_payload = {
        "accepted": True,
        "status": "applied",
        "action_id": "act_trade_control_restart",
        "audit_id": "act_trade_control_restart",
        "actor": "operator",
        "reason": "nfp_window",
        "idempotency_key": "idem_trade_control_restart",
        "request_context": {"panel": "execution"},
        "message": "trade control updated",
        "effective_state": {"trade_control": {"auto_entry_enabled": False}},
    }

    module.record_operator_action(
        command_type="update_trade_control",
        request_payload=request_payload,
        response_payload=response_payload,
        operation_id="act_trade_control_restart",
    )

    restarted = TradingModule(registry=DummyRegistry(), db_writer=db)
    replayed = restarted.find_operator_action_replay(
        command_type="update_trade_control",
        idempotency_key="idem_trade_control_restart",
        request_payload=request_payload,
    )

    assert replayed is not None
    assert replayed["source"] == "audit"
    assert replayed["response_payload"]["action_id"] == "act_trade_control_restart"


def test_trading_module_rejects_conflicting_operator_action_idempotency_reuse() -> None:
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())
    module.record_operator_action(
        command_type="update_runtime_mode",
        request_payload={
            "mode": "risk_off",
            "reason": "after_hours",
            "actor": "operator",
            "idempotency_key": "idem_runtime_mode_conflict",
            "request_context": {"panel": "overview"},
        },
        response_payload={
            "accepted": True,
            "status": "applied",
            "action_id": "act_runtime_mode_conflict",
            "audit_id": "act_runtime_mode_conflict",
            "actor": "operator",
            "reason": "after_hours",
            "idempotency_key": "idem_runtime_mode_conflict",
            "request_context": {"panel": "overview"},
            "message": "runtime mode updated",
            "effective_state": {"runtime_mode": {"current_mode": "risk_off"}},
        },
        operation_id="act_runtime_mode_conflict",
    )

    with pytest.raises(TradeOperatorActionReplayConflictError) as exc_info:
        module.find_operator_action_replay(
            command_type="update_runtime_mode",
            idempotency_key="idem_runtime_mode_conflict",
            request_payload={
                "mode": "observe",
                "reason": "manual",
                "actor": "operator",
                "idempotency_key": "idem_runtime_mode_conflict",
                "request_context": {"panel": "overview"},
            },
        )

    assert "different update_runtime_mode request" in str(exc_info.value)


def test_trading_module_logs_operator_action_record(caplog: pytest.LogCaptureFixture) -> None:
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    with caplog.at_level("INFO"):
        module.record_operator_action(
            command_type="update_trade_control",
            request_payload={
                "actor": "operator",
                "reason": "smoke",
                "idempotency_key": "idem_log_1",
            },
            response_payload={
                "accepted": True,
                "status": "applied",
                "action_id": "act_log_1",
                "audit_id": "act_log_1",
            },
            operation_id="act_log_1",
        )

    assert "Operator action recorded" in caplog.text
    assert "command=update_trade_control" in caplog.text
    assert "action_id=act_log_1" in caplog.text


def test_trading_module_caches_close_position_operator_action_for_memory_replay() -> None:
    module = TradingModule(registry=DummyRegistry(), db_writer=None)

    result = module.close_position(
        ticket=88,
        actor="operator",
        reason="manual_close",
        action_id="act_close_replay",
        audit_id="act_close_replay",
        idempotency_key="idem_close_replay",
        request_context={"panel": "positions"},
    )
    replayed = module.find_operator_action_replay(
        command_type="close_position",
        idempotency_key="idem_close_replay",
        request_payload={
            "ticket": 88,
            "actor": "operator",
            "reason": "manual_close",
            "idempotency_key": "idem_close_replay",
            "request_context": {"panel": "positions"},
        },
    )

    assert result["status"] == "completed"
    assert replayed is not None
    assert replayed["source"] == "memory"
    assert replayed["response_payload"]["action_id"] == "act_close_replay"


def test_trading_module_replays_cancel_orders_operator_action_after_restart() -> None:
    db = DummyDBWriter()
    module = TradingModule(registry=DummyRegistry(), db_writer=db)

    result = module.cancel_orders(
        symbol="XAUUSD",
        magic=7,
        actor="operator",
        reason="manual_cancel_orders",
        action_id="act_cancel_orders_restart",
        audit_id="act_cancel_orders_restart",
        idempotency_key="idem_cancel_orders_restart",
        request_context={"panel": "orders"},
    )
    restarted = TradingModule(registry=DummyRegistry(), db_writer=db)
    replayed = restarted.find_operator_action_replay(
        command_type="cancel_orders",
        idempotency_key="idem_cancel_orders_restart",
        request_payload={
            "symbol": "XAUUSD",
            "magic": 7,
            "actor": "operator",
            "reason": "manual_cancel_orders",
            "idempotency_key": "idem_cancel_orders_restart",
            "request_context": {"panel": "orders"},
        },
    )

    assert result["status"] == "completed"
    assert replayed is not None
    assert replayed["source"] == "audit"
    assert replayed["response_payload"]["effective_state"]["result"]["canceled"] == [11]


def test_trading_module_modify_positions_preserves_ticket_scope() -> None:
    registry = DummyRegistry()
    module = TradingModule(registry=registry, db_writer=DummyDBWriter())

    result = module.modify_positions(ticket=88, symbol="XAUUSD", sl=3010.0)

    assert result["modified"] == [8]
    assert registry.trading_service.modify_calls[-1] == {
        "ticket": 88,
        "symbol": "XAUUSD",
        "sl": 3010.0,
    }
