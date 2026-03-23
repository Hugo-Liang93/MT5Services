"""Basic position manager for signal-initiated trades.

Tracks positions opened by the TradeExecutor and provides
trailing stop and breakeven management via a background reconcile loop
that periodically syncs state with MT5 open positions.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .position_rules import check_breakeven, check_trailing_stop, should_close_end_of_day
from .sizing import TradeParameters

logger = logging.getLogger(__name__)

_RESTORABLE_COMMENT_PREFIXES = ("auto:", "agent:")


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
    timeframe: str = ""
    strategy: str = ""
    confidence: Optional[float] = None
    regime: Optional[str] = None
    source: str = "signal_executor"
    comment: str = ""
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    breakeven_applied: bool = False
    trailing_active: bool = False
    highest_price: Optional[float] = None
    lowest_price: Optional[float] = None


class PositionManager:
    """Track and manage signal-initiated positions."""

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
        self._close_callbacks: List[Callable[[TrackedPosition, Optional[float]], None]] = []
        self._position_context_resolver: Optional[
            Callable[[int, Optional[str]], Optional[Dict[str, Any]]]
        ] = None
        self._recovered_position_callback: Optional[
            Callable[[TrackedPosition], None]
        ] = None

        self._reconcile_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._reconcile_interval: float = 10.0
        self._reconcile_count: int = 0
        self._last_reconcile_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._last_end_of_day_close_date: Optional[str] = None

    def start(self, reconcile_interval: float = 10.0) -> None:
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
            "PositionManager started (reconcile_interval=%.1fs)",
            self._reconcile_interval,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._reconcile_thread is not None:
            self._reconcile_thread.join(timeout=5.0)
            self._reconcile_thread = None
        logger.info("PositionManager stopped")

    def track_position(
        self,
        ticket: int,
        signal_id: str,
        symbol: str,
        action: str,
        params: TradeParameters,
        *,
        timeframe: str = "",
        strategy: str = "",
        confidence: Optional[float] = None,
        regime: Optional[str] = None,
        source: str = "signal_executor",
        comment: str = "",
        opened_at: Optional[datetime] = None,
        fill_price: Optional[float] = None,
    ) -> TrackedPosition:
        resolved_entry_price = (
            float(fill_price) if fill_price is not None else float(params.entry_price)
        )
        pos = TrackedPosition(
            ticket=ticket,
            signal_id=signal_id,
            symbol=symbol,
            action=action,
            entry_price=resolved_entry_price,
            stop_loss=params.stop_loss,
            take_profit=params.take_profit,
            volume=params.position_size,
            atr_at_entry=params.atr_value,
            timeframe=timeframe,
            strategy=strategy,
            confidence=confidence,
            regime=regime,
            source=source,
            comment=str(comment or ""),
            opened_at=opened_at or datetime.now(timezone.utc),
            highest_price=resolved_entry_price if action == "buy" else None,
            lowest_price=resolved_entry_price if action == "sell" else None,
        )
        with self._lock:
            self._positions[ticket] = pos
        logger.info(
            "Tracking position ticket=%d signal=%s %s %s",
            ticket,
            signal_id,
            action,
            symbol,
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

        # breakeven/trailing 调用 MT5 API（可能耗时数秒），不能在锁内执行。
        # 锁外前先检查 pos 是否仍在 _positions 中（另一线程可能已 remove）。
        with self._lock:
            if ticket not in self._positions:
                return
        self._check_breakeven(pos, current_price)
        self._check_trailing_stop(pos, current_price)

    def add_close_callback(
        self,
        fn: Callable[[TrackedPosition, Optional[float]], None],
    ) -> None:
        if fn not in self._close_callbacks:
            self._close_callbacks.append(fn)

    def set_recovery_hooks(
        self,
        *,
        position_context_resolver: Optional[
            Callable[[int, Optional[str]], Optional[Dict[str, Any]]]
        ] = None,
        recovered_position_callback: Optional[Callable[[TrackedPosition], None]] = None,
    ) -> None:
        self._position_context_resolver = position_context_resolver
        self._recovered_position_callback = recovered_position_callback

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
                "timeframe": pos.timeframe,
                "strategy": pos.strategy,
                "confidence": pos.confidence,
                "regime": pos.regime,
                "source": pos.source,
                "comment": pos.comment,
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
            "last_reconcile_at": self._last_reconcile_at.isoformat()
            if self._last_reconcile_at
            else None,
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

    @staticmethod
    def _default_atr_from_position(price_open: float, stop_loss: float) -> float:
        try:
            if stop_loss:
                return abs(float(price_open) - float(stop_loss))
        except (TypeError, ValueError):
            return 0.0
        return 0.0

    @staticmethod
    def _action_from_position_type(position_type: Any) -> str:
        try:
            return "sell" if int(position_type) == 1 else "buy"
        except (TypeError, ValueError):
            return "buy"

    @staticmethod
    def _is_restorable_comment(comment: str) -> bool:
        normalized = str(comment or "").strip().lower()
        return any(normalized.startswith(prefix) for prefix in _RESTORABLE_COMMENT_PREFIXES)

    def sync_open_positions(self) -> dict[str, Any]:
        positions_getter = getattr(self._trading, "get_positions", None)
        if not callable(positions_getter):
            return {"synced": 0, "recovered": 0, "skipped": 0}

        try:
            open_positions = list(positions_getter())
        except Exception as exc:
            self._last_error = f"sync_open_positions: {exc}"
            logger.warning("PositionManager sync_open_positions error: %s", exc)
            return {"synced": 0, "recovered": 0, "skipped": 0, "error": str(exc)}

        synced = 0
        recovered = 0
        skipped = 0
        for raw_pos in open_positions:
            ticket = int(getattr(raw_pos, "ticket", 0) or 0)
            if ticket <= 0:
                skipped += 1
                continue
            with self._lock:
                if ticket in self._positions:
                    skipped += 1
                    continue

            comment = str(getattr(raw_pos, "comment", "") or "")
            context = None
            if self._position_context_resolver is not None:
                try:
                    context = self._position_context_resolver(ticket, comment)
                except Exception:
                    logger.debug(
                        "Position context resolver failed for ticket=%s",
                        ticket,
                        exc_info=True,
                    )
                    context = None
            context = dict(context or {})
            if not context.get("signal_id") and not self._is_restorable_comment(comment):
                skipped += 1
                continue

            action = str(
                context.get("action")
                or self._action_from_position_type(getattr(raw_pos, "type", None))
            )
            entry_price = float(getattr(raw_pos, "price_open", 0.0) or 0.0)
            stop_loss = float(getattr(raw_pos, "sl", 0.0) or 0.0)
            take_profit = float(getattr(raw_pos, "tp", 0.0) or 0.0)
            volume = float(getattr(raw_pos, "volume", 0.0) or 0.0)
            opened_at = getattr(raw_pos, "time", None)
            if not isinstance(opened_at, datetime):
                opened_at = datetime.now(timezone.utc)

            pos = TrackedPosition(
                ticket=ticket,
                signal_id=str(context.get("signal_id") or f"restored:{ticket}"),
                symbol=str(getattr(raw_pos, "symbol", "") or ""),
                action=action,
                entry_price=float(context.get("fill_price") or entry_price),
                stop_loss=stop_loss,
                take_profit=take_profit,
                volume=volume,
                atr_at_entry=self._default_atr_from_position(entry_price, stop_loss),
                timeframe=str(context.get("timeframe") or ""),
                strategy=str(context.get("strategy") or ""),
                confidence=context.get("confidence"),
                regime=context.get("regime"),
                source=str(context.get("source") or "mt5_bootstrap"),
                comment=str(context.get("comment") or comment),
                opened_at=opened_at,
                highest_price=entry_price if action == "buy" else None,
                lowest_price=entry_price if action == "sell" else None,
            )
            with self._lock:
                self._positions[ticket] = pos
            synced += 1
            if self._recovered_position_callback is not None and context.get("signal_id"):
                try:
                    self._recovered_position_callback(pos)
                    recovered += 1
                except Exception:
                    logger.warning(
                        "Recovered position callback failed for ticket=%s",
                        ticket,
                        exc_info=True,
                    )
        return {"synced": synced, "recovered": recovered, "skipped": skipped}

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

        eod_result = should_close_end_of_day(
            current_time=current,
            close_hour_utc=self.end_of_day_close_hour_utc,
            close_minute_utc=self.end_of_day_close_minute_utc,
            last_close_date=self._last_end_of_day_close_date,
        )
        if not eod_result.should_close:
            return None

        day_key = current.date().isoformat() if current.tzinfo is None else current.astimezone(timezone.utc).date().isoformat()

        # 先标记 EOD 日期，防止并发 reconcile 在 close_all 期间触发二次 EOD
        self._last_end_of_day_close_date = day_key

        positions_getter = getattr(self._trading, "get_positions", None)
        if callable(positions_getter):
            try:
                open_positions = list(positions_getter())
            except Exception:
                open_positions = []
        else:
            open_positions = []
        if not open_positions:
            return {"closed": [], "failed": []}

        close_all = getattr(self._trading, "close_all_positions", None)
        if not callable(close_all):
            return None

        try:
            result = close_all(comment="end_of_day_closeout")
        except Exception as exc:
            # close_all 异常时回退日期标记，允许下次 reconcile 循环重试
            self._last_end_of_day_close_date = None
            logger.error("EOD closeout failed, will retry next cycle: %s", exc)
            return {"closed": [], "failed": [], "error": str(exc)}
        if not isinstance(result, dict):
            result = {"result": result}
        failed = list(result.get("failed", []) or [])
        logger.info(
            "PositionManager: end-of-day closeout executed at %s (closed=%s, failed=%s)",
            current.isoformat(),
            len(result.get("closed", []) or []),
            len(failed),
        )
        return result

    def _reconcile_with_mt5(self) -> None:
        with self._lock:
            tracked_tickets = dict(self._positions)

        if not tracked_tickets:
            return

        symbols: set[str] = {pos.symbol for pos in tracked_tickets.values()}
        mt5_positions: Dict[int, Any] = {}
        for symbol in symbols:
            try:
                open_positions = self._trading.get_positions(symbol=symbol)
                for raw_pos in (open_positions or []):
                    ticket = getattr(raw_pos, "ticket", None)
                    if ticket is not None:
                        mt5_positions[int(ticket)] = raw_pos
            except Exception as exc:
                logger.debug("PositionManager: get_positions(%s) error: %s", symbol, exc)

        for ticket, pos in tracked_tickets.items():
            mt5_pos = mt5_positions.get(ticket)
            if mt5_pos is None:
                close_price = None
                close_source = "mt5_missing"
                close_details_getter = getattr(self._trading, "get_position_close_details", None)
                if callable(close_details_getter):
                    try:
                        close_details = close_details_getter(ticket=ticket, symbol=pos.symbol)
                    except Exception as exc:
                        logger.debug(
                            "PositionManager: get_position_close_details(%s) error: %s",
                            ticket,
                            exc,
                        )
                        close_details = None
                    if isinstance(close_details, dict):
                        raw_close_price = close_details.get("close_price")
                        if raw_close_price is not None:
                            try:
                                close_price = float(raw_close_price)
                                close_source = "history_deals"
                            except (TypeError, ValueError):
                                close_price = None
                logger.info(
                    "PositionManager: ticket=%d (%s %s) no longer open in MT5, "
                    "removing (close_source=%s)",
                    ticket, pos.action, pos.symbol, close_source,
                )
                self.remove_position(ticket)
                # 附加 close_source 供下游回调使用
                pos._close_source = close_source  # type: ignore[attr-defined]
                for cb in list(self._close_callbacks):
                    try:
                        cb(pos, close_price)
                    except Exception as cb_exc:
                        logger.warning(
                            "PositionManager: close callback error for ticket=%d: %s",
                            ticket,
                            cb_exc,
                        )
                continue

            current_price = getattr(mt5_pos, "price_current", None)
            if current_price is not None:
                try:
                    self.update_price(ticket, float(current_price))
                except Exception as exc:
                    logger.debug(
                        "PositionManager: update_price ticket=%d error: %s",
                        ticket,
                        exc,
                    )

    def _check_breakeven(self, pos: TrackedPosition, current_price: float) -> None:
        result = check_breakeven(
            action=pos.action,
            entry_price=pos.entry_price,
            current_price=current_price,
            atr_at_entry=pos.atr_at_entry,
            breakeven_atr_threshold=self.breakeven_atr_threshold,
            already_applied=pos.breakeven_applied,
        )
        if result.should_apply and result.new_stop_loss is not None:
            if self._modify_sl(pos, result.new_stop_loss):
                pos.breakeven_applied = True
                logger.info("Breakeven applied ticket=%d sl=%.2f", pos.ticket, result.new_stop_loss)
            else:
                logger.warning("Breakeven SL modify failed ticket=%d target_sl=%.2f", pos.ticket, result.new_stop_loss)

    def _check_trailing_stop(self, pos: TrackedPosition, current_price: float) -> None:
        result = check_trailing_stop(
            action=pos.action,
            current_stop_loss=pos.stop_loss,
            atr_at_entry=pos.atr_at_entry,
            trailing_atr_multiplier=self.trailing_atr_multiplier,
            breakeven_applied=pos.breakeven_applied,
            highest_price=pos.highest_price,
            lowest_price=pos.lowest_price,
        )
        if result.should_update and result.new_stop_loss is not None:
            if self._modify_sl(pos, result.new_stop_loss):
                pos.trailing_active = True
            else:
                logger.warning("Trailing SL modify failed ticket=%d target_sl=%.2f", pos.ticket, result.new_stop_loss)

    def _modify_sl(self, pos: TrackedPosition, new_sl: float) -> bool:
        try:
            target_sl = round(new_sl, 2)
            result = self._trading.modify_positions(ticket=pos.ticket, symbol=pos.symbol, sl=target_sl)
            if isinstance(result, dict):
                modified = {int(item) for item in result.get("modified", []) if item is not None}
                if modified and pos.ticket not in modified:
                    raise RuntimeError(f"ticket {pos.ticket} not modified")
                failed = list(result.get("failed", []) or [])
                if failed and pos.ticket not in modified:
                    raise RuntimeError(str(failed[0]))
            pos.stop_loss = target_sl
            return True
        except Exception as exc:
            logger.warning("Failed to modify SL for ticket=%d: %s", pos.ticket, exc)
            return False
