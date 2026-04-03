from __future__ import annotations

from typing import Any, Protocol


class MarginGuardPositionPort(Protocol):
    trailing_atr_multiplier: float

    def set_margin_guard(self, guard: Any) -> None: ...

    def tighten_trailing_stops(self, factor: float) -> int: ...


class MarginGuardTradePort(Protocol):
    def get_positions(self, symbol: str | None = None) -> Any: ...

    def close_position(self, ticket: int, **kwargs: Any) -> Any: ...

    def close_all_positions(self, **kwargs: Any) -> Any: ...


class MarginGuardExecutorPort(Protocol):
    def set_margin_guard(self, guard: Any) -> None: ...
