"""Basic position manager for signal-initiated trades.

Tracks positions opened by the TradeExecutor and provides
trailing stop and breakeven management via a background reconcile loop
that periodically syncs state with MT5 open positions.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .sizing import TradeParameters

logger = logging.getLogger(__name__)


@dataclass
class TrackedPosition:
    ticket: int
    signal_id: str
    symbol: str
    action: str
    entry_price: float
    stop_loss: float
    take_profit: float
    volume: float
    atr_at_entry: float
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    breakeven_applied: bool = False
    trailing_active: bool = False
    highest_price: Optional[float] = None
    lowest_price: Optional[float] = None


class PositionManager:
    """Track and manage signal-initiated positions.

    Provides trailing stop and breakeven adjustments. A background thread
    periodically fetches open positions from MT5 to update live prices
    and remove positions that have been closed by SL/TP or manually.
    """

    def __init__(
        self,
        trading_module: Any,
        *,
        trailing_atr_multiplier: float = 1.0,
        breakeven_atr_threshold: float = 1.0,
        end_of_day_close_enabled: bool = False,
        end_of_day_close_hour_utc: int = 21,
        end_of_day_close_minute_utc: int = 0,
    ):
        self._trading = trading_module
        self.trailing_atr_multiplier = trailing_atr_multiplier
        self.breakeven_atr_threshold = breakeven_atr_threshold
        self.end_of_day_close_enabled = bool(end_of_day_close_enabled)
        self.end_of_day_close_hour_utc = int(end_of_day_close_hour_utc)
        self.end_of_day_close_minute_utc = int(end_of_day_close_minute_utc)
        self._positions: Dict[int, TrackedPosition] = {}
        self._lock = threading.Lock()
        # O-1: 关仓回调列表，reconcile 检测到仓位关闭时通知下游（如 OutcomeTracker）
        self._close_callbacks: List[Callable[[TrackedPosition, Optional[float]], None]] = []

        self._reconcile_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._reconcile_interval: float = 10.0
        self._reconcile_count: int = 0
        self._last_reconcile_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._last_end_of_day_close_date: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, reconcile_interval: float = 10.0) -> None:
        """Start the background reconcile loop."""
        if self._reconcile_thread is not None and self._reconcile_thread.is_alive():
            return
        self._reconcile_interval = max(1.0, reconcile_interval)
        self._stop_event.clear()
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop,
            name="position-manager-reconcile",
            daemon=True,
        )
        self._reconcile_thread.start()
        logger.info(
            "PositionManager started (reconcile_interval=%.1fs)", self._reconcile_interval
        )

    def stop(self) -> None:
        """Stop the background reconcile loop."""
        self._stop_event.set()
        if self._reconcile_thread is not None:
            self._reconcile_thread.join(timeout=5.0)
            self._reconcile_thread = None
        logger.info("PositionManager stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def track_position(
        self,
        ticket: int,
        signal_id: str,
        symbol: str,
        action: str,
        params: TradeParameters,
    ) -> TrackedPosition:
        pos = TrackedPosition(
            ticket=ticket,
            signal_id=signal_id,
            symbol=symbol,
            action=action,
            entry_price=params.entry_price,
            stop_loss=params.stop_loss,
            take_profit=params.take_profit,
            volume=params.position_size,
            atr_at_entry=params.atr_value,
            highest_price=params.entry_price if action == "buy" else None,
            lowest_price=params.entry_price if action == "sell" else None,
        )
        with self._lock:
            self._positions[ticket] = pos
        logger.info(
            "Tracking position ticket=%d signal=%s %s %s",
            ticket, signal_id, action, symbol,
        )
        return pos

    def update_price(self, ticket: int, current_price: float) -> None:
        with self._lock:
            pos = self._positions.get(ticket)
        if pos is None:
            return

        if pos.action == "buy":
            if pos.highest_price is None or current_price > pos.highest_price:
                pos.highest_price = current_price
        elif pos.action == "sell":
            if pos.lowest_price is None or current_price < pos.lowest_price:
                pos.lowest_price = current_price

        self._check_breakeven(pos, current_price)
        self._check_trailing_stop(pos, current_price)

    def add_close_callback(
        self, fn: Callable[["TrackedPosition", Optional[float]], None]
    ) -> None:
        """O-1: 注册关仓回调，仓位从 MT5 消失时会以 (pos, close_price) 调用。

        ``close_price`` 为 MT5 报告的当前价格（None 表示无法获取）。
        """
        if fn not in self._close_callbacks:
            self._close_callbacks.append(fn)

    def remove_position(self, ticket: int) -> None:
        with self._lock:
            self._positions.pop(ticket, None)

    def active_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            snapshot = list(self._positions.values())
        return [
            {
                "ticket": pos.ticket,
                "signal_id": pos.signal_id,
                "symbol": pos.symbol,
                "action": pos.action,
                "entry_price": pos.entry_price,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "volume": pos.volume,
                "breakeven_applied": pos.breakeven_applied,
                "trailing_active": pos.trailing_active,
                "highest_price": pos.highest_price,
                "lowest_price": pos.lowest_price,
                "opened_at": pos.opened_at.isoformat(),
            }
            for pos in snapshot
        ]

    def status(self) -> Dict[str, Any]:
        with self._lock:
            position_count = len(self._positions)
        return {
            "running": self._reconcile_thread is not None and self._reconcile_thread.is_alive(),
            "reconcile_interval": self._reconcile_interval,
            "reconcile_count": self._reconcile_count,
            "last_reconcile_at": self._last_reconcile_at.isoformat() if self._last_reconcile_at else None,
            "last_error": self._last_error,
            "tracked_positions": position_count,
            "config": {
                "trailing_atr_multiplier": self.trailing_atr_multiplier,
                "breakeven_atr_threshold": self.breakeven_atr_threshold,
                "end_of_day_close_enabled": self.end_of_day_close_enabled,
                "end_of_day_close_hour_utc": self.end_of_day_close_hour_utc,
                "end_of_day_close_minute_utc": self.end_of_day_close_minute_utc,
            },
            "last_end_of_day_close_date": self._last_end_of_day_close_date,
        }

    # ------------------------------------------------------------------
    # Background reconcile loop
    # ------------------------------------------------------------------

    def _reconcile_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_end_of_day_closeout()
                self._reconcile_with_mt5()
                self._reconcile_count += 1
                self._last_reconcile_at = datetime.now(timezone.utc)
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("PositionManager reconcile error: %s", exc)
            self._stop_event.wait(timeout=self._reconcile_interval)

    def _run_end_of_day_closeout(self, now: Optional[datetime] = None) -> Optional[dict]:
        if not self.end_of_day_close_enabled:
            return None
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)

        closeout_time = current.replace(
            hour=self.end_of_day_close_hour_utc,
            minute=self.end_of_day_close_minute_utc,
            second=0,
            microsecond=0,
        )
        if current < closeout_time:
            return None

        day_key = current.date().isoformat()
        if self._last_end_of_day_close_date == day_key:
            return None

        positions_getter = getattr(self._trading, "get_positions", None)
        if callable(positions_getter):
            try:
                open_positions = list(positions_getter())
            except Exception:
                open_positions = []
        else:
            open_positions = []
        if not open_positions:
            self._last_end_of_day_close_date = day_key
            return {"closed": [], "failed": []}

        close_all = getattr(self._trading, "close_all_positions", None)
        if not callable(close_all):
            return None

        result = close_all(comment="end_of_day_closeout")
        if not isinstance(result, dict):
            result = {"result": result}
        failed = list(result.get("failed", []) or [])
        if not failed:
            self._last_end_of_day_close_date = day_key
        logger.info(
            "PositionManager: end-of-day closeout executed at %s (closed=%s, failed=%s)",
            current.isoformat(),
            len(result.get("closed", []) or []),
            len(failed),
        )
        return result

    def _reconcile_with_mt5(self) -> None:
        """Sync tracked positions with MT5 open positions.

        For each tracked ticket:
        - If still open in MT5: update price, run trailing/breakeven checks.
        - If no longer in MT5: remove from tracking (SL/TP or manual close).
        """
        with self._lock:
            tracked_tickets = dict(self._positions)

        if not tracked_tickets:
            return

        # Gather unique symbols to minimise MT5 API calls
        symbols: set[str] = {pos.symbol for pos in tracked_tickets.values()}
        mt5_positions: Dict[int, Any] = {}
        for symbol in symbols:
            try:
                open_positions = self._trading.get_positions(symbol=symbol)
                for p in (open_positions or []):
                    ticket = getattr(p, "ticket", None)
                    if ticket is not None:
                        mt5_positions[int(ticket)] = p
            except Exception as exc:
                logger.debug("PositionManager: get_positions(%s) error: %s", symbol, exc)

        for ticket, pos in tracked_tickets.items():
            mt5_pos = mt5_positions.get(ticket)
            if mt5_pos is None:
                # Position closed in MT5 — remove from tracking
                close_price = getattr(mt5_pos, "price_current", None) if mt5_pos else None
                if close_price is not None:
                    try:
                        close_price = float(close_price)
                    except (TypeError, ValueError):
                        close_price = None
                logger.info(
                    "PositionManager: ticket=%d (%s %s) no longer open in MT5, removing",
                    ticket, pos.action, pos.symbol,
                )
                self.remove_position(ticket)
                # O-1: 通知关仓回调（在锁外调用，避免死锁）
                for cb in list(self._close_callbacks):
                    try:
                        cb(pos, close_price)
                    except Exception as cb_exc:
                        logger.warning(
                            "PositionManager: close callback error for ticket=%d: %s",
                            ticket, cb_exc,
                        )
                continue

            current_price = getattr(mt5_pos, "price_current", None)
            if current_price is not None:
                try:
                    self.update_price(ticket, float(current_price))
                except Exception as exc:
                    logger.debug(
                        "PositionManager: update_price ticket=%d error: %s", ticket, exc
                    )

    # ------------------------------------------------------------------
    # Private SL management helpers
    # ------------------------------------------------------------------

    def _check_breakeven(self, pos: TrackedPosition, current_price: float) -> None:
        if pos.breakeven_applied:
            return

        threshold = pos.atr_at_entry * self.breakeven_atr_threshold
        if pos.action == "buy" and current_price >= pos.entry_price + threshold:
            new_sl = pos.entry_price + 0.01
            self._modify_sl(pos, new_sl)
            pos.breakeven_applied = True
            logger.info("Breakeven applied ticket=%d sl=%.2f", pos.ticket, new_sl)
        elif pos.action == "sell" and current_price <= pos.entry_price - threshold:
            new_sl = pos.entry_price - 0.01
            self._modify_sl(pos, new_sl)
            pos.breakeven_applied = True
            logger.info("Breakeven applied ticket=%d sl=%.2f", pos.ticket, new_sl)

    def _check_trailing_stop(self, pos: TrackedPosition, current_price: float) -> None:
        if not pos.breakeven_applied:
            return

        trail_distance = pos.atr_at_entry * self.trailing_atr_multiplier

        if pos.action == "buy" and pos.highest_price is not None:
            new_sl = pos.highest_price - trail_distance
            if new_sl > pos.stop_loss:
                self._modify_sl(pos, new_sl)
                pos.trailing_active = True
        elif pos.action == "sell" and pos.lowest_price is not None:
            new_sl = pos.lowest_price + trail_distance
            if new_sl < pos.stop_loss:
                self._modify_sl(pos, new_sl)
                pos.trailing_active = True

    def _modify_sl(self, pos: TrackedPosition, new_sl: float) -> None:
        try:
            self._trading.modify_positions(
                ticket=pos.ticket,
                sl=round(new_sl, 2),
            )
            pos.stop_loss = round(new_sl, 2)
        except Exception as exc:
            logger.warning("Failed to modify SL for ticket=%d: %s", pos.ticket, exc)
