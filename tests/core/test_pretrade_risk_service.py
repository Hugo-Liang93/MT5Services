from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest

from src.config import EconomicConfig
from src.config.centralized import RiskConfig
from src.risk.service import PreTradeRiskBlockedError, PreTradeRiskService


class DummyCalendar:
    def __init__(
        self,
        *,
        blocked: bool = False,
        stale: bool = False,
        provider_failures: int = 0,
    ) -> None:
        self.blocked = blocked
        self.stale = stale
        self.provider_failures = provider_failures

    def is_stale(self) -> bool:
        return self.stale

    def stats(self):
        return {
            "last_refresh_at": None,
            "refresh_in_progress": "false",
            "provider_status": {
                "tradingeconomics": {
                    "enabled": True,
                    "consecutive_failures": self.provider_failures,
                    "last_error": "timeout" if self.provider_failures else None,
                },
                "fred": {
                    "enabled": True,
                    "consecutive_failures": 0,
                    "last_error": None,
                },
            },
        }

    def get_trade_guard(self, **kwargs):
        return {
            "symbol": kwargs["symbol"],
            "evaluation_time": "2026-03-16T00:00:00+00:00",
            "blocked": self.blocked,
            "currencies": ["USD"],
            "countries": ["United States"],
            "active_windows": [{"window_start": "2026-03-16T00:00:00+00:00", "window_end": "2026-03-16T00:30:00+00:00"}] if self.blocked else [],
            "upcoming_windows": [],
            "importance_min": kwargs.get("importance_min") or 3,
        }


class DummyPosition:
    def __init__(self, symbol: str, volume: float, type: int = 0) -> None:
        self.symbol = symbol
        self.volume = volume
        self.type = type


class DummyOrder:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


@dataclass
class FakeAccountInfo:
    balance: float
    equity: float
    margin: float = 0.0
    margin_free: float | None = 0.0
    profit: float = 0.0
    leverage: int = 100
    currency: str = "USD"
    day_start_balance: float | None = None
    daily_realized_pnl: float | None = None
    daily_pnl: float | None = None


class DummyAccountService:
    def __init__(self, positions=None, orders=None, account_info=None) -> None:
        self._positions = positions or []
        self._orders = orders or []
        if account_info is None:
            self._account_info = FakeAccountInfo(10000.0, 10000.0)
        elif isinstance(account_info, dict):
            self._account_info = FakeAccountInfo(
                balance=account_info.get("balance", 10000.0),
                equity=account_info.get("equity", 10000.0),
                margin_free=account_info.get("margin_free"),
                day_start_balance=account_info.get("day_start_balance"),
                daily_realized_pnl=account_info.get("daily_realized_pnl"),
                daily_pnl=account_info.get("daily_pnl"),
                profit=account_info.get("profit", 0.0),
                leverage=account_info.get("leverage", 100),
                currency=account_info.get("currency", "USD"),
            )
        else:
            self._account_info = account_info

    def account_info(self):
        return self._account_info

    def positions(self, symbol=None):
        if symbol is None:
            return list(self._positions)
        return [position for position in self._positions if position.symbol == symbol]

    def orders(self, symbol=None):
        if symbol is None:
            return list(self._orders)
        return [order for order in self._orders if order.symbol == symbol]


def _settings(**overrides) -> EconomicConfig:
    payload = {
        "enabled": True,
        "trade_guard_enabled": True,
        "trade_guard_mode": "warn_only",
        "trade_guard_calendar_health_mode": "warn_only",
        "trade_guard_provider_failure_threshold": 3,
    }
    payload.update(overrides)
    return EconomicConfig(**payload)


def test_warns_when_calendar_health_is_degraded():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(stale=True, provider_failures=3),
        settings=_settings(trade_guard_mode="warn_only", trade_guard_calendar_health_mode="warn_only"),
        risk_settings=RiskConfig(market_order_protection="off"),
    )

    result = service.assess_trade(symbol="XAUUSD")

    assert result["verdict"] == "warn"
    assert result["blocked"] is False
    assert result["calendar_health_degraded"] is True
    assert result["warnings"]


def test_blocks_when_calendar_health_mode_is_fail_closed():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(stale=True, provider_failures=3),
        settings=_settings(trade_guard_mode="warn_only", trade_guard_calendar_health_mode="fail_closed"),
        risk_settings=RiskConfig(),
    )

    result = service.assess_trade(symbol="XAUUSD")

    assert result["verdict"] == "block"
    assert result["blocked"] is True
    assert result["calendar_health_mode"] == "fail_closed"


def test_enforces_block_for_active_event_window():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(blocked=True),
        settings=_settings(trade_guard_mode="block"),
        risk_settings=RiskConfig(),
    )

    with pytest.raises(PreTradeRiskBlockedError):
        service.enforce_trade_allowed(symbol="XAUUSD")


def test_allows_healthy_calendar_without_database_driver():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(),
        settings=_settings(),
        risk_settings=RiskConfig(market_order_protection="off"),
    )

    result = service.assess_trade(symbol="XAUUSD")

    assert result["verdict"] == "allow"
    assert result["blocked"] is False


