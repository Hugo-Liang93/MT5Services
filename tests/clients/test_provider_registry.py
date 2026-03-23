"""Provider Registry 和 Protocol 合规性测试。"""

from __future__ import annotations

from datetime import date
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from src.clients.economic_calendar import (
    EconomicCalendarEvent,
    EconomicCalendarProvider,
    FredCalendarClient,
    TradingEconomicsCalendarClient,
)
from src.clients.economic_calendar_registry import ProviderRegistry


# ────────────────────────── Protocol 合规性 ──────────────────────────


class _MockProvider:
    """满足 EconomicCalendarProvider Protocol 的最小实现。"""

    def __init__(self, name: str, configured: bool = True, release_watch: bool = True):
        self._name = name
        self._configured = configured
        self._release_watch = release_watch

    @property
    def name(self) -> str:
        return self._name

    def fetch_events(
        self,
        start_date: date,
        end_date: date,
        countries: Optional[List[str]] = None,
    ) -> List[EconomicCalendarEvent]:
        return []

    def supports_release_watch(self) -> bool:
        return self._release_watch

    def is_configured(self) -> bool:
        return self._configured


@pytest.mark.unit
class TestProviderProtocol:
    def test_mock_provider_satisfies_protocol(self):
        provider = _MockProvider("test")
        assert isinstance(provider, EconomicCalendarProvider)

    def test_te_client_satisfies_protocol(self):
        settings = MagicMock()
        settings.request_retries = 1
        settings.request_timeout_seconds = 5.0
        settings.retry_backoff_seconds = 0.1
        settings.refresh_jitter_seconds = 0.0
        settings.tradingeconomics_api_key = "test_key"
        client = TradingEconomicsCalendarClient(settings)
        assert isinstance(client, EconomicCalendarProvider)
        assert client.name == "tradingeconomics"
        assert client.supports_release_watch() is True
        assert client.is_configured() is True

    def test_te_client_not_configured_without_key(self):
        settings = MagicMock()
        settings.tradingeconomics_api_key = None
        client = TradingEconomicsCalendarClient(settings)
        assert client.is_configured() is False

    def test_fred_client_satisfies_protocol(self):
        settings = MagicMock()
        settings.request_retries = 1
        settings.request_timeout_seconds = 5.0
        settings.retry_backoff_seconds = 0.1
        settings.refresh_jitter_seconds = 0.0
        settings.fred_api_key = "test_key"
        client = FredCalendarClient(settings)
        assert isinstance(client, EconomicCalendarProvider)
        assert client.name == "fred"
        assert client.supports_release_watch() is False
        assert client.is_configured() is True


# ────────────────────────── Registry 测试 ──────────────────────────


@pytest.mark.unit
class TestProviderRegistry:
    def test_empty_registry(self):
        registry = ProviderRegistry()
        assert len(registry) == 0
        assert registry.all_names() == []
        assert registry.configured_names() == []
        assert registry.release_watch_names() == []

    def test_register_and_get(self):
        registry = ProviderRegistry()
        provider = _MockProvider("test_src")
        registry.register(provider)
        assert len(registry) == 1
        assert "test_src" in registry
        assert registry.get("test_src") is provider
        assert registry.get("nonexistent") is None

    def test_all_names(self):
        registry = ProviderRegistry()
        registry.register(_MockProvider("alpha"))
        registry.register(_MockProvider("beta"))
        assert sorted(registry.all_names()) == ["alpha", "beta"]

    def test_configured_names_filters_unconfigured(self):
        registry = ProviderRegistry()
        registry.register(_MockProvider("configured", configured=True))
        registry.register(_MockProvider("unconfigured", configured=False))
        assert registry.configured_names() == ["configured"]

    def test_release_watch_names(self):
        registry = ProviderRegistry()
        registry.register(
            _MockProvider("rw_yes", configured=True, release_watch=True)
        )
        registry.register(
            _MockProvider("rw_no", configured=True, release_watch=False)
        )
        registry.register(
            _MockProvider("unconfigured", configured=False, release_watch=True)
        )
        assert registry.release_watch_names() == ["rw_yes"]

    def test_register_overwrites(self):
        registry = ProviderRegistry()
        provider1 = _MockProvider("same_name", configured=True)
        provider2 = _MockProvider("same_name", configured=False)
        registry.register(provider1)
        registry.register(provider2)
        assert len(registry) == 1
        assert registry.get("same_name") is provider2

    def test_contains(self):
        registry = ProviderRegistry()
        registry.register(_MockProvider("exists"))
        assert "exists" in registry
        assert "not_exists" not in registry
