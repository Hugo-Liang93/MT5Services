from __future__ import annotations

import logging
import json
from collections.abc import Mapping

from src.config import get_merged_config

from .margin_guard import MarginGuard, load_margin_guard_config
from .ports import (
    MarginGuardExecutorPort,
    MarginGuardPositionPort,
    MarginGuardTradePort,
)

logger = logging.getLogger(__name__)


def wire_margin_guard(
    position_manager: MarginGuardPositionPort,
    trade_module: MarginGuardTradePort,
    trade_executor: MarginGuardExecutorPort | None = None,
) -> MarginGuard | None:
    section: dict[str, str] = {}
    risk_config = get_merged_config("risk")
    if not isinstance(risk_config, Mapping):
        raise TypeError("Risk configuration must be a mapping")

    margin_guard_section = risk_config.get("margin_guard")
    if margin_guard_section is None:
        section = {}
    elif isinstance(margin_guard_section, Mapping):
        section = {str(k): str(v) for k, v in margin_guard_section.items()}
    else:
        raise TypeError("Risk configuration [margin_guard] must be a section map")

    config = load_margin_guard_config(section)
    if not config.enabled:
        logger.info(
            "MarginGuard startup snapshot: %s",
            json.dumps(
                {
                    "enabled": False,
                    "reason": "enabled=false",
                    "raw_margin_guard_section_keys": sorted(section.keys()),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        return None

    logger.info(
        "MarginGuard startup snapshot: %s",
        json.dumps(
            {
                "enabled": True,
                "warn_level": config.warn_level,
                "danger_level": config.danger_level,
                "critical_level": config.critical_level,
                "block_new_trades_level": config.block_new_trades_level,
                "tighten_stops_level": config.tighten_stops_level,
                "tighten_stops_factor": config.tighten_stops_factor,
                "emergency_close_level": config.emergency_close_level,
                "emergency_close_strategy": config.emergency_close_strategy,
                "emergency_close_cooldown": config.emergency_close_cooldown,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )

    def close_worst() -> dict:
        positions = list(trade_module.get_positions())
        if not positions:
            return {"closed": None}
        worst = min(positions, key=lambda p: float(p.profit or 0))
        ticket = int(worst.ticket)
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
