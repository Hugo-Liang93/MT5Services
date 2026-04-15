"""Paper Trading 数据仓储：Session 和交易记录的持久化与查询。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.persistence.schema.paper_trading import (
    DDL,
    INSERT_TRADE_SQL,
    UPSERT_SESSION_SQL,
)

if TYPE_CHECKING:
    from src.backtesting.paper_trading.models import PaperTradeRecord
    from src.persistence.db import TimescaleWriter

logger = logging.getLogger(__name__)


class PaperTradingRepository:
    """Paper Trading 数据仓储。"""

    def __init__(self, writer: "TimescaleWriter") -> None:
        self._writer = writer

    def ensure_schema(self) -> None:
        """确保 Paper Trading 表结构已创建。"""
        try:
            with self._writer.connection() as conn, conn.cursor() as cur:
                cur.execute(DDL)
            logger.info("Paper trading schema ensured")
        except Exception:
            logger.warning("Failed to ensure paper trading schema", exc_info=True)

    def upsert_session(self, session: Dict[str, Any]) -> None:
        """插入或更新 session 记录。"""
        row = (
            session["session_id"],
            session.get("started_at"),
            session.get("stopped_at"),
            session.get("initial_balance", 0.0),
            session.get("final_balance"),
            self._writer._json(session.get("config_snapshot", {})),
            session.get("total_trades", 0),
            session.get("winning_trades", 0),
            session.get("losing_trades", 0),
            session.get("total_pnl", 0.0),
            session.get("max_drawdown_pct", 0.0),
            session.get("sharpe_ratio"),
        )
        self._writer._batch(UPSERT_SESSION_SQL, [row])

    def write_trades(self, trades: List["PaperTradeRecord"]) -> None:
        """批量写入已平仓交易记录。"""
        if not trades:
            return
        rows = []
        for t in trades:
            rows.append((
                t.trade_id,
                t.session_id,
                t.strategy,
                t.direction,
                t.symbol,
                t.timeframe,
                t.entry_time,
                t.entry_price,
                t.exit_time,
                t.exit_price,
                t.stop_loss,
                t.take_profit,
                t.position_size,
                t.confidence,
                t.regime,
                t.signal_id,
                t.pnl,
                t.pnl_pct,
                t.exit_reason,
                t.bars_held,
                t.slippage_cost,
                t.commission_cost,
                t.max_favorable_excursion,
                t.max_adverse_excursion,
                t.breakeven_activated,
                t.trailing_activated,
            ))
        self._writer._batch(INSERT_TRADE_SQL, rows, page_size=200)

    def fetch_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """查询 session 列表（按时间倒序）。"""
        sql = """
        SELECT session_id, started_at, stopped_at, initial_balance, final_balance,
               config_snapshot, total_trades, winning_trades, losing_trades,
               total_pnl, max_drawdown_pct, sharpe_ratio
        FROM paper_trading_sessions
        ORDER BY started_at DESC
        LIMIT %s OFFSET %s
        """
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (limit, offset))
            rows = cur.fetchall()
        return [self._session_row_to_dict(r) for r in rows]

    def fetch_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """查询单个 session。"""
        sql = """
        SELECT session_id, started_at, stopped_at, initial_balance, final_balance,
               config_snapshot, total_trades, winning_trades, losing_trades,
               total_pnl, max_drawdown_pct, sharpe_ratio
        FROM paper_trading_sessions
        WHERE session_id = %s
        """
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (session_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return self._session_row_to_dict(row)

    def fetch_trades(
        self,
        session_id: Optional[str] = None,
        strategy: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """查询交易记录。"""
        conditions = []
        params: list[Any] = []

        if session_id:
            conditions.append("session_id = %s")
            params.append(session_id)
        if strategy:
            conditions.append("strategy = %s")
            params.append(strategy)
        if symbol:
            conditions.append("symbol = %s")
            params.append(symbol)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        sql = f"""
        SELECT trade_id, session_id, strategy, direction, symbol, timeframe,
               entry_time, entry_price, exit_time, exit_price,
               stop_loss, take_profit, position_size, confidence, regime,
               signal_id, pnl, pnl_pct, exit_reason, bars_held,
               slippage_cost, commission_cost,
               max_favorable_excursion, max_adverse_excursion,
               breakeven_activated, trailing_activated
        FROM paper_trade_outcomes
        {where}
        ORDER BY entry_time DESC
        LIMIT %s
        """
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()

        return [self._trade_row_to_dict(r) for r in rows]

    def fetch_open_trades(self, session_id: str) -> List[Dict[str, Any]]:
        """返回指定 session 下所有 exit_time IS NULL 的 open trades。

        用于进程重启后的 PaperPortfolio 状态恢复：
        取回之前持久化的 open position 快照，重建内存 portfolio。
        """
        sql = """
        SELECT trade_id, session_id, strategy, direction, symbol, timeframe,
               entry_time, entry_price, exit_time, exit_price,
               stop_loss, take_profit, position_size, confidence, regime,
               signal_id, pnl, pnl_pct, exit_reason, bars_held,
               slippage_cost, commission_cost,
               max_favorable_excursion, max_adverse_excursion,
               breakeven_activated, trailing_activated
        FROM paper_trade_outcomes
        WHERE session_id = %s AND exit_time IS NULL
        ORDER BY entry_time ASC
        """
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (session_id,))
            rows = cur.fetchall()
        return [self._trade_row_to_dict(r) for r in rows]

    def fetch_latest_active_session(self) -> Optional[Dict[str, Any]]:
        """返回最近一个未正常 stop 的 session（stopped_at IS NULL）。

        进程重启 recovery 的入口点：如果上次进程没干净 stop（崩溃/kill），
        session 表里会留下 stopped_at=NULL 的记录，此时可选择 resume 该 session
        以保持 session_id 连续、open trades 可继续监控。
        """
        sql = """
        SELECT session_id, started_at, stopped_at, initial_balance, final_balance,
               config_snapshot, total_trades, winning_trades, losing_trades,
               total_pnl, max_drawdown_pct, sharpe_ratio
        FROM paper_trading_sessions
        WHERE stopped_at IS NULL
        ORDER BY started_at DESC
        LIMIT 1
        """
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        return self._session_row_to_dict(row) if row else None

    def fetch_performance_summary(
        self, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """查询绩效汇总（按策略/regime 分组）。"""
        where = "WHERE session_id = %s" if session_id else ""
        params: tuple = (session_id,) if session_id else ()

        sql = f"""
        SELECT
            strategy,
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE pnl > 0) as wins,
            COUNT(*) FILTER (WHERE pnl <= 0) as losses,
            SUM(pnl) as total_pnl,
            AVG(confidence) as avg_confidence,
            AVG(pnl) as avg_pnl
        FROM paper_trade_outcomes
        {where}
        GROUP BY strategy
        ORDER BY total_pnl DESC
        """
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        return {
            "by_strategy": [
                {
                    "strategy": r[0],
                    "total": r[1],
                    "wins": r[2],
                    "losses": r[3],
                    "win_rate": round(r[2] / r[1], 4) if r[1] > 0 else 0,
                    "total_pnl": round(float(r[4] or 0), 2),
                    "avg_confidence": round(float(r[5] or 0), 4),
                    "avg_pnl": round(float(r[6] or 0), 2),
                }
                for r in rows
            ]
        }

    @staticmethod
    def _session_row_to_dict(row: tuple) -> Dict[str, Any]:
        return {
            "session_id": row[0],
            "started_at": row[1].isoformat() if isinstance(row[1], datetime) else row[1],
            "stopped_at": row[2].isoformat() if isinstance(row[2], datetime) else row[2],
            "initial_balance": row[3],
            "final_balance": row[4],
            "config_snapshot": row[5],
            "total_trades": row[6],
            "winning_trades": row[7],
            "losing_trades": row[8],
            "total_pnl": round(float(row[9] or 0), 2),
            "max_drawdown_pct": round(float(row[10] or 0), 4),
            "sharpe_ratio": round(float(row[11]), 4) if row[11] is not None else None,
        }

    @staticmethod
    def _trade_row_to_dict(row: tuple) -> Dict[str, Any]:
        return {
            "trade_id": row[0],
            "session_id": row[1],
            "strategy": row[2],
            "direction": row[3],
            "symbol": row[4],
            "timeframe": row[5],
            "entry_time": row[6].isoformat() if isinstance(row[6], datetime) else row[6],
            "entry_price": row[7],
            "exit_time": row[8].isoformat() if isinstance(row[8], datetime) else row[8],
            "exit_price": row[9],
            "stop_loss": row[10],
            "take_profit": row[11],
            "position_size": row[12],
            "confidence": row[13],
            "regime": row[14],
            "signal_id": row[15],
            "pnl": row[16],
            "pnl_pct": row[17],
            "exit_reason": row[18],
            "bars_held": row[19],
            "slippage_cost": row[20],
            "commission_cost": row[21],
            "max_favorable_excursion": row[22],
            "max_adverse_excursion": row[23],
            "breakeven_activated": row[24],
            "trailing_activated": row[25],
        }
