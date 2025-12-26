"""
账户/持仓/订单查询业务层。
"""

from __future__ import annotations

from typing import List, Optional

from src.clients.mt5_account import MT5AccountClient, AccountInfo, Position, Order


class AccountService:
    def __init__(self, client: Optional[MT5AccountClient] = None):
        self.client = client or MT5AccountClient()

    def account_info(self) -> AccountInfo:
        return self.client.account_info()

    def positions(self, symbol: Optional[str] = None) -> List[Position]:
        return self.client.positions(symbol)

    def orders(self, symbol: Optional[str] = None) -> List[Order]:
        return self.client.orders(symbol)
