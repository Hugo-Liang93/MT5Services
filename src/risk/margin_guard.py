"""MarginGuard: margin-level monitoring with tiered alerts and automatic actions.

Designed as a standalone, stateless evaluator that can be called from
PositionManager's reconcile loop.  All thresholds are configurable via
``risk.ini [margin_guard]``.

Architecture:
    - ``MarginGuardConfig``: INI-driven dataclass (no business logic)
    - ``MarginLevel``: enum for tiered alert states
    - ``MarginGuardSnapshot``: immutable evaluation result (pure data)
    - ``MarginGuard``: stateful monitor (cooldown tracking, action dispatch)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ── Config ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class MarginGuardConfig:
    enabled: bool = True
    # Alert thresholds (frontend display)
    warn_level: float = 200.0
    danger_level: float = 150.0
    critical_level: float = 100.0
    # Action thresholds (backend automatic)
    block_new_trades_level: float = 200.0
    tighten_stops_level: float = 150.0
    tighten_stops_factor: float = 0.6
    emergency_close_level: float = 80.0
    emergency_close_strategy: str = "worst_first"
    emergency_close_cooldown: float = 30.0


def load_margin_guard_config(section: dict[str, str]) -> MarginGuardConfig:
    """Parse ``[margin_guard]`` INI section into config."""

    def _float(key: str, default: float) -> float:
        raw = section.get(key, "")
        try:
            return float(raw) if raw else default
        except (TypeError, ValueError):
            return default

    def _bool(key: str, default: bool) -> bool:
        raw = str(section.get(key, "")).strip().lower()
        if raw in ("true", "1", "yes"):
            return True
        if raw in ("false", "0", "no"):
            return False
        return default

    return MarginGuardConfig(
        enabled=_bool("enabled", True),
        warn_level=_float("warn_level", 200.0),
        danger_level=_float("danger_level", 150.0),
        critical_level=_float("critical_level", 100.0),
        block_new_trades_level=_float("block_new_trades_level", 200.0),
        tighten_stops_level=_float("tighten_stops_level", 150.0),
        tighten_stops_factor=_float("tighten_stops_factor", 0.6),
        emergency_close_level=_float("emergency_close_level", 80.0),
        emergency_close_strategy=section.get("emergency_close_strategy", "worst_first").strip(),
        emergency_close_cooldown=_float("emergency_close_cooldown", 30.0),
    )


# ── Alert Level ─────────────────────────────────────────────────


class MarginLevel(str, Enum):
    SAFE = "safe"
    WARN = "warn"
    DANGER = "danger"
    CRITICAL = "critical"


# ── Snapshot (evaluation result) ────────────────────────────────


@dataclass(frozen=True)
class MarginGuardSnapshot:
    margin_level: float  # equity / margin * 100 (inf when no margin)
    state: MarginLevel
    equity: float
    margin: float
    free_margin: float
    should_block_new_trades: bool
    should_tighten_stops: bool
    tighten_factor: float
    should_emergency_close: bool
    emergency_strategy: str
    actions_taken: tuple[str, ...] = ()


# ── Guard (stateful monitor) ───────────────────────────────────


class MarginGuard:
    """Margin-level monitor with tiered alerts and automatic risk actions.

    Called by PositionManager on each reconcile cycle.  Actions are
    dispatched via callbacks injected at construction time.
    """

    def __init__(
        self,
        config: MarginGuardConfig,
        *,
        close_worst_fn: Optional[Callable[[], dict[str, Any]]] = None,
        close_all_fn: Optional[Callable[[], dict[str, Any]]] = None,
        tighten_stops_fn: Optional[Callable[[float], int]] = None,
    ) -> None:
        self.config = config
        self._close_worst_fn = close_worst_fn
        self._close_all_fn = close_all_fn
        self._tighten_stops_fn = tighten_stops_fn
        self._last_emergency_close_at: float = 0.0
        self._last_snapshot: MarginGuardSnapshot | None = None
        self._emergency_close_count: int = 0
        self._tighten_count: int = 0
        self._block_count: int = 0

    @property
    def last_snapshot(self) -> MarginGuardSnapshot | None:
        return self._last_snapshot

    def should_block_new_trades(self) -> bool:
        """Quick check for TradeExecutor: should new trades be blocked?"""
        snap = self._last_snapshot
        if snap is None or not self.config.enabled:
            return False
        return snap.should_block_new_trades

    def evaluate(self, equity: float, margin: float, free_margin: float) -> MarginGuardSnapshot:
        """Compute margin level and determine alert state + required actions.

        Pure computation — does NOT execute actions. Call ``act()`` to dispatch.
        """
        if margin > 0:
            margin_level = (equity / margin) * 100.0
        else:
            margin_level = float("inf")

        cfg = self.config
        # Determine state
        if margin_level < cfg.critical_level:
            state = MarginLevel.CRITICAL
        elif margin_level < cfg.danger_level:
            state = MarginLevel.DANGER
        elif margin_level < cfg.warn_level:
            state = MarginLevel.WARN
        else:
            state = MarginLevel.SAFE

        snap = MarginGuardSnapshot(
            margin_level=round(margin_level, 2),
            state=state,
            equity=round(equity, 2),
            margin=round(margin, 2),
            free_margin=round(free_margin, 2),
            should_block_new_trades=margin_level < cfg.block_new_trades_level,
            should_tighten_stops=margin_level < cfg.tighten_stops_level,
            tighten_factor=cfg.tighten_stops_factor,
            should_emergency_close=margin_level < cfg.emergency_close_level,
            emergency_strategy=cfg.emergency_close_strategy,
        )
        self._last_snapshot = snap
        return snap

    def act(self, snapshot: MarginGuardSnapshot) -> list[str]:
        """Execute actions based on snapshot.  Returns list of action descriptions."""
        if not self.config.enabled:
            return []
        actions: list[str] = []

        # 1. Emergency close (highest priority)
        if snapshot.should_emergency_close:
            now = time.monotonic()
            cooldown = self.config.emergency_close_cooldown
            if now - self._last_emergency_close_at >= cooldown:
                result = self._do_emergency_close(snapshot.emergency_strategy)
                if result:
                    actions.append(result)
                    self._last_emergency_close_at = now

        # 2. Tighten stops
        if snapshot.should_tighten_stops and self._tighten_stops_fn is not None:
            try:
                count = self._tighten_stops_fn(snapshot.tighten_factor)
                if count > 0:
                    self._tighten_count += count
                    actions.append(f"tighten_stops:{count}")
                    logger.warning(
                        "MarginGuard: tightened trailing stops for %d positions (factor=%.2f, margin_level=%.1f%%)",
                        count, snapshot.tighten_factor, snapshot.margin_level,
                    )
            except Exception:
                logger.warning("MarginGuard: tighten_stops_fn failed", exc_info=True)

        # 3. Block new trades (passive — checked by TradeExecutor via should_block_new_trades())
        if snapshot.should_block_new_trades:
            self._block_count += 1
            if self._block_count == 1 or self._block_count % 50 == 0:
                logger.warning(
                    "MarginGuard: blocking new trades (margin_level=%.1f%% < %.0f%%, count=%d)",
                    snapshot.margin_level, self.config.block_new_trades_level, self._block_count,
                )

        # Update snapshot with actions taken
        if actions:
            self._last_snapshot = MarginGuardSnapshot(
                **{
                    **{f.name: getattr(snapshot, f.name) for f in snapshot.__dataclass_fields__.values()},
                    "actions_taken": tuple(actions),
                }
            )
        return actions

    def _do_emergency_close(self, strategy: str) -> str | None:
        if strategy == "all" and self._close_all_fn is not None:
            try:
                result = self._close_all_fn()
                self._emergency_close_count += 1
                logger.critical(
                    "MarginGuard: EMERGENCY close all positions! result=%s",
                    result,
                )
                return f"emergency_close_all:{result}"
            except Exception:
                logger.error("MarginGuard: emergency close_all failed", exc_info=True)
                return None

        if self._close_worst_fn is not None:
            try:
                result = self._close_worst_fn()
                self._emergency_close_count += 1
                logger.critical(
                    "MarginGuard: EMERGENCY close worst position! result=%s",
                    result,
                )
                return f"emergency_close_worst:{result}"
            except Exception:
                logger.error("MarginGuard: emergency close_worst failed", exc_info=True)
                return None

        return None

    def status(self) -> dict[str, Any]:
        snap = self._last_snapshot
        return {
            "enabled": self.config.enabled,
            "margin_level": snap.margin_level if snap else None,
            "state": snap.state.value if snap else "unknown",
            "equity": snap.equity if snap else None,
            "margin": snap.margin if snap else None,
            "free_margin": snap.free_margin if snap else None,
            "should_block_new_trades": snap.should_block_new_trades if snap else False,
            "should_tighten_stops": snap.should_tighten_stops if snap else False,
            "should_emergency_close": snap.should_emergency_close if snap else False,
            "emergency_close_count": self._emergency_close_count,
            "tighten_count": self._tighten_count,
            "block_count": self._block_count,
            "thresholds": {
                "warn": self.config.warn_level,
                "danger": self.config.danger_level,
                "critical": self.config.critical_level,
                "block_trades": self.config.block_new_trades_level,
                "tighten_stops": self.config.tighten_stops_level,
                "emergency_close": self.config.emergency_close_level,
            },
        }
