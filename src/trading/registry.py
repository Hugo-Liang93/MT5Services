from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Dict, Optional

from src.clients.mt5_account import MT5AccountClient
from src.clients.mt5_trading import MT5TradingClient
from src.config import MT5Settings, get_economic_config, get_risk_config, load_mt5_accounts
from src.core.account_service import AccountService
from src.risk.service import PreTradeRiskService
from src.trading.trading_service import TradingService


class TradingAccountRegistry:
    def __init__(self, accounts: Optional[Dict[str, MT5Settings]] = None, economic_calendar_service=None):
        self._accounts = accounts or load_mt5_accounts()
        self._economic_calendar_service = economic_calendar_service
        self._lock = threading.RLock()
        self._account_services: Dict[str, AccountService] = {}
        self._trading_services: Dict[str, TradingService] = {}

    def list_accounts(self) -> list[dict]:
        default_alias = self.default_account_alias()
        return [
            {
                "alias": alias,
                "label": settings.account_label or alias,
                "login": settings.mt5_login,
                "server": settings.mt5_server,
                "timezone": settings.timezone,
                "enabled": settings.enabled,
                "default": alias == default_alias,
            }
            for alias, settings in self._accounts.items()
        ]

    def default_account_alias(self) -> str:
        return next(iter(self._accounts.keys()), "default")

    def resolve_alias(self, account_alias: Optional[str] = None) -> str:
        alias = account_alias or self.default_account_alias()
        if alias not in self._accounts:
            raise KeyError(f"MT5 account alias not configured: {alias}")
        return alias

    def get_settings(self, account_alias: Optional[str] = None) -> MT5Settings:
        return self._accounts[self.resolve_alias(account_alias)]

    def get_account_service(self, account_alias: Optional[str] = None) -> AccountService:
        alias = self.resolve_alias(account_alias)
        with self._lock:
            service = self._account_services.get(alias)
            if service is None:
                service = AccountService(client=MT5AccountClient(self.get_settings(alias)))
                self._account_services[alias] = service
            return service

    def get_trading_service(self, account_alias: Optional[str] = None) -> TradingService:
        alias = self.resolve_alias(account_alias)
        with self._lock:
            service = self._trading_services.get(alias)
            if service is None:
                account_service = self.get_account_service(alias)
                service = TradingService(
                    client=MT5TradingClient(self.get_settings(alias)),
                    account_client=account_service.client,
                    pre_trade_risk_service=PreTradeRiskService(
                        economic_calendar_service=self._economic_calendar_service,
                        account_service=account_service,
                        settings=get_economic_config(),
                        risk_settings=get_risk_config(),
                    ),
                )
                self._trading_services[alias] = service
            return service

    @contextmanager
    def operation_scope(self, account_alias: Optional[str] = None):
        alias = self.resolve_alias(account_alias)
        with self._lock:
            yield alias, self.get_trading_service(alias), self.get_account_service(alias)
