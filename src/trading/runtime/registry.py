from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Optional

from src.clients.mt5_account import MT5AccountClient
from src.clients.mt5_trading import MT5TradingClient
from src.config import (
    MT5Settings,
    build_account_key,
    get_economic_config,
    get_risk_config,
    load_mt5_settings,
    resolve_current_environment,
)
from src.risk.service import PreTradeRiskService
from src.trading.application.trading_service import TradingService


class TradingAccountRegistry:
    def __init__(
        self,
        settings: Optional[MT5Settings] = None,
        economic_calendar_service=None,
    ):
        self._settings = settings or load_mt5_settings()
        self._economic_calendar_service = economic_calendar_service
        self._lock = threading.RLock()
        self._account_client: MT5AccountClient | None = None
        self._trading_service: TradingService | None = None

    @property
    def account_alias(self) -> str:
        return self._settings.account_alias

    def list_accounts(self) -> list[dict]:
        environment = resolve_current_environment(instance_name=self._settings.instance_name)
        if environment is None:
            raise ValueError("runtime environment is not configured for trading account registry")
        return [
            {
                "alias": self._settings.account_alias,
                "label": self._settings.account_label or self._settings.account_alias,
                "account_key": build_account_key(
                    environment,
                    self._settings.mt5_server,
                    self._settings.mt5_login,
                ),
                "login": self._settings.mt5_login,
                "server": self._settings.mt5_server,
                "environment": environment,
                "timezone": self._settings.timezone,
                "enabled": self._settings.enabled,
                "default": True,
            }
        ]

    def resolve_alias(self, account_alias: Optional[str] = None) -> str:
        alias = str(account_alias or self._settings.account_alias).strip()
        if alias != self._settings.account_alias:
            raise KeyError(f"MT5 account alias not configured for this instance: {alias}")
        return alias

    def get_settings(self, account_alias: Optional[str] = None) -> MT5Settings:
        self.resolve_alias(account_alias)
        return self._settings

    def get_account_service(self, account_alias: Optional[str] = None) -> MT5AccountClient:
        self.resolve_alias(account_alias)
        with self._lock:
            if self._account_client is None:
                self._account_client = MT5AccountClient(self._settings)
            return self._account_client

    def get_trading_service(self, account_alias: Optional[str] = None) -> TradingService:
        self.resolve_alias(account_alias)
        with self._lock:
            if self._trading_service is None:
                account_client = self.get_account_service(self._settings.account_alias)
                self._trading_service = TradingService(
                    client=MT5TradingClient(self._settings),
                    account_client=account_client,
                    pre_trade_risk_service=PreTradeRiskService(
                        economic_calendar_service=self._economic_calendar_service,
                        account_service=account_client,
                        settings=get_economic_config(),
                        risk_settings=get_risk_config(),
                    ),
                )
            return self._trading_service

    @contextmanager
    def operation_scope(self, account_alias: Optional[str] = None):
        alias = self.resolve_alias(account_alias)
        with self._lock:
            yield alias, self.get_trading_service(alias), self.get_account_service(alias)
