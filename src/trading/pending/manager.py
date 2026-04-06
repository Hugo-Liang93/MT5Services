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

import dataclasses
import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from src.signals.models import SignalEvent
from ..execution.sizing import TradeParameters

from ..ports import PendingOrderCancellationPort

logger = logging.getLogger(__name__)

# ── 时间框架 → bar 周期秒数映射 ──────────────────────────────────────────────
_TF_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
    "MN": 2592000,
}


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


    # 超时降级：超时时如果价格偏离参考价 < 此值×ATR，以市价入场而非丢弃
    # 0 = 禁用降级（超时直接丢弃）
    timeout_fallback_atr: float = 0.5

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
    bar_seconds = _TF_SECONDS.get(tf, 300)
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
        inspect_mt5_order_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self._config = config
        self._market = market_service
        self._cancellation_port = cancellation_port
        self._execute_fn = execute_fn
        self._on_expired_fn = on_expired_fn  # (signal_id, reason) 过期回调
        self._inspect_mt5_order_fn = inspect_mt5_order_fn
        self._on_mt5_order_tracked: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_mt5_order_filled: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None
        self._on_mt5_order_expired: Optional[Callable[[Dict[str, Any], str], None]] = None
        self._on_mt5_order_cancelled: Optional[Callable[[Dict[str, Any], str], None]] = None
        self._on_mt5_order_missing: Optional[Callable[[Dict[str, Any], str], None]] = None

        self._pending: dict[str, PendingEntry] = {}  # signal_id → PendingEntry
        # MT5 挂单追踪：signal_id → {ticket, expires_at, direction, symbol, strategy}
        self._mt5_orders: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()  # RLock: 回调链可能重入 submit/cancel
        self._monitor_thread: Optional[threading.Thread] = None
        self._fill_worker_thread: Optional[threading.Thread] = None
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

    @property
    def config(self) -> PendingEntryConfig:
        return self._config

    @config.setter
    def config(self, value: PendingEntryConfig) -> None:
        self._config = value

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

    def inspect_mt5_order(self, info: Dict[str, Any]) -> Dict[str, Any]:
        inspector = self._inspect_mt5_order_fn
        if not callable(inspector):
            return {"status": "pending"}
        return inspector(dict(info)) or {"status": "pending"}

    def restore_mt5_order(self, info: Dict[str, Any]) -> None:
        restored = dict(info)
        signal_id = str(restored.get("signal_id") or "").strip()
        if not signal_id:
            return
        with self._lock:
            self._mt5_orders[signal_id] = restored

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
        reason: str = "new_signal_override",
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
    ) -> None:
        """注册 MT5 挂单，由 monitor loop 负责超时取消。"""
        tracked_info = {
            "ticket": order_ticket,
            "signal_id": signal_id,
            "expires_at": expires_at,
            "direction": direction,
            "symbol": symbol,
            "strategy": strategy,
            "timeframe": timeframe,
            "confidence": confidence,
            "regime": regime,
            "comment": str(comment or ""),
            "params": params,
            "order_kind": str(order_kind or ""),
            "entry_low": entry_low,
            "entry_high": entry_high,
            "trigger_price": trigger_price,
            "entry_price_requested": entry_price_requested,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "volume": volume,
            "created_at": created_at,
            "metadata": dict(metadata or {}),
        }
        with self._lock:
            if signal_id in self._mt5_orders:
                existing_ticket = self._mt5_orders[signal_id].get("ticket")
                logger.warning(
                    "PendingEntry: signal_id=%s already tracked with ticket=%s, "
                    "ignoring duplicate submission with ticket=%d",
                    signal_id, existing_ticket, order_ticket,
                )
                return
            self._mt5_orders[signal_id] = tracked_info
            self._stats["mt5_orders_placed"] += 1
        if self._on_mt5_order_tracked is not None:
            self._on_mt5_order_tracked(dict(tracked_info))
        logger.info(
            "PendingEntry: tracking MT5 order ticket=%d for %s/%s %s (expires=%s)",
            order_ticket, symbol, strategy, direction,
            expires_at.isoformat(),
        )

    def _check_mt5_order_state(self) -> None:
        """检查已追踪 MT5 挂单是否仍在挂单簿，或已经成交转为持仓。"""
        if not callable(self._inspect_mt5_order_fn):
            return

        with self._lock:
            tracked_orders = [
                (sid, dict(info))
                for sid, info in self._mt5_orders.items()
            ]

        for sid, info in tracked_orders:
            try:
                state = self.inspect_mt5_order(dict(info))
            except Exception:
                logger.warning(
                    "PendingEntry: inspect_mt5_order_fn failed for signal %s",
                    sid,
                    exc_info=True,
                )
                continue

            status = str(state.get("status") or "pending").strip().lower()
            if status not in {"filled", "missing"}:
                continue

            with self._lock:
                current = self._mt5_orders.get(sid)
                if current is None:
                    continue
                if int(current.get("ticket", 0) or 0) != int(info.get("ticket", 0) or 0):
                    continue
                self._mt5_orders.pop(sid, None)
                if status == "filled":
                    self._stats["mt5_orders_filled"] += 1
                else:
                    self._stats["mt5_orders_missing"] += 1

            if status == "filled" and self._on_mt5_order_filled is not None:
                self._on_mt5_order_filled(dict(info), dict(state))
            if status == "missing" and self._on_mt5_order_missing is not None:
                self._on_mt5_order_missing(
                    dict(info),
                    str(state.get("reason") or "missing_without_fill"),
                )

            if status == "filled":
                logger.info(
                    "PendingEntry: MT5 order ticket=%d filled for %s/%s (signal=%s, position=%s)",
                    int(info.get("ticket", 0) or 0),
                    info.get("symbol"),
                    info.get("strategy"),
                    sid[:8],
                    state.get("ticket"),
                )
            else:
                logger.info(
                    "PendingEntry: MT5 order ticket=%d no longer exists for %s/%s "
                    "(signal=%s, reason=%s)",
                    int(info.get("ticket", 0) or 0),
                    info.get("symbol"),
                    info.get("strategy"),
                    sid[:8],
                    state.get("reason") or "missing_without_fill",
                )

    def _check_mt5_order_expiry(self) -> None:
        """检查 MT5 挂单是否超时，超时则通过 MT5 API 取消。"""
        now = datetime.now(timezone.utc)
        expired: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            for sid, info in list(self._mt5_orders.items()):
                if now >= info["expires_at"]:
                    expired.append((sid, dict(info)))

        for sid, info in expired:
            ticket = info["ticket"]
            cancelled = False
            result: Any = None
            try:
                result = self._cancellation_port.cancel_orders_by_tickets([ticket])
                cancelled = self._ticket_in_result(result, ticket, success_keys=("canceled", "cancelled"))
            except Exception:
                logger.error(
                    "PendingEntry: failed to cancel MT5 order ticket=%d",
                    ticket, exc_info=True,
                )
            if cancelled:
                logger.info(
                    "PendingEntry: cancelled expired MT5 order ticket=%d "
                    "for %s/%s (signal=%s)",
                    ticket, info["symbol"], info["strategy"], sid[:8],
                )
                with self._lock:
                    current = self._mt5_orders.get(sid)
                    if current is not None and int(current.get("ticket", 0) or 0) == int(ticket):
                        self._mt5_orders.pop(sid, None)
                        self._stats["mt5_orders_expired"] += 1
                if self._on_mt5_order_expired is not None:
                    self._on_mt5_order_expired(dict(info), "mt5_order_expired")
            if self._on_expired_fn:
                try:
                    if cancelled:
                        self._on_expired_fn(sid, "mt5_order_expired")
                except Exception:
                    pass
            elif result is not None:
                logger.warning(
                    "PendingEntry: expiry cancel not confirmed for MT5 order ticket=%d "
                    "for %s/%s (signal=%s, result=%s)",
                    ticket,
                    info["symbol"],
                    info["strategy"],
                    sid[:8],
                    result,
                )

    def cancel_mt5_orders_by_symbol(
        self,
        symbol: str,
        reason: str = "new_signal_override",
        *,
        exclude_direction: Optional[str] = None,
    ) -> int:
        """取消指定品种的所有 MT5 挂单。"""
        to_cancel: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            for sid, info in list(self._mt5_orders.items()):
                if (
                    info["symbol"] == symbol
                    and (
                        exclude_direction is None
                        or str(info.get("direction") or "") != exclude_direction
                    )
                ):
                    to_cancel.append((sid, dict(info)))
        cancelled_count = 0
        for sid, info in to_cancel:
            ticket = info["ticket"]
            try:
                result = self._cancellation_port.cancel_orders_by_tickets([ticket])
                if self._ticket_in_result(result, ticket, success_keys=("canceled", "cancelled")):
                    with self._lock:
                        current = self._mt5_orders.get(sid)
                        if current is not None and int(current.get("ticket", 0) or 0) == int(ticket):
                            self._mt5_orders.pop(sid, None)
                    cancelled_count += 1
                    if self._on_mt5_order_cancelled is not None:
                        self._on_mt5_order_cancelled(dict(info), reason)
            except Exception:
                logger.error("Failed to cancel MT5 order ticket=%d", ticket, exc_info=True)
        return cancelled_count

    @staticmethod
    def _ticket_in_result(result: Any, ticket: int, *, success_keys: tuple[str, ...]) -> bool:
        if isinstance(result, dict):
            success = set()
            for key in success_keys:
                success.update(
                    PendingEntryManager._extract_tickets(result.get(key, []))
                )
            failed = PendingEntryManager._extract_tickets(result.get("failed", []))
            if ticket in success:
                return True
            if failed:
                return ticket not in failed
        return bool(result)

    @staticmethod
    def _extract_tickets(items: Any) -> set[int]:
        tickets: set[int] = set()
        for item in list(items or []):
            candidate = item
            if isinstance(item, dict):
                candidate = (
                    item.get("ticket")
                    or item.get("order")
                    or item.get("order_id")
                )
            try:
                normalized = int(candidate)
            except (TypeError, ValueError):
                continue
            if normalized > 0:
                tickets.add(normalized)
        return tickets

    def start(self) -> None:
        """启动价格监控线程和填单执行线程。"""
        monitor_alive = self._monitor_thread is not None and self._monitor_thread.is_alive()
        fill_alive = self._fill_worker_thread is not None and self._fill_worker_thread.is_alive()
        if monitor_alive and fill_alive:
            return
        self._stop_event.clear()
        if not monitor_alive:
            t = threading.Thread(
                target=self._monitor_loop, name="pending-entry-monitor", daemon=True
            )
            t.start()
            self._monitor_thread = t
        if not fill_alive:
            fw = threading.Thread(
                target=self._fill_worker, name="pending-entry-fill", daemon=True
            )
            fw.start()
            self._fill_worker_thread = fw
        logger.info("PendingEntryManager started (check_interval=%.2fs)", self._config.check_interval)

    def is_running(self) -> bool:
        monitor_alive = self._monitor_thread is not None and self._monitor_thread.is_alive()
        fill_alive = self._fill_worker_thread is not None and self._fill_worker_thread.is_alive()
        return monitor_alive and fill_alive

    def shutdown(self) -> None:
        """停止监控并清理。

        先等待 monitor 和 fill_worker 线程完全退出，
        再清理 _pending，避免线程仍在迭代时并发修改。
        """
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5.0)
            self._monitor_thread = None
        if self._fill_worker_thread is not None:
            self._fill_worker_thread.join(timeout=5.0)
            self._fill_worker_thread = None
        self._clear_fill_queue()
        # 线程已退出，安全清理 _pending
        with self._lock:
            for entry in self._pending.values():
                if entry.status == "pending":
                    entry.status = "cancelled"
                    entry.cancel_reason = "shutdown"
            self._pending.clear()

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._pending.values() if e.status == "pending")

    def status(self) -> dict[str, Any]:
        """返回状态快照（线程安全）。"""
        with self._lock:
            entries = [
                {
                    "signal_id": e.signal_event.signal_id,
                    "symbol": e.signal_event.symbol,
                    "direction": e.signal_event.direction,
                    "strategy": e.signal_event.strategy,
                    "zone": [e.entry_low, e.entry_high],
                    "reference_price": e.reference_price,
                    "zone_mode": e.zone_mode,
                    "checks_count": e.checks_count,
                    "best_price_seen": e.best_price_seen,
                    "remaining_seconds": max(
                        0,
                        (e.expires_at - datetime.now(timezone.utc)).total_seconds(),
                    ),
                }
                for e in self._pending.values()
                if e.status == "pending"
            ]
            stats_copy = dict(self._stats)
        filled = stats_copy["total_filled"]
        submitted = stats_copy["total_submitted"]
        return {
            "active_count": len(entries),
            "entries": entries,
            "stats": {
                **stats_copy,
                "fill_rate": round(filled / submitted, 3) if submitted > 0 else None,
                "avg_price_improvement": (
                    round(stats_copy["total_price_improvement"] / filled, 4)
                    if filled > 0
                    else None
                ),
            },
        }

    # ── 内部逻辑 ──────────────────────────────────────────────────────────

    def active_execution_contexts(self) -> list[dict[str, Any]]:
        with self._lock:
            pending_entries = [
                {
                    "signal_id": e.signal_event.signal_id,
                    "symbol": e.signal_event.symbol,
                    "timeframe": e.signal_event.timeframe,
                    "strategy": e.signal_event.strategy,
                    "direction": e.signal_event.direction,
                    "source": "pending_entry",
                    "status": e.status,
                }
                for e in self._pending.values()
                if e.status == "pending"
            ]
            mt5_entries = [
                {
                    "signal_id": str(info.get("signal_id") or ""),
                    "symbol": str(info.get("symbol") or ""),
                    "timeframe": str(info.get("timeframe") or ""),
                    "strategy": str(info.get("strategy") or ""),
                    "direction": str(info.get("direction") or ""),
                    "source": "mt5_order",
                    "status": "pending",
                    "ticket": int(info.get("ticket") or 0),
                }
                for info in self._mt5_orders.values()
            ]
        return pending_entries + mt5_entries

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_all_entries()
                self._check_mt5_order_state()
                self._check_mt5_order_expiry()
            except Exception:
                logger.error("PendingEntryManager monitor error", exc_info=True)
            self._stop_event.wait(timeout=self._config.check_interval)

    def _fill_worker(self) -> None:
        """独立线程消费填单结果，调用 execute_fn。

        与 TradeExecutor 的 exec_worker 隔离，避免竞争 TradeExecutor 内部状态。
        """
        while not self._stop_event.is_set() or not self._fill_queue.empty():
            try:
                result = self._fill_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._execute_fn(
                    result.signal_event,
                    result.trade_params,
                    result.cost_metrics,
                )
            except Exception:
                logger.error(
                    "PendingEntry execute failed after fill: %s",
                    result.signal_event.signal_id,
                    exc_info=True,
                )
            finally:
                self._fill_queue.task_done()

    def _clear_fill_queue(self) -> None:
        while True:
            try:
                self._fill_queue.get_nowait()
                self._fill_queue.task_done()
            except queue.Empty:
                break

    def _check_all_entries(self) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            entries = [
                e for e in self._pending.values() if e.status == "pending"
            ]

        for entry in entries:
            # 超时检查
            if now >= entry.expires_at:
                self._expire_entry(entry)
                continue

            # 获取当前 quote
            quote = self._market.get_quote(entry.signal_event.symbol)
            if quote is None:
                continue

            self._check_price(entry, quote)

    def _check_price(self, entry: PendingEntry, quote: Any) -> None:
        prices = _extract_quote_prices(quote)
        if prices is None:
            return

        bid, ask = prices
        direction = entry.signal_event.direction
        # BUY → 用 ask（实际买入价），SELL → 用 bid（实际卖出价）
        check_price = ask if direction == "buy" else bid

        with self._lock:
            # 检查 entry 是否仍在 pending 中（可能已被其他线程 cancel）
            if entry.signal_event.signal_id not in self._pending:
                return
            if entry.status != "pending":
                return

            entry.checks_count += 1

            # 更新 best_price_seen
            if entry.best_price_seen is None:
                entry.best_price_seen = check_price
            elif direction == "buy":
                entry.best_price_seen = min(entry.best_price_seen, check_price)
            else:
                entry.best_price_seen = max(entry.best_price_seen, check_price)

        # 检查是否在入场区间内
        if not (entry.entry_low <= check_price <= entry.entry_high):
            return

        # 额外 spread 检查
        if self._config.max_spread_points > 0:
            spread_points = self._get_spread_points(bid, ask, entry.signal_event.symbol)
            if spread_points is not None and spread_points > self._config.max_spread_points:
                logger.debug(
                    "PendingEntry %s: spread %.1f > max %.1f, skip this check",
                    entry.signal_event.signal_id[:8],
                    spread_points,
                    self._config.max_spread_points,
                )
                return

        self._fill_entry(entry, check_price)

    def _get_spread_points(self, bid: float, ask: float, symbol: str) -> Optional[float]:
        try:
            point = self._market.get_symbol_point(symbol)
            if point is None or point <= 0:
                return None
            return abs(ask - bid) / point
        except Exception:
            return None

    def _fill_entry(self, entry: PendingEntry, fill_price: float) -> None:
        with self._lock:
            # 再次检查 entry 是否仍在 pending 中
            if self._pending.pop(entry.signal_event.signal_id, None) is None:
                return
            entry.status = "filled"
            entry.fill_price = fill_price

            self._stats["total_filled"] += 1
            # 价格改善：BUY 时 reference - fill（越低越好），SELL 时 fill - reference（越高越好）
            if entry.signal_event.direction == "buy":
                improvement = entry.reference_price - fill_price
            else:
                improvement = fill_price - entry.reference_price
            self._stats["total_price_improvement"] += improvement

        logger.info(
            "PendingEntry filled: %s/%s %s @ %.2f (ref=%.2f, improve=%.2f, "
            "waited=%ds, checks=%d)",
            entry.signal_event.symbol,
            entry.signal_event.strategy,
            entry.signal_event.direction,
            fill_price,
            entry.reference_price,
            improvement,
            (datetime.now(timezone.utc) - entry.created_at).total_seconds(),
            entry.checks_count,
        )

        # 用实际成交价更新 TradeParameters（frozen dataclass → replace）
        # SL/TP 随 fill_price 等距平移，保持原始 ATR 距离不变
        price_shift = fill_price - entry.trade_params.entry_price
        updated_params = dataclasses.replace(
            entry.trade_params,
            entry_price=fill_price,
            stop_loss=round(entry.trade_params.stop_loss + price_shift, 2),
            take_profit=round(entry.trade_params.take_profit + price_shift, 2),
        )

        # 入队给 fill_worker 执行（不在 monitor 线程中直接调用 execute_fn）
        fill_result = _FillResult(
            signal_event=entry.signal_event,
            trade_params=updated_params,
            cost_metrics=entry.cost_metrics,
        )
        try:
            self._fill_queue.put_nowait(fill_result)
        except queue.Full:
            # 成交结果不可丢弃：阻塞等待最多 5 秒
            logger.warning(
                "PendingEntry fill queue full, blocking for %s",
                entry.signal_event.signal_id,
            )
            try:
                self._fill_queue.put(fill_result, timeout=5.0)
            except queue.Full:
                logger.error(
                    "PendingEntry fill queue still full after 5s, fill LOST for %s. "
                    "This indicates fill_worker is stuck or too slow.",
                    entry.signal_event.signal_id,
                )
                # 标记为 expired 并从 _pending 移除，避免永久滞留内存
                with self._lock:
                    self._pending.pop(entry.signal_event.signal_id, None)
                    entry.status = "expired"
                    entry.cancel_reason = "fill_queue_overflow"
                    self._stats["total_expired"] += 1

    def _expire_entry(self, entry: PendingEntry) -> None:
        # ── 超时降级：价格离参考价不远时以当前价市价入场 ──
        fallback_atr = self._config.timeout_fallback_atr
        if fallback_atr > 0 and entry.trade_params.atr_value > 0:
            quote = self._market.get_quote(entry.signal_event.symbol)
            if quote is not None:
                prices = _extract_quote_prices(quote)
                if prices is not None:
                    bid, ask = prices
                    check_price = ask if entry.signal_event.direction == "buy" else bid
                    distance = abs(check_price - entry.reference_price)
                    threshold = entry.trade_params.atr_value * fallback_atr

                    if distance <= threshold:
                        logger.info(
                            "PendingEntry timeout fallback: %s/%s %s @ %.2f "
                            "(ref=%.2f, dist=%.2f < threshold=%.2f)",
                            entry.signal_event.symbol,
                            entry.signal_event.strategy,
                            entry.signal_event.direction,
                            check_price,
                            entry.reference_price,
                            distance,
                            threshold,
                        )
                        self._fill_entry(entry, check_price)
                        return

        # ── 正常超时：丢弃 ──
        with self._lock:
            if self._pending.pop(entry.signal_event.signal_id, None) is None:
                return
            entry.status = "expired"
            entry.cancel_reason = "timeout"
            self._stats["total_expired"] += 1

        logger.info(
            "PendingEntry expired: %s/%s %s best_seen=%.2f zone=[%.2f, %.2f] checks=%d",
            entry.signal_event.symbol,
            entry.signal_event.strategy,
            entry.signal_event.direction,
            entry.best_price_seen or 0.0,
            entry.entry_low,
            entry.entry_high,
            entry.checks_count,
        )

        if self._on_expired_fn:
            try:
                self._on_expired_fn(entry.signal_event.signal_id, "pending_expired")
            except Exception:
                logger.warning(
                    "on_expired_fn callback failed for expired signal %s",
                    entry.signal_event.signal_id,
                    exc_info=True,
                )
