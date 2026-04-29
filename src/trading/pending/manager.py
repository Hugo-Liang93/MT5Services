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

from ..execution.sizing import TradeParameters
from ..ports import PendingOrderCancellationPort
from ..reasons import REASON_NEW_SIGNAL_OVERRIDE, REASON_SHUTDOWN
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
    """一个等待价格确认的挂起入场意图。

    ADR-013 OCO group 支持：
      - entry_key 是 _pending 字典的内部主键（默认 = signal_id；OCO 多 member
        时 = f"{signal_id}#{member_id}"）。同 signal_id 但不同 member_id 的
        entry 各自独立追踪，但属于同一 group_id 受 any_fill 撤 sibling 约束。
      - group_id 单 member 场景仍填入（与 EntrySpecGroup.group_id 一致）。
      - member_id / group_role 用于审计与 fill 逻辑。
    """

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

    # ADR-013 OCO group 字段
    entry_key: str = ""  # _pending key；空则 fallback 到 signal_id
    group_id: str = ""
    member_id: str = ""
    group_role: str = ""  # "limit" / "stop" / "market"

    # 追踪
    best_price_seen: Optional[float] = None
    checks_count: int = 0
    fill_price: Optional[float] = None
    cancel_reason: str = ""

    @property
    def effective_key(self) -> str:
        """获取 _pending dict 的索引 key：entry_key 优先，否则 signal_id。"""
        return self.entry_key or self.signal_event.signal_id


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
    # ADR-013 OCO bookkeeping：fill_worker 完成 execute_fn 后用这两个字段
    # 触发 _on_member_filled(group_id, entry_key) 撤 sibling。
    entry_key: str = ""
    group_id: str = ""


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
        entry_policy_registry: Any = None,
    ):
        self._config = config
        self._market = market_service
        self._cancellation_port = cancellation_port
        self._execute_fn = execute_fn
        self._on_expired_fn = on_expired_fn  # (signal_id, reason) 过期回调
        self._inspect_mt5_order_fn = inspect_mt5_order_fn
        # ADR-013: 由 factories/signals.py 注入；submit_pending_entry 从这里
        # 取 registry 调 derive 拿 EntrySpecGroup。ADR-006 公开属性约定。
        self.entry_policy_registry: Any = entry_policy_registry
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

        # ADR-013 OCO: _pending key 为 entry_key（默认 = signal_id；多 member
        # 时 = f"{signal_id}#{member_id}"）。同 signal_id 但不同 member 的 entry
        # 共享同一 group_id，受 any_fill 撤 sibling 约束。
        self._pending: dict[str, PendingEntry] = {}  # entry_key → PendingEntry
        # MT5 挂单追踪：entry_key → {ticket, expires_at, direction, symbol, ...}
        self._mt5_orders: dict[str, dict[str, Any]] = {}
        # OCO 反向索引：group_id → set[entry_key]
        self._groups: dict[str, set[str]] = {}
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
            fill_result_factory=lambda signal_event, trade_params, cost_metrics, entry_key="", group_id="": _FillResult(
                signal_event=signal_event,
                trade_params=trade_params,
                cost_metrics=cost_metrics,
                entry_key=entry_key,
                group_id=group_id,
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
        # ADR-013: 装配 OCO 反向索引 + any-fill 撤 sibling 回调到 monitoring
        self._monitoring_service.set_groups_index(
            self._groups,
            on_member_filled=self._on_member_filled_handler,
        )

    def _on_member_filled_handler(self, group_id: str, entry_key: str) -> None:
        """ADR-013 any-fill 触发链：fill_worker 调用此方法撤 sibling。

        失败重试 3 次（broker 拒绝 cancel 时）。最终失败标记 group 为
        pending_cancellation 状态 + WARN，但不阻塞返回（fill 已成功）。
        """
        for attempt in range(3):
            try:
                cancelled = self.cancel_by_group(
                    group_id,
                    reason="oco_sibling_filled",
                    exclude_key=entry_key,
                )
                logger.info(
                    "OCO sibling cancelled: group=%s filled_key=%s cancelled=%d",
                    group_id,
                    entry_key,
                    cancelled,
                )
                # 同时撤 MT5 挂单（broker 侧）
                try:
                    self._cancellation_port.cancel_orders_by_group(group_id)
                except AttributeError:
                    # cancellation_port 未实现 cancel_orders_by_group →
                    # P4 fallback：用 cancel_mt5_orders_by_symbol 兜底（粗粒度）
                    pass
                return
            except Exception as exc:  # ADR-013: any-fill 撤 sibling 重试 3 次
                logger.warning(
                    "OCO sibling cancellation attempt %d/3 failed for group %s: %s",
                    attempt + 1,
                    group_id,
                    exc,
                )
        logger.error(
            "OCO sibling cancellation FAILED after 3 retries for group %s; "
            "manual intervention required",
            group_id,
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
        """提交一个新的挂起入场（ADR-013 OCO: 内部 key 用 effective_key）。

        §0z P2：旧实现 ``self._pending[sig] = entry`` 直接覆盖 → 老 entry 与
        其已注册的 MT5 ticket（PendingOrderLifecycleManager）失联。重复
        entry_key 必须先 cancel 旧项以触发 ``on_expired_fn`` 让上游清理 MT5
        跟踪，再接受新项；这样同 key 的 lifecycle 始终单一。

        OCO 多 member 场景下，每个 member 都有独立 entry_key，互不冲突。
        group_id 在 _groups 反向索引中维护，供 any-fill 撤 sibling 用。
        """
        # 默认 entry_key 等于 signal_id（向后兼容）
        if not entry.entry_key:
            entry.entry_key = entry.signal_event.signal_id
        key = entry.entry_key

        if key in self._pending:
            logger.warning(
                "PendingEntry submit: entry_key=%s already pending, replacing "
                "previous entry (cleaning up MT5 tracking via cancel callback)",
                key,
            )
            self.cancel(key, reason="replaced_by_new_submit")
        with self._lock:
            self._pending[key] = entry
            self._stats["total_submitted"] += 1
            if entry.group_id:
                self._groups.setdefault(entry.group_id, set()).add(key)
        logger.info(
            "PendingEntry submitted: %s/%s %s key=%s zone=[%.2f, %.2f] mode=%s "
            "group=%s/%s expires=%s",
            entry.signal_event.symbol,
            entry.signal_event.strategy,
            entry.signal_event.direction,
            key,
            entry.entry_low,
            entry.entry_high,
            entry.zone_mode,
            entry.group_id or "-",
            entry.member_id or "-",
            entry.expires_at.strftime("%H:%M:%S"),
        )

    def submit_oco_group(
        self,
        members: list[PendingEntry],
        *,
        group_id: str,
    ) -> None:
        """ADR-013: 提交多 member OCO group。

        每个 member 必须已设置 entry_key / group_id / member_id / group_role。
        member 之间的 entry_key 必须唯一。group_id 在 _groups 反向索引中维护。
        提交失败时不回滚已成功的 member（OCO 半成功状态由 any-fill 自然清理）。
        """
        if not members:
            raise ValueError("OCO group must have at least 1 member")
        keys = [m.entry_key or m.signal_event.signal_id for m in members]
        if len(set(keys)) != len(keys):
            raise ValueError(f"OCO group has duplicate entry_keys: {keys}")
        for m in members:
            if not m.entry_key:
                m.entry_key = m.signal_event.signal_id
            if not m.group_id:
                m.group_id = group_id
            self.submit(m)

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

    def cancel(self, key: str, reason: str = "manual") -> bool:
        """取消指定 entry_key 的挂起入场（ADR-013: key 可为 entry_key 或 signal_id）。

        OCO 多 member 场景下：传 signal_id 时只撤匹配该 signal_id 的首 entry
        （向后兼容旧调用）。要整组撤请用 cancel_by_group(group_id)。
        """
        with self._lock:
            entry = self._pending.pop(key, None)
            if entry is None:
                # 向后兼容：传入 signal_id 时尝试撤所有匹配该 signal 的 entries
                # （旧调用接口）；返回 True 表示至少撤了一个。
                signal_keys = [
                    k
                    for k, e in self._pending.items()
                    if e.signal_event.signal_id == key
                ]
                if not signal_keys:
                    return False
                entry = self._pending.pop(signal_keys[0])
            entry.status = "cancelled"
            entry.cancel_reason = reason
            self._stats["total_cancelled"] += 1
            # 从 _groups 反向索引移除
            if entry.group_id and entry.group_id in self._groups:
                self._groups[entry.group_id].discard(entry.entry_key)
                if not self._groups[entry.group_id]:
                    self._groups.pop(entry.group_id, None)
        logger.info(
            "PendingEntry cancelled: %s/%s %s key=%s reason=%s",
            entry.signal_event.symbol,
            entry.signal_event.strategy,
            entry.signal_event.direction,
            entry.entry_key,
            reason,
        )
        if self._on_expired_fn:
            try:
                self._on_expired_fn(
                    entry.signal_event.signal_id, f"pending_cancelled:{reason}"
                )
            except Exception:
                logger.warning(
                    "on_expired_fn callback failed for cancelled key %s",
                    key,
                    exc_info=True,
                )
        return True

    def cancel_by_group(
        self,
        group_id: str,
        reason: str = "oco_sibling_filled",
        *,
        exclude_key: Optional[str] = None,
    ) -> int:
        """ADR-013: 撤销 OCO group 中的所有 member（exclude_key 不撤）。

        any-fill 触发链：fill_worker → _on_member_filled → cancel_by_group
        （exclude_key=已 fill 的 member）。返回实际撤掉的 member 数。
        """
        with self._lock:
            members = list(self._groups.get(group_id, set()))
        cancelled = 0
        for member_key in members:
            if exclude_key is not None and member_key == exclude_key:
                continue
            if self.cancel(member_key, reason=reason):
                cancelled += 1
        return cancelled

    def cancel_by_symbol(
        self,
        symbol: str,
        reason: str = REASON_NEW_SIGNAL_OVERRIDE,
        *,
        exclude_direction: Optional[str] = None,
    ) -> int:
        """取消指定品种的所有挂起入场（ADR-013: OCO group 整组撤）。

        Args:
            exclude_direction: 不取消该方向的 pending（用于 cancel_same_direction=false）

        OCO 多 member 场景：任一 member 命中匹配条件即整组撤（不论其他 member
        方向是否 exclude）——OCO 是「任一成交撤其余」语义，反向信号到来时整组
        都失效。
        """
        cancelled_entries: list[tuple[str, PendingEntry]] = []
        with self._lock:
            # 第一步：找到所有需要撤的 entry_keys（含 group 整组扩展）
            primary_keys = [
                key
                for key, entry in self._pending.items()
                if entry.signal_event.symbol == symbol
                and entry.status == "pending"
                and (
                    exclude_direction is None
                    or entry.signal_event.direction != exclude_direction
                )
            ]
            # OCO group 扩展：primary_keys 中任一属于某 group → 该 group 全部撤
            keys_to_cancel: set[str] = set(primary_keys)
            for key in list(primary_keys):
                entry = self._pending.get(key)
                if entry and entry.group_id and entry.group_id in self._groups:
                    keys_to_cancel.update(self._groups[entry.group_id])
            for key in keys_to_cancel:
                entry = self._pending.pop(key, None)
                if entry is None:
                    continue
                entry.status = "cancelled"
                entry.cancel_reason = reason
                self._stats["total_cancelled"] += 1
                if entry.group_id and entry.group_id in self._groups:
                    self._groups[entry.group_id].discard(key)
                    if not self._groups[entry.group_id]:
                        self._groups.pop(entry.group_id, None)
                cancelled_entries.append((entry.signal_event.signal_id, entry))
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
        # ADR-013 OCO group 字段
        order_group_id: Optional[str] = None,
        group_member_id: Optional[str] = None,
        group_role: Optional[str] = None,
    ) -> None:
        """注册 MT5 挂单，由 monitor loop 负责超时取消。

        ADR-013: 如果挂单属于一个 EntrySpecGroup，需要传 order_group_id /
        group_member_id / group_role。单 member 场景同样需要（P3 起 group_id
        始终非空）。group 信息通过 metadata 透传给 lifecycle_manager / DB。
        """
        # 把 group 信息合并到 metadata（生命周期管理器只接受 metadata dict）
        merged_metadata = dict(metadata or {})
        if order_group_id is not None:
            merged_metadata["order_group_id"] = order_group_id
        if group_member_id is not None:
            merged_metadata["group_member_id"] = group_member_id
        if group_role is not None:
            merged_metadata["group_role"] = group_role

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
            metadata=merged_metadata,
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
            self._monitor_lifecycle.is_running() and self._fill_lifecycle.is_running()
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
