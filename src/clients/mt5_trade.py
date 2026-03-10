"""
Backward-compatible trade error definitions.
"""

from __future__ import annotations

from src.clients.base import MT5BaseError


class MT5TradeError(MT5BaseError):
    """Unified trade/account API error base class."""

