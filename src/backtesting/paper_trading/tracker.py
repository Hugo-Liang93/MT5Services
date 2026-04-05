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
        """交易开仓回调（当前仅日志，可扩展为写入 open positions 表）。"""
        pass

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
