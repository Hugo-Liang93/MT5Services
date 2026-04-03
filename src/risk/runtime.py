from __future__ import annotations

from configparser import ConfigParser

from .margin_guard import MarginGuard, load_margin_guard_config
from .ports import (
    MarginGuardExecutorPort,
    MarginGuardPositionPort,
    MarginGuardTradePort,
)


def wire_margin_guard(
    position_manager: MarginGuardPositionPort,
    trade_module: MarginGuardTradePort,
    trade_executor: MarginGuardExecutorPort | None = None,
) -> MarginGuard | None:
    parser = ConfigParser()
    parser.read("config/risk.ini", encoding="utf-8")
    section = (
        dict(parser["margin_guard"])
        if parser.has_section("margin_guard")
        else {}
    )
    config = load_margin_guard_config(section)
    if not config.enabled:
        return None

    def close_worst() -> dict:
        positions = list(trade_module.get_positions())
        if not positions:
            return {"closed": None}
        worst = min(positions, key=lambda p: float(getattr(p, "profit", 0) or 0))
        ticket = int(getattr(worst, "ticket", 0))
        if ticket <= 0:
            return {"error": "invalid ticket"}
        result = trade_module.close_position(ticket, comment="margin_guard_emergency")
        return {"ticket": ticket, "result": result}

    def close_all() -> dict:
        return trade_module.close_all_positions(comment="margin_guard_emergency_all")

    def tighten_stops(factor: float) -> int:
        return position_manager.tighten_trailing_stops(factor)

    guard = MarginGuard(
        config,
        close_worst_fn=close_worst,
        close_all_fn=close_all,
        tighten_stops_fn=tighten_stops,
    )
    position_manager.set_margin_guard(guard)
    if trade_executor is not None:
        trade_executor.set_margin_guard(guard)
    return guard
