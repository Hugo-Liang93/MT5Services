from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from src.trading.service import TradingModule


@dataclass
class DummyAccountInfo:
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    leverage: int
    currency: str


class DummyTradingService:
    class Client:
        @staticmethod
        def health():
            return {"connected": True, "login": 1001, "server": "Broker-Live"}

    client = Client()

    def execute_trade(self, **kwargs):
        return {"ticket": 123, "symbol": kwargs["symbol"], "order_kind": kwargs.get("order_kind", "market")}

    def precheck_trade(self, **kwargs):
        if kwargs.get("symbol") == "BLOCKED":
            return {"action": "block", "symbol": kwargs["symbol"], "checks": [], "warnings": [], "blocked": True, "reason": "blocked"}
        return {"action": "allow", "symbol": kwargs["symbol"], "checks": [], "warnings": [], "blocked": False}

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
        return {"modified": [8], "failed": []}

    def get_positions(self, symbol=None, magic=None):
        return []

    def get_orders(self, symbol=None, magic=None):
        return []


class DummyAccountService:
    def account_info(self):
        return DummyAccountInfo(1001, 1000.0, 1010.0, 50.0, 960.0, 100, "USD")

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
            {"alias": "live", "label": "Live", "login": 1001, "server": "Broker-Live", "timezone": "UTC", "enabled": True, "default": True},
            {"alias": "demo", "label": "Demo", "login": 2002, "server": "Broker-Demo", "timezone": "UTC", "enabled": True, "default": False},
        ]


class DummyDBWriter:
    def __init__(self):
        self.rows = []

    def write_trade_operations(self, rows):
        self.rows.extend(list(rows))

    def fetch_trade_operations(self, **kwargs):
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
            )
        ]

    def summarize_trade_operations(self, **kwargs):
        return [("live", "execute_trade", "success", 1, 12.0, self.rows[-1][0])]


def test_trading_module_records_account_aware_trade_operations():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    result = module.execute_trade(symbol="XAUUSD", volume=0.2, side="buy", order_kind="limit")

    assert result["ticket"] == 123
    assert result["trace_id"]
    assert result["operation_id"]
    assert module.db_writer.rows[-1][2] == "live"
    assert module.db_writer.rows[-1][3] == "execute_trade"


def test_trading_module_records_account_info_as_json_safe_payload():
    db = DummyDBWriter()
    module = TradingModule(registry=DummyRegistry(), db_writer=db)

    info = module.account_info()

    assert info.login == 1001
    payload = db.rows[-1][16]
    assert payload["result"]["login"] == 1001


def test_trading_module_monitoring_summary_reads_audit_rows():
    db = DummyDBWriter()
    module = TradingModule(registry=DummyRegistry(), db_writer=db)
    module.execute_trade(symbol="XAUUSD", volume=0.1, side="buy")

    summary = module.monitoring_summary(hours=24)

    assert summary["summary"][0]["operation_type"] == "execute_trade"
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


def test_trading_module_dispatch_operation_rejects_unknown():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    try:
        module.dispatch_operation("unknown_op", {})
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "unsupported trading operation" in str(exc)


def test_trading_module_entry_to_order_status_ready():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    status = module.entry_to_order_status(symbol="XAUUSD", volume=0.1, side="buy")

    assert status["ready_for_order"] is True
    assert status["stages"]["order"] == "ready"


def test_trading_module_dispatch_trade_respects_risk_block():
    module = TradingModule(registry=DummyRegistry(), db_writer=DummyDBWriter())

    try:
        module.dispatch_operation("trade", {"symbol": "BLOCKED", "volume": 0.1, "side": "buy"})
        assert False, "expected risk block"
    except Exception as exc:
        assert "blocked" in str(exc)
