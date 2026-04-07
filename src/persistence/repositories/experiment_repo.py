"""实验追踪仓储：跨 Research/Backtest/PaperTrading 的实验生命周期记录。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.persistence.schema.experiment import (
    ADVANCE_TO_BACKTEST_SQL,
    ADVANCE_TO_PAPER_SQL,
    DDL,
    FETCH_EXPERIMENT_SQL,
    INSERT_EXPERIMENT_SQL,
    LIST_EXPERIMENTS_SQL,
    MARK_ABANDONED_SQL,
    RECORD_BACKTEST_METRICS_SQL,
    RECORD_VALIDATION_SQL,
    UPDATE_STATUS_SQL,
)

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter

logger = logging.getLogger(__name__)

_COLUMNS = [
    "experiment_id",
    "created_at",
    "updated_at",
    "status",
    "symbol",
    "timeframe",
    "mining_run_id",
    "backtest_run_ids",
    "recommendation_id",
    "paper_session_id",
    "backtest_sharpe",
    "backtest_win_rate",
    "paper_sharpe",
    "paper_win_rate",
    "validation_passed",
    "notes",
]


def _row_to_dict(row: tuple) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    for i, col in enumerate(_COLUMNS):
        val = row[i]
        if col in ("created_at", "updated_at") and val is not None:
            d[col] = val.isoformat()
        elif col == "backtest_run_ids" and val is None:
            d[col] = []
        else:
            d[col] = val
    return d


class ExperimentRepository:
    """实验追踪仓储。"""

    def __init__(self, writer: "TimescaleWriter") -> None:
        self._writer = writer

    def ensure_schema(self) -> None:
        try:
            with self._writer.connection() as conn, conn.cursor() as cur:
                cur.execute(DDL)
            logger.info("Experiment schema ensured")
        except Exception:
            logger.warning("Failed to ensure experiment schema", exc_info=True)

    def _execute(self, sql: str, params: tuple) -> None:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)

    def _query(self, sql: str, params: tuple) -> List[tuple]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()  # type: ignore[no-any-return]

    # ── 写入 ─────────────────────────────────────────────────────

    def create(
        self,
        experiment_id: str,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        status: str = "research",
    ) -> None:
        """创建新实验记录。"""
        self._execute(INSERT_EXPERIMENT_SQL, (experiment_id, status, symbol, timeframe))

    def advance_to_backtest(self, experiment_id: str, backtest_run_id: str) -> None:
        """记录回测关联并推进状态。"""
        self._execute(ADVANCE_TO_BACKTEST_SQL, (backtest_run_id, experiment_id))

    def record_backtest_metrics(
        self,
        experiment_id: str,
        sharpe: float,
        win_rate: float,
    ) -> None:
        """记录回测关键指标快照。"""
        self._execute(RECORD_BACKTEST_METRICS_SQL, (sharpe, win_rate, experiment_id))

    def advance_to_paper(
        self,
        experiment_id: str,
        paper_session_id: str,
        recommendation_id: Optional[str] = None,
    ) -> None:
        """记录 Paper Trading 关联并推进状态。"""
        self._execute(
            ADVANCE_TO_PAPER_SQL,
            (paper_session_id, recommendation_id, experiment_id),
        )

    def record_validation(
        self,
        experiment_id: str,
        paper_sharpe: float,
        paper_win_rate: float,
        passed: bool,
    ) -> None:
        """记录 Paper Trading 验证结果。"""
        self._execute(
            RECORD_VALIDATION_SQL,
            (paper_sharpe, paper_win_rate, passed, experiment_id),
        )

    def advance_to_live(self, experiment_id: str) -> None:
        """标记实验进入实盘阶段。"""
        self._execute(UPDATE_STATUS_SQL, ("live", experiment_id))

    def mark_abandoned(self, experiment_id: str) -> None:
        """标记实验为废弃。"""
        self._execute(MARK_ABANDONED_SQL, (experiment_id,))

    # ── 查询 ─────────────────────────────────────────────────────

    def fetch(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """查询单个实验详情。"""
        rows = self._query(FETCH_EXPERIMENT_SQL, (experiment_id,))
        if not rows:
            return None
        return _row_to_dict(rows[0])

    def list_experiments(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """查询实验列表（按 updated_at 倒序）。"""
        where_clause = ""
        params: list = []
        if status is not None:
            where_clause = "WHERE status = %s"
            params.append(status)
        params.extend([limit, offset])

        sql = LIST_EXPERIMENTS_SQL.format(where_clause=where_clause)
        rows = self._query(sql, tuple(params))
        return [_row_to_dict(row) for row in rows]
