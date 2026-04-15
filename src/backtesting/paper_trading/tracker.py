"""Paper Trading 持久化追踪器：将交易记录和 session 信息写入独立数据表。"""

from __future__ import annotations

import logging
import threading
import time as time_mod
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from src.utils.timezone import utc_now

from .models import PaperSession, PaperTradeRecord

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter

logger = logging.getLogger(__name__)


class PaperTradeTracker:
    """Paper Trading 持久化钩子。

    异步刷写：收集交易记录到缓冲区，定时批量写入 DB。
    可作为 bridge.on_trade_closed / on_trade_opened 回调使用。
    """

    def __init__(
        self,
        db_writer: Optional["TimescaleWriter"],
        flush_interval: float = 30.0,
    ) -> None:
        self._db = db_writer
        self._flush_interval = flush_interval
        self._pending_trades: List[PaperTradeRecord] = []
        self._pending_sessions: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._running = False
        self._flush_thread: Optional[threading.Thread] = None
        # session_snapshot_provider: 由 builder 注入；flush 前主动拉取当前 session 最新元数据。
        # 这样 tracker 不需要持有 bridge 引用即可定期持久化运行中的 session。
        self._session_snapshot_provider: Optional[Callable[[], Optional[PaperSession]]] = None
        # open_trades_snapshot_provider: flush 前主动拉取所有 open trades 的最新字段
        # （MFE/MAE/current_sl/bars_held），周期性 upsert 到 DB。
        self._open_trades_snapshot_provider: Optional[
            Callable[[], List[PaperTradeRecord]]
        ] = None

    def set_session_snapshot_provider(
        self, provider: Optional[Callable[[], Optional[PaperSession]]]
    ) -> None:
        """注入一个回调，flush 时主动拉取当前 session 元数据。"""
        self._session_snapshot_provider = provider

    def set_open_trades_snapshot_provider(
        self, provider: Optional[Callable[[], List[PaperTradeRecord]]]
    ) -> None:
        """注入一个回调，flush 时主动拉取所有 open trades 的最新快照。

        用于让 DB 里的 open trade 字段（SL/TP/MFE/MAE 等）随价格变动同步刷新，
        而不是只保留入场时的 snapshot。
        """
        self._open_trades_snapshot_provider = provider

    def start(self) -> None:
        if self._running or self._db is None:
            return
        self._running = True
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            name="paper-trade-flush",
            daemon=True,
        )
        self._flush_thread.start()
        # 立即触发一次 flush：拉取 bridge 的初始 session 元数据入库，
        # 避免 flush_interval（默认 30s）内进程崩溃丢失 session 起始记录。
        self._flush_now()

    def stop(self) -> None:
        self._running = False
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=5.0)
            self._flush_thread = None
        self._flush_now()

    def on_trade_closed(self, trade: PaperTradeRecord) -> None:
        """交易平仓回调：加入待刷写队列。"""
        with self._lock:
            self._pending_trades.append(trade)

    def on_trade_opened(self, trade: PaperTradeRecord) -> None:
        """交易开仓回调：入队等待刷写。

        upsert 由 INSERT_TRADE_SQL 的 ON CONFLICT (trade_id) DO UPDATE 保证：
        first flush 写入 open record（exit_time=NULL），后续 close 再 update 同一行。
        这样进程意外退出也能从 DB 恢复 open positions。
        """
        with self._lock:
            self._pending_trades.append(trade)

    def save_session(self, session: PaperSession) -> None:
        """保存/更新 session 记录。"""
        with self._lock:
            self._pending_sessions.append(session.to_dict())

    def _flush_loop(self) -> None:
        while self._running:
            time_mod.sleep(self._flush_interval)
            self._flush_now()

    def _flush_now(self) -> None:
        """将缓冲区数据写入 DB。"""
        if self._db is None:
            return

        # 主动拉取当前 session 最新元数据。
        # 即使 pending 队列为空，只要 session 还在 active，也会刷一次最新 metrics
        # （balance/total_pnl/total_trades 等），保证 DB 与运行时一致。
        if self._session_snapshot_provider is not None:
            try:
                snapshot = self._session_snapshot_provider()
                if snapshot is not None:
                    with self._lock:
                        self._pending_sessions.append(snapshot.to_dict())
            except Exception:
                logger.debug("paper_trading: session snapshot provider failed", exc_info=True)

        # 主动拉取 open trades 最新字段，配合 upsert 让 DB 里 open position 数据保持 fresh。
        if self._open_trades_snapshot_provider is not None:
            try:
                open_trades = self._open_trades_snapshot_provider()
                if open_trades:
                    with self._lock:
                        self._pending_trades.extend(open_trades)
            except Exception:
                logger.debug("paper_trading: open trades snapshot provider failed", exc_info=True)

        with self._lock:
            trades = list(self._pending_trades)
            sessions = list(self._pending_sessions)
            self._pending_trades.clear()
            self._pending_sessions.clear()

        if not trades and not sessions:
            return

        try:
            repo = self._db.paper_trading_repo
            if sessions:
                for s in sessions:
                    repo.upsert_session(s)
            if trades:
                repo.write_trades(trades)
            logger.debug(
                "paper_trading: flushed %d trades, %d sessions",
                len(trades),
                len(sessions),
            )
        except Exception:
            logger.warning("paper_trading: flush failed", exc_info=True)
            # 放回队列重试
            with self._lock:
                self._pending_trades = trades + self._pending_trades
                self._pending_sessions = sessions + self._pending_sessions
