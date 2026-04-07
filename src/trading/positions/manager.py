"""Position manager for signal-initiated trades.

Tracks positions opened by the TradeExecutor. Uses Chandelier Exit
(regime-aware, ATR-dynamic) for trailing stop management via a background
reconcile loop that periodically syncs state with MT5 open positions.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

from src.utils.common import timeframe_seconds

from ..closeout.service import ExposureCloseoutController
from ..execution.sizing import TradeParameters
from ..ports import PositionManagementPort
from .exit_rules import (
    ChandelierConfig,
    check_end_of_day,
    evaluate_exit,
    resolve_aggression,
)


class IndicatorSource(Protocol):
    """读取已计算好的指标快照（由 IndicatorManager 实现）。"""

    def get_indicator(
        self,
        symbol: str,
        timeframe: str,
        indicator_name: str,
    ) -> Optional[Dict[str, Any]]: ...

    def get_all_indicators(
        self,
        symbol: str,
        timeframe: str,
    ) -> Dict[str, Dict[str, Any]]: ...


logger = logging.getLogger(__name__)

# Comment prefixes that identify positions opened by this system.
# "auto:" is the legacy format; timeframe prefixes (e.g. "m5:", "h1:") are the
# current format since the comment was changed to "{tf}:{strategy}:{direction}".
_RESTORABLE_COMMENT_PREFIXES = (
    "auto:",
    "agent:",
    "m1:",
    "m5:",
    "m15:",
    "m30:",
    "h1:",
    "h4:",
    "d1:",
    "w1:",
    "mn1:",
)


@dataclass(frozen=True)
class _ChandelierAction:
    """锁内 evaluate 产出的待执行 SL 修改动作，锁外执行 MT5 API 调用。"""

    pos: "TrackedPosition"
    new_sl: float
    reason: str
    notify_update: bool = False  # 是否通知 on_position_updated 回调


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
    # Chandelier Exit 状态
    initial_risk: float = 0.0  # R = |entry - initial_sl|，开仓时计算
    initial_stop_loss: float = 0.0  # 开仓时的原始 SL
    peak_price: Optional[float] = None
    bars_held: int = 0
    breakeven_activated: bool = False
    strategy_category: str = (
        ""  # trend / reversion / breakout（legacy，结构化策略用 exit_spec）
    )
    exit_spec: dict = field(default_factory=dict)  # 策略 _exit_spec() 输出
    sl_atr_mult: float = 0.0  # 入场 SL 的 ATR 倍数（用于 Chandelier R 单位保护）
    recent_signal_dirs: list = field(default_factory=list)
    # 出场追溯字段（由 _check_chandelier_exit 写入，on_position_closed 读取）
    last_exit_reason: str = (
        ""  # trailing_stop / signal_exit / timeout / stop_loss / take_profit
    )
    last_r_multiple: float = 0.0  # 退出时的 R 倍数
    last_exit_regime: str = ""  # 退出时的 regime
    # 旧字段（兼容 + peak 跟踪）
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
        trading_module: PositionManagementPort,
        end_of_day_closeout: ExposureCloseoutController,
        *,
        chandelier_config: Optional[ChandelierConfig] = None,
        indicator_source: Optional[IndicatorSource] = None,
        regime_detector: Optional[Any] = None,
        end_of_day_close_enabled: bool = False,
        end_of_day_close_hour_utc: int = 21,
        end_of_day_close_minute_utc: int = 0,
    ):
        self._trading = trading_module
        self._end_of_day_closeout = end_of_day_closeout
        self._chandelier_config = chandelier_config or ChandelierConfig()
        self._indicator_source = indicator_source
        self._regime_detector = regime_detector
        self.end_of_day_close_enabled = bool(end_of_day_close_enabled)
        self.end_of_day_close_hour_utc = int(end_of_day_close_hour_utc)
        self.end_of_day_close_minute_utc = int(end_of_day_close_minute_utc)
        self._positions: Dict[int, TrackedPosition] = {}
        self._lock = threading.Lock()
        self._close_callbacks: List[
            Callable[[TrackedPosition, Optional[float]], None]
        ] = []
        self._position_context_resolver: Optional[
            Callable[[int, Optional[str]], Optional[Dict[str, Any]]]
        ] = None
        self._position_state_resolver: Optional[
            Callable[[int], Optional[Dict[str, Any]]]
        ] = None
        self._recovered_position_callback: Optional[
            Callable[[TrackedPosition], None]
        ] = None
        self._on_position_tracked: Optional[Callable[[TrackedPosition, str], None]] = (
            None
        )
        self._on_position_updated: Optional[Callable[[TrackedPosition, str], None]] = (
            None
        )
        self._on_position_closed: Optional[
            Callable[[TrackedPosition, Optional[float]], None]
        ] = None
        self._sl_tp_history_writer: Optional[Callable[[list[tuple]], None]] = None

        self._reconcile_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._reconcile_interval: float = 10.0
        self._reconcile_count: int = 0
        self._last_reconcile_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._last_end_of_day_gate_date: Optional[str] = None
        self._last_end_of_day_close_date: Optional[str] = None
        self._margin_guard: Any = (
            None  # Optional[MarginGuard], injected via set_margin_guard()
        )

    def start(self, reconcile_interval: float = 10.0) -> None:
        if self._reconcile_thread is not None and self._reconcile_thread.is_alive():
            return
        self._reconcile_interval = max(1.0, reconcile_interval)
        self._stop_event.clear()
        # 启动前立即同步一次持仓价格，修复 stop 期间 peak_price 等状态过时的问题
        try:
            self._reconcile_with_mt5()
        except Exception as exc:
            logger.warning(
                "PositionManager: initial reconcile on start failed: %s", exc
            )
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

    def is_running(self) -> bool:
        return self._reconcile_thread is not None and self._reconcile_thread.is_alive()

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
        exit_spec: Optional[dict] = None,
        strategy_category: str = "",
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
        # 计算入场 SL ATR 倍数（用于 Chandelier R 单位保护约束）
        if params.atr_value > 0 and params.sl_distance > 0:
            pos.sl_atr_mult = round(params.sl_distance / params.atr_value, 4)
        if exit_spec:
            pos.exit_spec = dict(exit_spec)
        if strategy_category:
            pos.strategy_category = strategy_category
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
                    pos.peak_price = pos.highest_price
            elif pos.action == "sell":
                if pos.lowest_price is None or current_price < pos.lowest_price:
                    pos.lowest_price = current_price
                    pos.peak_price = pos.lowest_price

            # Chandelier Exit 判断在锁内完成（纯计算），返回待执行的 SL 修改动作
            pending_action = self._evaluate_chandelier_exit(pos, current_price)

        # MT5 API 调用在锁外执行，避免持锁期间阻塞
        if pending_action is not None:
            self._apply_chandelier_action(pending_action)

    def on_signal_event(self, event: Any) -> None:
        """接收 SignalRuntime 的信号事件，更新持仓的 recent_signal_dirs。

        仅处理 scope="confirmed" 的信号（bar close 确认）。
        将信号方向写入对应策略/投票组名下的所有持仓。
        """
        if getattr(event, "scope", "") != "confirmed":
            return
        direction = getattr(event, "direction", "")
        if direction not in ("buy", "sell", "hold"):
            return
        strategy = getattr(event, "strategy", "")
        if not strategy:
            return

        with self._lock:
            for pos in self._positions.values():
                if pos.strategy == strategy:
                    pos.recent_signal_dirs.append(direction)
                    # 只保留最近 N 条
                    max_keep = self._chandelier_config.signal_exit_confirmation_bars + 2
                    if len(pos.recent_signal_dirs) > max_keep:
                        pos.recent_signal_dirs = pos.recent_signal_dirs[-max_keep:]

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
        on_position_closed: Optional[
            Callable[[TrackedPosition, Optional[float]], None]
        ] = None,
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
                "strategy_category": pos.strategy_category,
                "confidence": pos.confidence,
                "regime": pos.regime,
                "source": pos.source,
                "entry_price": pos.entry_price,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "volume": pos.volume,
                "current_price": pos.current_price,
                "peak_price": pos.peak_price,
                "unrealized_pnl": (
                    round(
                        (pos.current_price - pos.entry_price)
                        * (1 if pos.action == "buy" else -1),
                        2,
                    )
                    if pos.current_price is not None
                    else None
                ),
                # Chandelier Exit 实时状态
                "initial_risk": round(pos.initial_risk, 2),
                "r_multiple": pos.last_r_multiple,
                "bars_held": pos.bars_held,
                "breakeven_activated": pos.breakeven_activated,
                "sl_atr_mult": pos.sl_atr_mult,
                "last_exit_reason": pos.last_exit_reason or None,
                "last_exit_regime": pos.last_exit_regime or None,
                "opened_at": pos.opened_at.isoformat(),
            }
            for pos in snapshot
        ]

    def status(self) -> Dict[str, Any]:
        with self._lock:
            position_count = len(self._positions)
        cfg = self._chandelier_config
        return {
            "running": self.is_running(),
            "reconcile_interval": self._reconcile_interval,
            "reconcile_count": self._reconcile_count,
            "last_reconcile_at": (
                self._last_reconcile_at.isoformat() if self._last_reconcile_at else None
            ),
            "last_error": self._last_error,
            "tracked_positions": position_count,
            "config": {
                "end_of_day_close_enabled": self.end_of_day_close_enabled,
                "end_of_day_close_hour_utc": self.end_of_day_close_hour_utc,
                "end_of_day_close_minute_utc": self.end_of_day_close_minute_utc,
                "chandelier_regime_aware": cfg.regime_aware,
                "chandelier_max_tp_r": cfg.max_tp_r,
                "chandelier_breakeven_buffer_r": cfg.breakeven_buffer_r,
                "chandelier_signal_exit_bars": cfg.signal_exit_confirmation_bars,
                "chandelier_timeout_bars": cfg.timeout_bars,
                "chandelier_enforce_r_floor": cfg.enforce_r_floor,
                "chandelier_tf_trail_scale": (
                    dict(cfg.tf_trail_scale) if cfg.tf_trail_scale else {}
                ),
            },
            "last_end_of_day_gate_date": self._last_end_of_day_gate_date,
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
        if self._last_end_of_day_gate_date is None:
            return False
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        if self._last_end_of_day_gate_date != today:
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

        try:
            open_positions = list(self._trading.get_positions())
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
            opened_utc = (
                opened if opened.tzinfo else opened.replace(tzinfo=timezone.utc)
            )
            if opened_utc.date() < now.date():
                overnight.append(pos)

        if not overnight:
            return None

        logger.warning(
            "PositionManager: detected %d overnight positions, force closing",
            len(overnight),
        )
        try:
            result = self._trading.close_all_positions(comment="overnight_force_close")
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
        return any(
            normalized.startswith(prefix) for prefix in _RESTORABLE_COMMENT_PREFIXES
        )

    def sync_open_positions(self) -> dict[str, Any]:
        try:
            open_positions = list(self._trading.get_positions())
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
            if not merged_context.get("signal_id") and not self._is_restorable_comment(
                effective_comment
            ):
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

            symbol_str = str(getattr(raw_pos, "symbol", "") or "")
            timeframe_str = str(merged_context.get("timeframe") or "")

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
                        or (
                            action == "sell"
                            and stop_loss <= entry_price
                            and stop_loss > 0
                        )
                    )
                ),
                trailing_active=bool(merged_context.get("trailing_active")),
            )
            # 计算 sl_atr_mult（恢复路径：从 entry/sl/atr 反算）
            if pos.atr_at_entry > 0 and pos.stop_loss > 0:
                pos.sl_atr_mult = round(
                    abs(pos.entry_price - pos.stop_loss) / pos.atr_at_entry,
                    4,
                )
            with self._lock:
                self._positions[ticket] = pos
            if self._on_position_tracked is not None:
                self._on_position_tracked(pos, "recovered")
            synced += 1
            if self._recovered_position_callback is not None and merged_context.get(
                "signal_id"
            ):
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

    def tighten_trailing_stops(self, factor: float) -> int:
        original = float(self.trailing_atr_multiplier or 0.0)
        tightened = original * float(factor or 0.0)
        if tightened <= 0.0 or tightened >= original:
            return 0
        self.trailing_atr_multiplier = tightened
        with self._lock:
            return len(self._positions)

    def _reconcile_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_end_of_day_closeout()
                self._reconcile_with_mt5()
                self._check_regime_changes()
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
        try:
            info = self._trading.account_info()
        except Exception:
            return
        equity = float(getattr(info, "equity", 0) or 0)
        margin = float(getattr(info, "margin", 0) or 0)
        free_margin = float(
            getattr(info, "margin_free", 0) or getattr(info, "free_margin", 0) or 0
        )
        if equity <= 0:
            return
        snapshot = guard.evaluate(equity, margin, free_margin)
        guard.act(snapshot)

    def _run_end_of_day_closeout(
        self, now: Optional[datetime] = None
    ) -> Optional[dict]:
        if not self.end_of_day_close_enabled:
            return None
        current = now or datetime.now(timezone.utc)

        eod_triggered = check_end_of_day(
            current_time=current,
            close_hour_utc=self.end_of_day_close_hour_utc,
            close_minute_utc=self.end_of_day_close_minute_utc,
            last_close_date=self._last_end_of_day_close_date,
        )
        if not eod_triggered:
            return None

        day_key = (
            current.date().isoformat()
            if current.tzinfo is None
            else current.astimezone(timezone.utc).date().isoformat()
        )
        self._last_end_of_day_gate_date = day_key

        closeout_status = self._end_of_day_closeout.execute(
            reason="end_of_day",
            comment="end_of_day_closeout",
        )
        result = dict(closeout_status.get("result") or {})
        if result.get("completed"):
            self._last_end_of_day_close_date = day_key
            logger.info(
                "PositionManager: end-of-day closeout completed at %s (closed=%s, canceled=%s)",
                current.isoformat(),
                len(result["positions"]["completed"]),
                len(result["orders"]["completed"]),
            )
        else:
            self._last_error = "end_of_day_closeout_incomplete"
            logger.warning(
                "PositionManager: end-of-day closeout incomplete at %s (remaining_positions=%s, remaining_orders=%s)",
                current.isoformat(),
                len(result.get("remaining_positions", []) or []),
                len(result.get("remaining_orders", []) or []),
            )
        return result

    def _reconcile_with_mt5(self) -> None:
        # Recover newly-opened MT5 positions each cycle so pending-order fills
        # can enter the management pipeline without requiring a service restart.
        try:
            recovery = self.sync_open_positions()
            if int(recovery.get("synced", 0) or 0) > 0:
                logger.info(
                    "PositionManager reconcile recovered positions: %s", recovery
                )
        except Exception as exc:
            logger.debug(
                "PositionManager: sync_open_positions during reconcile failed: %s", exc
            )

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
                for raw_pos in open_positions or []:
                    ticket = getattr(raw_pos, "ticket", None)
                    if ticket is not None:
                        mt5_positions[int(ticket)] = raw_pos
            except Exception as exc:
                failed_symbols.add(symbol)
                logger.debug(
                    "PositionManager: get_positions(%s) error: %s", symbol, exc
                )

        for ticket, pos in tracked_tickets.items():
            # 跳过查询失败的 symbol，防止 MT5 连接闪断时误判持仓已关闭
            if pos.symbol in failed_symbols:
                continue
            mt5_pos = mt5_positions.get(ticket)
            if mt5_pos is None:
                close_price = None
                close_source = "mt5_missing"
                try:
                    close_details = self._trading.get_position_close_details(
                        ticket=ticket,
                        symbol=pos.symbol,
                    )
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
                    ticket,
                    pos.action,
                    pos.symbol,
                    close_source,
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
                            ticket,
                            pos.volume,
                            live_vol,
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

    # ── Chandelier Exit 出场检查 ────────────────────────────────────────

    def _get_current_atr(self, pos: TrackedPosition) -> float:
        """从 IndicatorManager 读取当前 ATR（已计算好的缓存值）。"""
        if self._indicator_source is None:
            return pos.atr_at_entry  # fallback: 用开仓时的 ATR
        atr_data = self._indicator_source.get_indicator(
            pos.symbol,
            pos.timeframe,
            "atr14",
        )
        if isinstance(atr_data, dict):
            val = atr_data.get("atr")
            if val is not None and float(val) > 0:
                return float(val)
        return pos.atr_at_entry

    def _get_current_regime(self, pos: TrackedPosition) -> str:
        """从 IndicatorManager 读取指标并检测当前 Regime。"""
        if self._indicator_source is None or self._regime_detector is None:
            return pos.regime or ""
        indicators = self._indicator_source.get_all_indicators(
            pos.symbol,
            pos.timeframe,
        )
        if not indicators:
            return pos.regime or ""
        try:
            regime = self._regime_detector.detect(indicators)
            return regime.value
        except Exception:
            return pos.regime or ""

    def _check_regime_changes(self) -> None:
        """检测 regime 切换并动态调整已有仓位的出场 profile。

        当 regime 从入场时变化时，重新计算 aggression：
        - 如果新 aggression 更低（更保护）→ 立即采用
        - 如果新 aggression 更高（更放手）→ 部分提升（保守过渡）
        目的：regime 恶化时快速收紧出场，regime 改善时谨慎放宽。
        """
        if self._indicator_source is None or self._regime_detector is None:
            return

        with self._lock:
            snapshot = list(self._positions.values())

        for pos in snapshot:
            if not pos.exit_spec or "aggression" not in pos.exit_spec:
                continue

            current_regime = self._get_current_regime(pos)
            if not current_regime or current_regime == (pos.regime or ""):
                continue

            category = pos.strategy_category or ""
            if not category:
                continue

            old_aggression = float(pos.exit_spec["aggression"])
            new_aggression = resolve_aggression(
                category,
                current_regime,
                self._chandelier_config.aggression_overrides or None,
            )

            if abs(new_aggression - old_aggression) < 0.05:
                continue

            if new_aggression < old_aggression:
                # regime 恶化（逆势化）→ 立即采用更保护的 aggression
                adjusted = new_aggression
            else:
                # regime 改善 → 保守过渡：仅提升差值的 50%
                adjusted = old_aggression + (new_aggression - old_aggression) * 0.5

            adjusted = round(max(0.0, min(1.0, adjusted)), 4)

            logger.info(
                "Regime change detected: ticket=%d %s regime=%s→%s "
                "aggression=%.3f→%.3f (category=%s)",
                pos.ticket,
                pos.strategy,
                pos.regime or "?",
                current_regime,
                old_aggression,
                adjusted,
                category,
            )

            with self._lock:
                if pos.ticket in self._positions:
                    pos.exit_spec["aggression"] = adjusted
                    pos.last_exit_regime = current_regime

    @staticmethod
    def _compute_bars_held(pos: TrackedPosition) -> int:
        """从持仓时长和 TF 计算已持有 bar 数。"""
        tf_sec = timeframe_seconds(pos.timeframe)
        elapsed = (datetime.now(timezone.utc) - pos.opened_at).total_seconds()
        return max(0, int(elapsed / tf_sec))

    def _evaluate_chandelier_exit(
        self,
        pos: TrackedPosition,
        current_price: float,
    ) -> "Optional[_ChandelierAction]":
        """Chandelier Exit 纯计算（必须在 _lock 内调用）。

        返回待执行的 SL 修改动作（None = 无需操作）。
        不包含任何 I/O（MT5 API 调用由 _apply_chandelier_action 执行）。
        """
        if pos.initial_risk <= 0:
            return None

        pos.bars_held = self._compute_bars_held(pos)
        current_atr = self._get_current_atr(pos)
        current_regime = self._get_current_regime(pos)

        result = evaluate_exit(
            action=pos.action,
            entry_price=pos.entry_price,
            # 实盘 SL/TP 触发由 MT5 服务器处理，这里传实时价仅用于
            # R 倍数计算、breakeven 判断、Chandelier trail 计算
            bar_high=current_price,
            bar_low=current_price,
            bar_close=current_price,
            current_stop_loss=pos.stop_loss,
            initial_risk=pos.initial_risk,
            peak_price=pos.peak_price or pos.entry_price,
            current_atr=current_atr,
            bars_held=pos.bars_held,
            breakeven_already_activated=pos.breakeven_activated,
            recent_signal_dirs=pos.recent_signal_dirs,
            strategy_category=pos.strategy_category,
            current_regime=current_regime,
            timeframe=pos.timeframe,
            initial_sl_atr_mult=pos.sl_atr_mult,
            config=self._chandelier_config,
            exit_spec=pos.exit_spec or None,
        )

        pos.breakeven_activated = result.breakeven_activated
        # 持续更新追溯字段（每次检查都写入最新值，平仓时读取）
        pos.last_r_multiple = result.r_multiple
        pos.last_exit_regime = current_regime
        if result.close_reason:
            pos.last_exit_reason = result.close_reason

        # 主动退出（信号反转 / 超时）：把 SL 收紧到当前价附近，
        # 由 MT5 服务器在下一个 tick 触发平仓。
        if result.should_close and result.close_reason in ("signal_exit", "timeout"):
            # SL 设到当前价的不利方向 1 点处，确保下一个 tick 触发
            if pos.action == "buy":
                urgent_sl = current_price - 0.01
            else:
                urgent_sl = current_price + 0.01
            logger.info(
                "Chandelier urgent exit: ticket=%d reason=%s strategy=%s r=%.2f → sl=%.2f",
                pos.ticket,
                result.close_reason,
                pos.strategy,
                result.r_multiple,
                urgent_sl,
            )
            return _ChandelierAction(
                pos=pos,
                new_sl=urgent_sl,
                reason=result.close_reason,
            )

        # Trailing SL 更新
        if result.new_stop_loss is not None and result.new_stop_loss != pos.stop_loss:
            return _ChandelierAction(
                pos=pos,
                new_sl=result.new_stop_loss,
                reason="chandelier_trail",
                notify_update=True,
            )

        return None

    def _apply_chandelier_action(self, action: _ChandelierAction) -> None:
        """锁外执行 Chandelier Exit 产出的 SL 修改动作（含 MT5 API 调用）。"""
        # 执行前确认仓位仍然存在（防止在锁释放后仓位已被关闭）
        with self._lock:
            if action.pos.ticket not in self._positions:
                logger.debug(
                    "Chandelier action skipped: ticket=%d already closed",
                    action.pos.ticket,
                )
                return
        if self._modify_sl(action.pos, action.new_sl, reason=action.reason):
            if action.notify_update and self._on_position_updated is not None:
                self._on_position_updated(action.pos, action.reason)

    def _check_chandelier_exit(
        self, pos: TrackedPosition, current_price: float
    ) -> None:
        """Chandelier Exit 持仓检查（用于 reconcile 等无锁上下文）。

        注意：update_price 中已改用 _evaluate_chandelier_exit + _apply_chandelier_action
        的锁安全拆分模式。此方法保留给 reconcile loop 等单线程调用路径。
        """
        action = self._evaluate_chandelier_exit(pos, current_price)
        if action is not None:
            self._apply_chandelier_action(action)

    def _modify_sl(
        self, pos: TrackedPosition, new_sl: float, reason: str = "trailing_sl"
    ) -> bool:
        old_sl = pos.stop_loss
        target_sl = round(new_sl, 2)
        retcode: int | None = None
        broker_comment: str = ""
        success = False
        try:
            result = self._trading.modify_positions(
                ticket=pos.ticket, symbol=pos.symbol, sl=target_sl
            )
            if isinstance(result, dict):
                modified_list = result.get("modified", [])
                modified_tickets = {
                    int(item["ticket"]) if isinstance(item, dict) else int(item)
                    for item in modified_list
                    if item is not None
                }
                if modified_list and isinstance(modified_list[0], dict):
                    retcode = modified_list[0].get("retcode")
                    broker_comment = modified_list[0].get("comment", "")
                if modified_tickets and pos.ticket not in modified_tickets:
                    raise RuntimeError(f"ticket {pos.ticket} not modified")
                failed = list(result.get("failed", []) or [])
                if failed and pos.ticket not in modified_tickets:
                    retcode = (
                        failed[0].get("retcode")
                        if isinstance(failed[0], dict)
                        else None
                    )
                    broker_comment = (
                        str(failed[0].get("error", ""))
                        if isinstance(failed[0], dict)
                        else str(failed[0])
                    )
                    raise RuntimeError(broker_comment)
            pos.stop_loss = target_sl
            success = True
            return True
        except Exception as exc:
            logger.warning("Failed to modify SL for ticket=%d: %s", pos.ticket, exc)
            if not broker_comment:
                broker_comment = str(exc)
            return False
        finally:
            self._record_sl_tp_change(
                pos,
                reason=reason,
                action_type="modify_sl",
                old_sl=old_sl,
                new_sl=target_sl,
                old_tp=None,
                new_tp=None,
                success=success,
                retcode=retcode,
                broker_comment=broker_comment,
            )

    def _modify_tp(
        self, pos: TrackedPosition, new_tp: float, reason: str = "trailing_tp"
    ) -> bool:
        old_tp = pos.take_profit
        target_tp = round(new_tp, 2)
        retcode: int | None = None
        broker_comment: str = ""
        success = False
        try:
            result = self._trading.modify_positions(
                ticket=pos.ticket, symbol=pos.symbol, tp=target_tp
            )
            if isinstance(result, dict):
                modified_list = result.get("modified", [])
                modified_tickets = {
                    int(item["ticket"]) if isinstance(item, dict) else int(item)
                    for item in modified_list
                    if item is not None
                }
                if modified_list and isinstance(modified_list[0], dict):
                    retcode = modified_list[0].get("retcode")
                    broker_comment = modified_list[0].get("comment", "")
                if modified_tickets and pos.ticket not in modified_tickets:
                    raise RuntimeError(f"ticket {pos.ticket} not modified")
                failed = list(result.get("failed", []) or [])
                if failed and pos.ticket not in modified_tickets:
                    retcode = (
                        failed[0].get("retcode")
                        if isinstance(failed[0], dict)
                        else None
                    )
                    broker_comment = (
                        str(failed[0].get("error", ""))
                        if isinstance(failed[0], dict)
                        else str(failed[0])
                    )
                    raise RuntimeError(broker_comment)
            pos.take_profit = target_tp
            success = True
            return True
        except Exception as exc:
            logger.warning("Failed to modify TP for ticket=%d: %s", pos.ticket, exc)
            if not broker_comment:
                broker_comment = str(exc)
            return False
        finally:
            self._record_sl_tp_change(
                pos,
                reason=reason,
                action_type="modify_tp",
                old_sl=None,
                new_sl=None,
                old_tp=old_tp,
                new_tp=target_tp,
                success=success,
                retcode=retcode,
                broker_comment=broker_comment,
            )

    def _record_sl_tp_change(
        self,
        pos: TrackedPosition,
        *,
        reason: str,
        action_type: str,
        old_sl: float | None,
        new_sl: float | None,
        old_tp: float | None,
        new_tp: float | None,
        success: bool,
        retcode: int | None,
        broker_comment: str,
    ) -> None:
        """将 SL/TP 变更记录写入 position_sl_tp_history 表。"""
        if self._sl_tp_history_writer is None:
            return
        from src.utils.timezone import utc_now

        try:
            row = (
                utc_now(),  # recorded_at
                "",  # account_alias（由 writer 填充）
                int(pos.ticket),  # position_ticket
                pos.signal_id,  # signal_id
                pos.symbol,  # symbol
                action_type,  # action_type
                reason,  # reason
                old_sl,  # old_stop_loss
                new_sl,  # new_stop_loss
                old_tp,  # old_take_profit
                new_tp,  # new_take_profit
                pos.current_price,  # current_price
                pos.highest_price,  # highest_price
                pos.lowest_price,  # lowest_price
                pos.atr_at_entry,  # atr_at_entry
                success,  # success
                retcode,  # retcode
                broker_comment[:200] if broker_comment else None,  # broker_comment
                "{}",  # metadata (JSON)
            )
            self._sl_tp_history_writer([row])
        except Exception as exc:
            logger.debug("Failed to write SL/TP history: %s", exc)