def test_blocks_when_position_limit_is_reached():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(),
        account_service=DummyAccountService(
            positions=[DummyPosition("XAUUSD", 0.5), DummyPosition("XAUUSD", 0.3)],
        ),
        settings=_settings(),
        risk_settings=RiskConfig(max_positions_per_symbol=2),
    )

    result = service.assess_trade(symbol="XAUUSD", volume=0.1, side="buy")

    assert result["verdict"] == "block"
    assert result["blocked"] is True
    assert any(check["name"] == "max_positions_per_symbol" for check in result["checks"])


def test_blocks_when_symbol_volume_limit_would_be_exceeded():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(),
        account_service=DummyAccountService(
            positions=[DummyPosition("XAUUSD", 1.2)],
        ),
        settings=_settings(),
        risk_settings=RiskConfig(max_volume_per_symbol=1.5),
    )

    result = service.assess_trade(symbol="XAUUSD", volume=0.4, side="buy")

    assert result["verdict"] == "block"
    assert result["blocked"] is True
    assert any(check["name"] == "max_volume_per_symbol" for check in result["checks"])


def test_blocks_when_same_direction_net_lots_limit_would_be_exceeded():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(),
        account_service=DummyAccountService(
            positions=[
                DummyPosition("XAUUSD", 0.20, type=0),
                DummyPosition("XAUUSD", 0.08, type=0),
                DummyPosition("XAUUSD", 0.10, type=1),
            ],
        ),
        settings=_settings(),
        risk_settings=RiskConfig(max_net_lots_per_symbol=0.30),
    )

    result = service.assess_trade(symbol="XAUUSD", volume=0.05, side="buy")

    assert result["verdict"] == "block"
    assert result["blocked"] is True
    assert any(check["name"] == "max_net_lots_per_symbol" for check in result["checks"])


def test_blocks_when_protection_is_required_but_missing():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(),
        account_service=DummyAccountService(),
        settings=_settings(),
        risk_settings=RiskConfig(market_order_protection="sl_or_tp"),
    )

    result = service.assess_trade(symbol="XAUUSD", volume=0.2, side="buy")

    assert result["verdict"] == "block"
    assert result["blocked"] is True
    assert any(check["name"] == "market_order_protection" for check in result["checks"])


def test_blocks_when_trade_is_outside_allowed_sessions():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(),
        account_service=DummyAccountService(),
        settings=_settings(),
        risk_settings=RiskConfig(allowed_sessions="london,new_york"),
    )

    result = service.assess_trade(
        symbol="XAUUSD",
        volume=0.2,
        side="buy",
        at_time=datetime.fromisoformat("2026-03-19T23:00:00+00:00"),
    )

    assert result["verdict"] == "block"
    assert result["blocked"] is True
    assert any(check["name"] == "allowed_sessions" for check in result["checks"])


def test_blocks_buy_against_bearish_sweep_confirmation():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(),
        account_service=DummyAccountService(),
        settings=_settings(),
        risk_settings=RiskConfig(),
    )

    result = service.assess_trade(
        symbol="XAUUSD",
        volume=0.2,
        side="buy",
        metadata={
            "market_structure": {
                "current_session": "new_york",
                "sweep_confirmation_state": "bearish_sweep_confirmed_previous_day_high",
                "confirmation_reference": "previous_day_high",
                "structure_bias": "bearish_sweep_confirmed",
            }
        },
    )

    assert result["verdict"] == "block"
    assert result["blocked"] is True
    assert any(check["name"] == "market_structure" for check in result["checks"])


def test_warns_when_buying_against_new_york_open_downside_expansion():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(),
        account_service=DummyAccountService(),
        settings=_settings(),
        risk_settings=RiskConfig(market_order_protection="off"),
    )

    result = service.assess_trade(
        symbol="XAUUSD",
        volume=0.2,
        side="buy",
        at_time=datetime.fromisoformat("2026-03-19T14:30:00+00:00"),
        metadata={
            "market_structure": {
                "current_session": "new_york",
                "breakout_state": "below_new_york_open_low",
                "structure_bias": "bearish_breakout",
                "sweep_confirmation_state": "none",
                "first_pullback_state": "none",
                "reclaim_state": "none",
                "new_york_open_low": 3010.0,
            }
        },
    )

    assert result["verdict"] == "warn"
    assert result["blocked"] is False
    assert "buy_against_new_york_open_downside_expansion" in result["warnings"]


def test_blocks_when_daily_loss_limit_is_reached() -> None:
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(),
        account_service=DummyAccountService(
            account_info={"balance": 10000.0, "equity": 9600.0}
        ),
        settings=_settings(),
        risk_settings=RiskConfig(daily_loss_limit_pct=3.0),
    )

    result = service.assess_trade(symbol="XAUUSD", volume=0.2, side="buy")

    assert result["verdict"] == "block"
    assert result["blocked"] is True
    check = next(item for item in result["checks"] if item["name"] == "daily_loss_limit")
    assert check["reason"] == "daily_loss_limit_reached"
    assert check["details"]["source"] == "equity_balance_drawdown_proxy"
