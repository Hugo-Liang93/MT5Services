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

from .position_rules import (
    IndicatorExitConfig,
    check_breakeven,
    check_indicator_exit,
    check_trailing_stop,
    check_trailing_take_profit,
    should_close_end_of_day,
)
from .sizing import TradeParameters

logger = logging.getLogger(__name__)

# Comment prefixes that identify positions opened by this system.
# "auto:" is the legacy format; timeframe prefixes (e.g. "m5:", "h1:") are the
# current format since the comment was changed to "{tf}:{strategy}:{direction}".
_RESTORABLE_COMMENT_PREFIXES = (
    "auto:", "agent:",
    "m1:", "m5:", "m15:", "m30:", "h1:", "h4:", "d1:", "w1:", "mn1:",
)


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
    entry_indicators: Dict[str, Dict] = field(default_factory=dict)
    breakeven_applied: bool = False
    trailing_active: bool = False
    highest_price: Optional[float] = None
    lowest_price: Optional[float] = None
    current_price: Optional[float] = None
    close_source: Optional[str] = None


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
        # Trailing Take Profit（盈利后主动收缩 TP）
        trailing_tp_enabled: bool = False,
        trailing_tp_activation_atr: float = 1.5,
        trailing_tp_trail_atr: float = 0.8,
        # 指标驱动出场（持仓期间检测指标反转，收紧 SL）
        indicator_exit_config: IndicatorExitConfig = IndicatorExitConfig(),
        # 指标快照回调（接收 symbol+timeframe 返回 indicators Dict）
        indicator_snapshot_fn: Optional[Callable[[str, str], Dict[str, Dict]]] = None,
    ):
        self._trading = trading_module
        self.trailing_atr_multiplier = trailing_atr_multiplier
        self.breakeven_atr_threshold = breakeven_atr_threshold
        self.end_of_day_close_enabled = bool(end_of_day_close_enabled)
        self.end_of_day_close_hour_utc = int(end_of_day_close_hour_utc)
        self.end_of_day_close_minute_utc = int(end_of_day_close_minute_utc)
        self._trailing_tp_enabled = trailing_tp_enabled
        self._trailing_tp_activation_atr = trailing_tp_activation_atr
        self._trailing_tp_trail_atr = trailing_tp_trail_atr
        self._indicator_exit_config = indicator_exit_config
        self._indicator_snapshot_fn = indicator_snapshot_fn
        self._positions: Dict[int, TrackedPosition] = {}
        self._lock = threading.Lock()
        self._close_callbacks: List[Callable[[TrackedPosition, Optional[float]], None]] = []
        self._position_context_resolver: Optional[
            Callable[[int, Optional[str]], Optional[Dict[str, Any]]]
        ] = None
        self._position_state_resolver: Optional[
            Callable[[int], Optional[Dict[str, Any]]]
        ] = None
        self._recovered_position_callback: Optional[
            Callable[[TrackedPosition], None]
        ] = None
        self._on_position_tracked: Optional[Callable[[TrackedPosition, str], None]] = None
        self._on_position_updated: Optional[Callable[[TrackedPosition, str], None]] = None
        self._on_position_closed: Optional[Callable[[TrackedPosition, Optional[float]], None]] = None

        self._reconcile_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._reconcile_interval: float = 10.0
        self._reconcile_count: int = 0
        self._last_reconcile_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._last_end_of_day_close_date: Optional[str] = None
        self._margin_guard: Any = None  # Optional[MarginGuard], injected via set_margin_guard()

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
        breakeven_applied: bool = False,
        trailing_active: bool = False,
        highest_price: Optional[float] = None,
        lowest_price: Optional[float] = None,
        current_price: Optional[float] = None,
        entry_indicators: Optional[Dict[str, Dict]] = None,
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
            entry_indicators=dict(entry_indicators or {}),
            breakeven_applied=bool(breakeven_applied),
            trailing_active=bool(trailing_active),
            highest_price=(
                highest_price
                if highest_price is not None
                else (resolved_entry_price if action == "buy" else None)
            ),
            lowest_price=(
                lowest_price
                if lowest_price is not None
                else (resolved_entry_price if action == "sell" else None)
            ),
            current_price=current_price,
        )
        with self._lock:
            self._positions[ticket] = pos
        if self._on_position_tracked is not None:
            self._on_position_tracked(pos, "tracked")
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
            # 更新价格跟踪
            pos.current_price = current_price
            if pos.action == "buy":
                if pos.highest_price is None or current_price > pos.highest_price:
                    pos.highest_price = current_price
            elif pos.action == "sell":
                if pos.lowest_price is None or current_price < pos.lowest_price:
                    pos.lowest_price = current_price
            # 快照用于锁外 MT5 API 调用
            need_breakeven = not pos.breakeven_applied
            need_trailing = pos.breakeven_applied

        # breakeven/trailing/trailing_tp/indicator_exit 调用 MT5 API（可能耗时），在锁外执行。
        if need_breakeven:
            self._check_breakeven(pos, current_price)
        if need_trailing:
            self._check_trailing_stop(pos, current_price)
        if self._trailing_tp_enabled and pos.atr_at_entry > 0:
            self._check_trailing_tp(pos)
        if self._indicator_exit_config.enabled and pos.atr_at_entry > 0:
            self._check_indicator_exit(pos, current_price)

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
        position_state_resolver: Optional[
            Callable[[int], Optional[Dict[str, Any]]]
        ] = None,
        recovered_position_callback: Optional[Callable[[TrackedPosition], None]] = None,
    ) -> None:
        self._position_context_resolver = position_context_resolver
        self._position_state_resolver = position_state_resolver
        self._recovered_position_callback = recovered_position_callback

    def set_state_hooks(
        self,
        *,
        on_position_tracked: Optional[Callable[[TrackedPosition, str], None]] = None,
        on_position_updated: Optional[Callable[[TrackedPosition, str], None]] = None,
        on_position_closed: Optional[Callable[[TrackedPosition, Optional[float]], None]] = None,
    ) -> None:
        self._on_position_tracked = on_position_tracked
        self._on_position_updated = on_position_updated
        self._on_position_closed = on_position_closed

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
                "current_price": pos.current_price,
                "unrealized_pnl": (
                    round((pos.current_price - pos.entry_price) * (1 if pos.action == "buy" else -1), 2)
                    if pos.current_price is not None
                    else None
                ),
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
            "margin_guard": self.margin_guard_status(),
        }

    def is_after_eod_today(self) -> bool:
        """当天 EOD 已执行，当前处于 EOD 后禁止开仓的窗口。

        用于 TradeExecutor 在开仓前检查，防止 EOD 平仓后又开新仓。
        次日首个交易时段（亚盘 UTC 0:00）开始后自动解除。
        """
        if not self.end_of_day_close_enabled:
            return False
        if self._last_end_of_day_close_date is None:
            return False
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        if self._last_end_of_day_close_date != today:
            return False
        # EOD 已执行，且当前时间仍在 EOD 之后（同一天）
        return True

    def force_close_overnight(self) -> Optional[Dict[str, Any]]:
        """启动时检测并强制平仓过夜仓位。

        如果 EOD 因服务宕机而被跳过，次日启动时立即全平。
        只在 end_of_day_close_enabled=True 时生效。
        """
        if not self.end_of_day_close_enabled:
            return None

        positions_getter = getattr(self._trading, "get_positions", None)
        if not callable(positions_getter):
            return None
        try:
            open_positions = list(positions_getter())
        except Exception:
            return None
        if not open_positions:
            return None

        # 检查是否有仓位是"昨天或更早"开的
        now = datetime.now(timezone.utc)
        overnight: list = []
        for pos in open_positions:
            opened = getattr(pos, "time", None)
            if opened is None:
                overnight.append(pos)
                continue
            if not isinstance(opened, datetime):
                overnight.append(pos)
                continue
            opened_utc = opened if opened.tzinfo else opened.replace(tzinfo=timezone.utc)
            if opened_utc.date() < now.date():
                overnight.append(pos)

        if not overnight:
            return None

        logger.warning(
            "PositionManager: detected %d overnight positions, force closing",
            len(overnight),
        )
        close_all = getattr(self._trading, "close_all_positions", None)
        if not callable(close_all):
            return None
        try:
            result = close_all(comment="overnight_force_close")
            logger.info("Overnight force close result: %s", result)
            return result if isinstance(result, dict) else {"result": result}
        except Exception as exc:
            logger.error("Overnight force close failed: %s", exc)
            return {"error": str(exc)}

    def margin_guard_status(self) -> Dict[str, Any] | None:
        if self._margin_guard is None:
            return None
        return self._margin_guard.status()

    @staticmethod
    def _default_atr_from_position(price_open: float, stop_loss: float) -> float:
        """从入场价和止损价反推 ATR 近似值。

        SL 距离 = sl_atr_mult × ATR，所以 ATR ≈ SL距离 / sl_atr_mult。
        使用 2.0 作为默认 SL 倍数（覆盖 M30/H1 的配置值）。
        """
        try:
            if stop_loss:
                sl_distance = abs(float(price_open) - float(stop_loss))
                return sl_distance / 2.0  # 反推 ATR
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
            persisted_state = None
            if self._position_state_resolver is not None:
                try:
                    persisted_state = dict(self._position_state_resolver(ticket) or {})
                except Exception:
                    logger.debug(
                        "Position state resolver failed for ticket=%s",
                        ticket,
                        exc_info=True,
                    )
                    persisted_state = None
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
            merged_context = dict(persisted_state or {})
            merged_context.update(dict(context or {}))
            effective_comment = str(merged_context.get("comment") or comment)
            if not merged_context.get("signal_id") and not self._is_restorable_comment(effective_comment):
                skipped += 1
                continue

            action = str(
                merged_context.get("action")
                or self._action_from_position_type(getattr(raw_pos, "type", None))
            )
            entry_price = float(
                merged_context.get("entry_price")
                or getattr(raw_pos, "price_open", 0.0)
                or 0.0
            )
            stop_loss = float(getattr(raw_pos, "sl", 0.0) or 0.0)
            take_profit = float(getattr(raw_pos, "tp", 0.0) or 0.0)
            volume = float(getattr(raw_pos, "volume", 0.0) or 0.0)
            opened_at = getattr(raw_pos, "time", None)
            if not isinstance(opened_at, datetime):
                opened_at = datetime.now(timezone.utc)

            # 恢复的仓位缺少 entry_indicators，尝试从当前快照补充
            # （不完美——用的是当前值而非入场时的值，但比完全没有强，
            # 至少能让 Indicator Exit 的方向翻转检测生效）
            restored_indicators: Dict[str, Dict] = {}
            symbol_str = str(getattr(raw_pos, "symbol", "") or "")
            timeframe_str = str(merged_context.get("timeframe") or "")
            if self._indicator_snapshot_fn and symbol_str and timeframe_str:
                try:
                    snap = self._indicator_snapshot_fn(symbol_str, timeframe_str)
                    if snap:
                        restored_indicators = dict(snap)
                except Exception:
                    pass

            pos = TrackedPosition(
                ticket=ticket,
                signal_id=str(merged_context.get("signal_id") or f"restored:{ticket}"),
                symbol=symbol_str,
                action=action,
                entry_price=float(merged_context.get("fill_price") or entry_price),
                stop_loss=stop_loss,
                take_profit=take_profit,
                volume=volume,
                atr_at_entry=float(
                    merged_context.get("atr_at_entry")
                    or self._default_atr_from_position(entry_price, stop_loss)
                ),
                timeframe=timeframe_str,
                strategy=str(merged_context.get("strategy") or ""),
                confidence=merged_context.get("confidence"),
                regime=merged_context.get("regime"),
                source=str(merged_context.get("source") or "mt5_bootstrap"),
                entry_indicators=restored_indicators,
                comment=effective_comment,
                opened_at=opened_at,
                highest_price=(
                    merged_context.get("highest_price")
                    if merged_context.get("highest_price") is not None
                    else (entry_price if action == "buy" else None)
                ),
                lowest_price=(
                    merged_context.get("lowest_price")
                    if merged_context.get("lowest_price") is not None
                    else (entry_price if action == "sell" else None)
                ),
                current_price=merged_context.get("current_price"),
                breakeven_applied=bool(
                    merged_context.get("breakeven_applied")
                    or (
                        (action == "buy" and stop_loss >= entry_price)
                        or (action == "sell" and stop_loss <= entry_price and stop_loss > 0)
                    )
                ),
                trailing_active=bool(merged_context.get("trailing_active")),
            )
            with self._lock:
                self._positions[ticket] = pos
            if self._on_position_tracked is not None:
                self._on_position_tracked(pos, "recovered")
            synced += 1
            if self._recovered_position_callback is not None and merged_context.get("signal_id"):
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

    def set_margin_guard(self, guard: Any) -> None:
        """Inject a MarginGuard instance (optional, called from builder)."""
        self._margin_guard = guard

    def _reconcile_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_end_of_day_closeout()
                self._reconcile_with_mt5()
                self._run_margin_guard()
                self._reconcile_count += 1
                self._last_reconcile_at = datetime.now(timezone.utc)
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("PositionManager reconcile error: %s", exc)
            self._stop_event.wait(timeout=self._reconcile_interval)

    def _run_margin_guard(self) -> None:
        guard = self._margin_guard
        if guard is None or not guard.config.enabled:
            return
        account_fn = getattr(self._trading, "account_info", None)
        if not callable(account_fn):
            return
        try:
            info = account_fn()
        except Exception:
            return
        equity = float(getattr(info, "equity", 0) or 0)
        margin = float(getattr(info, "margin", 0) or 0)
        free_margin = float(getattr(info, "margin_free", 0) or getattr(info, "free_margin", 0) or 0)
        if equity <= 0:
            return
        snapshot = guard.evaluate(equity, margin, free_margin)
        guard.act(snapshot)

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
        # Recover newly-opened MT5 positions each cycle so pending-order fills
        # can enter the management pipeline without requiring a service restart.
        try:
            recovery = self.sync_open_positions()
            if int(recovery.get("synced", 0) or 0) > 0:
                logger.info("PositionManager reconcile recovered positions: %s", recovery)
        except Exception as exc:
            logger.debug("PositionManager: sync_open_positions during reconcile failed: %s", exc)

        with self._lock:
            tracked_tickets = dict(self._positions)

        if not tracked_tickets:
            return

        symbols: set[str] = {pos.symbol for pos in tracked_tickets.values()}
        mt5_positions: Dict[int, Any] = {}
        failed_symbols: set[str] = set()
        for symbol in symbols:
            try:
                open_positions = self._trading.get_positions(symbol=symbol)
                for raw_pos in (open_positions or []):
                    ticket = getattr(raw_pos, "ticket", None)
                    if ticket is not None:
                        mt5_positions[int(ticket)] = raw_pos
            except Exception as exc:
                failed_symbols.add(symbol)
                logger.debug("PositionManager: get_positions(%s) error: %s", symbol, exc)

        for ticket, pos in tracked_tickets.items():
            # 跳过查询失败的 symbol，防止 MT5 连接闪断时误判持仓已关闭
            if pos.symbol in failed_symbols:
                continue
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
                pos.close_source = close_source
                for cb in list(self._close_callbacks):
                    try:
                        cb(pos, close_price)
                    except Exception as cb_exc:
                        logger.warning(
                            "PositionManager: close callback error for ticket=%d: %s",
                            ticket,
                            cb_exc,
                        )
                if self._on_position_closed is not None:
                    self._on_position_closed(pos, close_price)
                continue

            # 检测部分平仓：MT5 volume 与 tracked volume 不一致时更新
            mt5_volume = getattr(mt5_pos, "volume", None)
            if mt5_volume is not None:
                try:
                    live_vol = float(mt5_volume)
                    if live_vol > 0 and abs(live_vol - pos.volume) > 1e-6:
                        logger.info(
                            "PositionManager: partial close detected ticket=%d volume %.2f→%.2f",
                            ticket, pos.volume, live_vol,
                        )
                        with self._lock:
                            pos.volume = live_vol
                        if self._on_position_updated is not None:
                            self._on_position_updated(pos, "partial_close")
                except (TypeError, ValueError):
                    pass

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
                if self._on_position_updated is not None:
                    self._on_position_updated(pos, "breakeven_applied")
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
                if self._on_position_updated is not None:
                    self._on_position_updated(pos, "trailing_sl_updated")
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

    def _modify_tp(self, pos: TrackedPosition, new_tp: float) -> bool:
        try:
            target_tp = round(new_tp, 2)
            result = self._trading.modify_positions(ticket=pos.ticket, symbol=pos.symbol, tp=target_tp)
            if isinstance(result, dict):
                modified = {int(item) for item in result.get("modified", []) if item is not None}
                if modified and pos.ticket not in modified:
                    raise RuntimeError(f"ticket {pos.ticket} not modified")
                failed = list(result.get("failed", []) or [])
                if failed and pos.ticket not in modified:
                    raise RuntimeError(str(failed[0]))
            pos.take_profit = target_tp
            return True
        except Exception as exc:
            logger.warning("Failed to modify TP for ticket=%d: %s", pos.ticket, exc)
            return False

    def _check_trailing_tp(self, pos: TrackedPosition) -> None:
        result = check_trailing_take_profit(
            action=pos.action,
            entry_price=pos.entry_price,
            current_take_profit=pos.take_profit,
            atr_at_entry=pos.atr_at_entry,
            activation_atr=self._trailing_tp_activation_atr,
            trail_atr=self._trailing_tp_trail_atr,
            highest_price=pos.highest_price,
            lowest_price=pos.lowest_price,
        )
        if result.should_update and result.new_take_profit is not None:
            if self._modify_tp(pos, result.new_take_profit):
                if self._on_position_updated is not None:
                    self._on_position_updated(pos, "trailing_tp_updated")
                logger.info(
                    "Trailing TP applied ticket=%d tp=%.2f",
                    pos.ticket, result.new_take_profit,
                )

    def _check_indicator_exit(self, pos: TrackedPosition, current_price: float) -> None:
        if not pos.entry_indicators or not self._indicator_snapshot_fn:
            return
        if not pos.timeframe:
            return
        try:
            current_indicators = self._indicator_snapshot_fn(pos.symbol, pos.timeframe)
        except Exception:
            return
        if not current_indicators:
            return
        result = check_indicator_exit(
            direction=pos.action,
            current_price=current_price,
            current_sl=pos.stop_loss,
            entry_price=pos.entry_price,
            atr_at_entry=pos.atr_at_entry,
            entry_indicators=pos.entry_indicators,
            current_indicators=current_indicators,
            config=self._indicator_exit_config,
        )
        if result.should_tighten_sl and result.new_stop_loss is not None:
            if self._modify_sl(pos, result.new_stop_loss):
                if self._on_position_updated is not None:
                    self._on_position_updated(pos, "indicator_exit_tightened")
                logger.info(
                    "Indicator exit SL tightened ticket=%d reason=%s sl=%.2f",
                    pos.ticket, result.reason, result.new_stop_loss,
                )
