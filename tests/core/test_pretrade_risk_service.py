from __future__ import annotations

import pytest

from src.config import EconomicConfig
from src.core.pretrade_risk_service import PreTradeRiskBlockedError, PreTradeRiskService


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
    )

    result = service.assess_trade(symbol="XAUUSD")

    assert result["action"] == "warn"
    assert result["blocked"] is False
    assert result["calendar_health_degraded"] is True
    assert result["warnings"]


def test_blocks_when_calendar_health_mode_is_fail_closed():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(stale=True, provider_failures=3),
        settings=_settings(trade_guard_mode="warn_only", trade_guard_calendar_health_mode="fail_closed"),
    )

    result = service.assess_trade(symbol="XAUUSD")

    assert result["action"] == "block"
    assert result["blocked"] is True
    assert result["calendar_health_mode"] == "fail_closed"


def test_enforces_block_for_active_event_window():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(blocked=True),
        settings=_settings(trade_guard_mode="block"),
    )

    with pytest.raises(PreTradeRiskBlockedError):
        service.enforce_trade_allowed(symbol="XAUUSD")


def test_allows_healthy_calendar_without_database_driver():
    service = PreTradeRiskService(
        economic_calendar_service=DummyCalendar(),
        settings=_settings(),
    )

    result = service.assess_trade(symbol="XAUUSD")

    assert result["action"] == "allow"
    assert result["blocked"] is False
