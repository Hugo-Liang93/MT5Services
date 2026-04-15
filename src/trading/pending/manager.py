"""PendingEntryManager: 价格确认入场机制。

信号产生后不立即市价下单，而是等待 Quote 价格落入计算好的入场区间后执行，
结合 confirmed 信号的可靠性与实时价格的精确性。

数据流:
    TradeExecutor._handle_confirmed()
        → PendingEntryManager.submit(PendingEntry)
        → _monitor_loop（后台线程，读取 Quote bid/ask）
        → 价格确认 → _fill_queue → execute_fn()
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from src.signals.models import SignalEvent
from src.trading.runtime.lifecycle import OwnedThreadLifecycle
from src.utils.common import timeframe_seconds

from ..reasons import REASON_NEW_SIGNAL_OVERRIDE, REASON_SHUTDOWN
from ..execution.sizing import TradeParameters
from ..ports import PendingOrderCancellationPort
from .lifecycle import PendingOrderLifecycleManager
from .monitoring import PendingEntryMonitoringService
from .snapshot import PendingEntrySnapshotService

logger = logging.getLogger(__name__)


@dataclass
class PendingEntryConfig:
    """[pending_entry] section 配置。"""

    # 价格监控
    check_interval: float = 0.5
    max_spread_points: float = 0.0  # 0 = 不做额外 spread 检查

    # 超时（bars 数量）— 全局默认值
    timeout_bars: dict[str, float] = field(
        default_factory=lambda: {
            "M1": 3.0,
            "M5": 2.0,
            "M15": 1.5,
            "M30": 1.5,
            "H1": 1.0,
            "H4": 0.5,
            "D1": 0.25,
        }
    )
    default_timeout_bars: float = 2.0

    # 超时入场补偿：超时时若价格偏离参考价 < 此值×ATR，则按市价入场而非直接丢弃
    # 0 = 禁用降级（超时直接丢弃）
    timeout_fill_tolerance_atr: float = 0.5

    # 信号覆盖行为
    cancel_on_new_signal: bool = True
    cancel_same_direction: bool = False


@dataclass
class PendingEntry:
    """一个等待价格确认的挂起入场意图。"""

    signal_event: SignalEvent
    trade_params: TradeParameters
    cost_metrics: dict[str, Any]

    # 入场区间
    entry_low: float
    entry_high: float
    reference_price: float

    # 生命周期
    created_at: datetime
    expires_at: datetime
    status: str = "pending"  # pending / filled / expired / cancelled

    # 区间模式
    zone_mode: str = "pullback"

    # 追踪
    best_price_seen: Optional[float] = None
    checks_count: int = 0
    fill_price: Optional[float] = None
    cancel_reason: str = ""


def _extract_quote_prices(quote: Any) -> Optional[tuple[float, float]]:
    """安全地从 quote 对象提取 (bid, ask)，支持 object 和 dict。"""
    try:
        if isinstance(quote, dict):
            bid = float(quote["bid"])
            ask = float(quote["ask"])
        else:
            bid = float(quote.bid)
            ask = float(quote.ask)
        return bid, ask
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def compute_timeout(
    timeframe: str,
    config: PendingEntryConfig,
) -> timedelta:
    """计算超时时长（纯 TF 驱动）。"""
    tf = timeframe.strip().upper() if timeframe else ""
    bars = config.timeout_bars.get(tf, config.default_timeout_bars)
    # 未知 TF 保守估计用 M5 (300s)；已知 TF 用统一映射
    _KNOWN_TFS = {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"}
    bar_seconds = timeframe_seconds(tf) if tf in _KNOWN_TFS else 300
    return timedelta(seconds=bars * bar_seconds)


# ── 填单结果（从 monitor 线程传递到 fill_worker 线程） ────────────────────────
@dataclass(frozen=True)
class _FillResult:
    """monitor 线程确认价格后产生的填单指令。"""

    signal_event: SignalEvent
    trade_params: TradeParameters  # 已更新 entry_price
    cost_metrics: dict[str, Any]


class PendingEntryManager:
    """管理挂起的入场意图，监控 Quote 价格确认后执行。

    线程模型：
    - monitor 线程：轮询 Quote，检查价格区间，将填单结果写入 _fill_queue
    - fill_worker 线程：消费 _fill_queue，调用 execute_fn（避免与 TradeExecutor 竞争）
    - 所有对 _pending 和 _stats 的访问都持 _lock
    """

    def __init__(
        self,
        config: PendingEntryConfig,
        market_service: Any,
        cancellation_port: PendingOrderCancellationPort,
        execute_fn: Callable[[SignalEvent, TradeParameters, Dict[str, Any]], Any],
        on_expired_fn: Optional[Callable[[str, str], None]] = None,
        inspect_mt5_order_fn: Optional[
            Callable[[Dict[str, Any]], Dict[str, Any]]
        ] = None,
    ):
        self._config = config
        self._market = market_service
        self._cancellation_port = cancellation_port
        self._execute_fn = execute_fn
        self._on_expired_fn = on_expired_fn  # (signal_id, reason) 过期回调
        self._inspect_mt5_order_fn = inspect_mt5_order_fn
        self._on_mt5_order_tracked: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_mt5_order_filled: Optional[
            Callable[[Dict[str, Any], Dict[str, Any]], None]
        ] = None
        self._on_mt5_order_expired: Optional[Callable[[Dict[str, Any], str], None]] = (
            None
        )
        self._on_mt5_order_cancelled: Optional[
            Callable[[Dict[str, Any], str], None]
        ] = None
        self._on_mt5_order_missing: Optional[Callable[[Dict[str, Any], str], None]] = (
            None
        )

        self._pending: dict[str, PendingEntry] = {}  # signal_id → PendingEntry
        # MT5 挂单追踪：signal_id → {ticket, expires_at, direction, symbol, strategy}
        self._mt5_orders: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()  # RLock: 回调链可能重入 submit/cancel
        self._monitor_thread: Optional[threading.Thread] = None
        self._fill_worker_thread: Optional[threading.Thread] = None
        # 复用 OwnedThreadLifecycle 工具统一线程生命周期管理（DRY + ADR-005 contract），
        # 不再手写 join/timeout/僵尸清理逻辑。
        self._monitor_lifecycle = OwnedThreadLifecycle(
            self, "_monitor_thread", label="PendingEntryMonitor"
        )
        self._fill_lifecycle = OwnedThreadLifecycle(
            self, "_fill_worker_thread", label="PendingEntryFill"
        )
        self._fill_queue: queue.Queue[_FillResult] = queue.Queue(maxsize=128)
        self._stop_event = threading.Event()

        # 统计（受 _lock 保护）
        self._stats = {
            "total_submitted": 0,
            "total_filled": 0,
            "total_expired": 0,
            "total_cancelled": 0,
            "total_price_improvement": 0.0,
            "mt5_orders_placed": 0,
            "mt5_orders_filled": 0,
            "mt5_orders_expired": 0,
            "mt5_orders_missing": 0,
        }

        self._lifecycle_manager = PendingOrderLifecycleManager(
            lock=self._lock,
            mt5_orders=self._mt5_orders,
            cancellation_port=self._cancellation_port,
            inspect_mt5_order_fn=self._inspect_mt5_order_fn,
            stats=self._stats,
        )
        self._monitoring_service = PendingEntryMonitoringService(
            stop_event=self._stop_event,
            pending=self._pending,
            lock=self._lock,
            fill_queue=self._fill_queue,
            market_service=self._market,
            execute_fn=self._execute_fn,
            on_expired_fn=self._on_expired_fn,
            extract_quote_prices=_extract_quote_prices,
            fill_result_factory=lambda signal_event, trade_params, cost_metrics: _FillResult(
                signal_event=signal_event,
                trade_params=trade_params,
                cost_metrics=cost_metrics,
            ),
            stats=self._stats,
            check_interval=self._config.check_interval,
            max_spread_points=self._config.max_spread_points,
            timeout_fill_tolerance_atr=self._config.timeout_fill_tolerance_atr,
        )
        self._snapshot_service = PendingEntrySnapshotService(
            pending=self._pending,
            mt5_orders=self._mt5_orders,
            lock=self._lock,
            stats=self._stats,
        )

    @property
    def config(self) -> PendingEntryConfig:
        return self._config

    @config.setter
    def config(self, value: PendingEntryConfig) -> None:
        self._config = value
        self._monitoring_service.set_runtime_config(
            check_interval=value.check_interval,
            max_spread_points=value.max_spread_points,
            timeout_fill_tolerance_atr=value.timeout_fill_tolerance_atr,
        )

    def submit(self, entry: PendingEntry) -> None:
        """提交一个新的挂起入场。"""
        with self._lock:
            self._pending[entry.signal_event.signal_id] = entry
            self._stats["total_submitted"] += 1
        logger.info(
            "PendingEntry submitted: %s/%s %s zone=[%.2f, %.2f] mode=%s expires=%s",
            entry.signal_event.symbol,
            entry.signal_event.strategy,
            entry.signal_event.direction,
            entry.entry_low,
            entry.entry_high,
            entry.zone_mode,
            entry.expires_at.strftime("%H:%M:%S"),
        )

    def set_mt5_order_lifecycle_hooks(
        self,
        *,
        on_tracked: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_filled: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
        on_expired: Optional[Callable[[Dict[str, Any], str], None]] = None,
        on_cancelled: Optional[Callable[[Dict[str, Any], str], None]] = None,
        on_missing: Optional[Callable[[Dict[str, Any], str], None]] = None,
    ) -> None:
        self._on_mt5_order_tracked = on_tracked
        self._on_mt5_order_filled = on_filled
        self._on_mt5_order_expired = on_expired
        self._on_mt5_order_cancelled = on_cancelled
        self._on_mt5_order_missing = on_missing
        self._lifecycle_manager.set_order_lifecycle_hooks(
            on_tracked=on_tracked,
            on_filled=on_filled,
            on_expired=on_expired,
            on_cancelled=on_cancelled,
            on_missing=on_missing,
        )

    def inspect_mt5_order(self, info: Dict[str, Any]) -> Dict[str, Any]:
        return self._lifecycle_manager.inspect_mt5_order(info)

    def restore_mt5_order(self, info: Dict[str, Any]) -> None:
        self._lifecycle_manager.restore_mt5_order(info)

    def cancel(self, signal_id: str, reason: str = "manual") -> bool:
        """取消指定的挂起入场。"""
        with self._lock:
            entry = self._pending.pop(signal_id, None)
            if entry is None:
                return False
            entry.status = "cancelled"
            entry.cancel_reason = reason
            self._stats["total_cancelled"] += 1
        logger.info(
            "PendingEntry cancelled: %s/%s %s reason=%s",
            entry.signal_event.symbol,
            entry.signal_event.strategy,
            entry.signal_event.direction,
            reason,
        )
        if self._on_expired_fn:
            try:
                self._on_expired_fn(signal_id, f"pending_cancelled:{reason}")
            except Exception:
                logger.warning(
                    "on_expired_fn callback failed for cancelled signal %s",
                    signal_id,
                    exc_info=True,
                )
        return True

    def cancel_by_symbol(
        self,
        symbol: str,
        reason: str = REASON_NEW_SIGNAL_OVERRIDE,
        *,
        exclude_direction: Optional[str] = None,
    ) -> int:
        """取消指定品种的所有挂起入场。

        Args:
            exclude_direction: 不取消该方向的 pending（用于 cancel_same_direction=false）
        """
        cancelled_entries: list[tuple[str, PendingEntry]] = []
        with self._lock:
            to_remove = [
                sid
                for sid, entry in self._pending.items()
                if entry.signal_event.symbol == symbol
                and entry.status == "pending"
                and (
                    exclude_direction is None
                    or entry.signal_event.direction != exclude_direction
                )
            ]
            for sid in to_remove:
                entry = self._pending.pop(sid)
                entry.status = "cancelled"
                entry.cancel_reason = reason
                self._stats["total_cancelled"] += 1
                cancelled_entries.append((sid, entry))
        # 回调在锁外执行（避免死锁）
        for sid, entry in cancelled_entries:
            if self._on_expired_fn:
                try:
                    self._on_expired_fn(sid, f"pending_cancelled:{reason}")
                except Exception:
                    logger.warning(
                        "on_expired_fn callback failed for cancelled signal %s",
                        sid,
                        exc_info=True,
                    )
        if cancelled_entries:
            logger.info(
                "PendingEntry cancel_by_symbol %s: cancelled %d entries (reason=%s)",
                symbol,
                len(cancelled_entries),
                reason,
            )
        # 同时取消该品种的 MT5 挂单
        mt5_cancelled = self.cancel_mt5_orders_by_symbol(
            symbol,
            reason,
            exclude_direction=exclude_direction,
        )
        return len(cancelled_entries) + mt5_cancelled

    # ── MT5 挂单生命周期管理 ──────────────────────────────────────

    def track_mt5_order(
        self,
        signal_id: str,
        order_ticket: int,
        expires_at: datetime,
        direction: str,
        symbol: str,
        strategy: str,
        *,
        timeframe: str = "",
        confidence: Optional[float] = None,
        regime: Optional[str] = None,
        comment: str = "",
        params: Optional[TradeParameters] = None,
        order_kind: str = "",
        entry_low: Optional[float] = None,
        entry_high: Optional[float] = None,
        trigger_price: Optional[float] = None,
        entry_price_requested: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        volume: Optional[float] = None,
        created_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
        exit_spec: Optional[Dict[str, Any]] = None,
        strategy_category: str = "",
    ) -> None:
        """注册 MT5 挂单，由 monitor loop 负责超时取消。"""
        self._lifecycle_manager.track_mt5_order(
            signal_id=signal_id,
            order_ticket=order_ticket,
            expires_at=expires_at,
            direction=direction,
            symbol=symbol,
            strategy=strategy,
            timeframe=timeframe,
            confidence=confidence,
            regime=regime,
            comment=comment,
            params=params,
            order_kind=order_kind,
            entry_low=entry_low,
            entry_high=entry_high,
            trigger_price=trigger_price,
            entry_price_requested=entry_price_requested,
            stop_loss=stop_loss,
            take_profit=take_profit,
            volume=volume,
            created_at=created_at,
            metadata=metadata,
            exit_spec=exit_spec,
            strategy_category=strategy_category,
        )

    def cancel_mt5_orders_by_symbol(
        self,
        symbol: str,
        reason: str = REASON_NEW_SIGNAL_OVERRIDE,
        *,
        exclude_direction: Optional[str] = None,
    ) -> int:
        """取消指定品种的所有 MT5 挂单。"""
        return self._lifecycle_manager.cancel_mt5_orders_by_symbol(
            symbol=symbol,
            reason=reason,
            exclude_direction=exclude_direction,
        )

    def start(self) -> None:
        """启动价格监控线程和填单执行线程。

        线程生命周期由 OwnedThreadLifecycle 统一管理（ADR-005 contract）：
          - wait_previous: 等待上次 shutdown() 超时遗留的僵尸线程退出再启动
          - ensure_running: 仅在未运行时创建新线程，避免重复 spawn
        """
        # 清理上次 shutdown 超时残留的僵尸引用（如有）
        if not self._monitor_lifecycle.wait_previous(timeout=5.0):
            return
        if not self._fill_lifecycle.wait_previous(timeout=5.0):
            return

        if self.is_running():
            return

        self._stop_event.clear()
        self._monitor_lifecycle.ensure_running(
            lambda: threading.Thread(
                target=self._monitor_loop,
                name="pending-entry-monitor",
                daemon=True,
            )
        )
        self._fill_lifecycle.ensure_running(
            lambda: threading.Thread(
                target=self._monitoring_service.run_fill_worker,
                name="pending-entry-fill",
                daemon=True,
            )
        )
        logger.info(
            "PendingEntryManager started (check_interval=%.2fs)",
            self._config.check_interval,
        )

    def is_running(self) -> bool:
        return (
            self._monitor_lifecycle.is_running()
            and self._fill_lifecycle.is_running()
        )

    def shutdown(self) -> None:
        """停止监控并清理。

        先等待 monitor 和 fill_worker 线程完全退出，
        再清理 _pending，避免线程仍在迭代时并发修改。

        两个 lifecycle 共享同一个 _stop_event：第一次 stop() 调用 set 后，
        两个线程都会收到信号自然退出；第二次 stop() 的 set 是幂等无副作用。
        若任一线程超时，其 lifecycle 会保留引用（不清空），下次 start()
        通过 wait_previous 处理（ADR-005）。
        """
        self._monitor_lifecycle.stop(self._stop_event, timeout=5.0)
        self._fill_lifecycle.stop(self._stop_event, timeout=5.0)
        self._monitoring_service.clear_fill_queue()
        # 线程已退出（或超时被 lifecycle 标记），安全清理 _pending
        with self._lock:
            for entry in self._pending.values():
                if entry.status == "pending":
                    entry.status = "cancelled"
                    entry.cancel_reason = REASON_SHUTDOWN
            self._pending.clear()

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._pending.values() if e.status == "pending")

    def status(self) -> dict[str, Any]:
        """返回状态快照（线程安全）。"""
        return self._snapshot_service.status()

    # ── 内部逻辑 ──────────────────────────────────────────────────────────

    def active_execution_contexts(self) -> list[dict[str, Any]]:
        return self._snapshot_service.active_execution_contexts()

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_all_entries()
                self._check_mt5_order_state()
                self._check_mt5_order_expiry()
            except Exception:
                logger.error("PendingEntryManager monitor error", exc_info=True)
            self._stop_event.wait(timeout=self._monitoring_service.monitor_interval())

    def _check_all_entries(self) -> None:
        self._monitoring_service.check_all_entries()

    def _check_mt5_order_state(self) -> None:
        self._lifecycle_manager.check_mt5_order_state()

    def _check_mt5_order_expiry(self) -> None:
        self._lifecycle_manager.check_mt5_order_expiry(self._on_expired_fn)
