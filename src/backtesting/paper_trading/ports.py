"""Paper Trading 交互边界 Protocol 定义。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

from src.signals.models import SignalEvent


class MarketPricePort(Protocol):
    """提供实时报价的端口。"""

    def get_quote(self, symbol: str) -> Any:
        """返回最新 Quote（bid/ask），无报价返回 None。"""
        ...


class SignalListenerPort(Protocol):
    """信号事件监听回调。"""

    def on_signal_event(self, event: SignalEvent) -> None: ...


class PaperTradeWritePort(Protocol):
    """Paper Trading 持久化写入端口。"""

    def write_session(self, session: Dict[str, Any]) -> None: ...

    def write_trades(self, trades: list[tuple]) -> None: ...


class PaperTradingQueryPort(Protocol):
    """Paper Trading 查询端口。"""

    def status(self) -> Dict[str, Any]: ...

    def get_closed_trades(self) -> list[Dict[str, Any]]: ...

    def get_metrics(self) -> Dict[str, Any]: ...

    def get_session(self) -> Optional[Dict[str, Any]]: ...
