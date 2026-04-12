"""Research 挖掘结果持久化仓储。"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.persistence.schema.research import (
    DDL,
    FETCH_MINING_RUN_SQL,
    INSERT_MINING_RUN_SQL,
    LIST_MINING_RUNS_SQL,
)

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter
from src.research.core.contracts import MiningResult

logger = logging.getLogger(__name__)


class ResearchRepository:
    """Research 挖掘结果持久化仓储。"""

    def __init__(self, writer: "TimescaleWriter") -> None:
        self._writer = writer

    def ensure_schema(self) -> None:
        try:
            with self._writer.connection() as conn, conn.cursor() as cur:
                cur.execute(DDL)
            logger.info("Research schema ensured")
        except Exception:
            logger.warning("Failed to ensure research schema", exc_info=True)

    def save_mining_result(self, result: "MiningResult") -> None:
        """持久化挖掘结果。"""
        ds = result.data_summary
        row = (
            result.run_id,
            result.experiment_id,
            ds.symbol if ds else "",
            ds.timeframe if ds else "",
            ds.start_time if ds else None,
            ds.end_time if ds else None,
            ds.n_bars if ds else 0,
            "completed" if result.completed_at else "running",
            self._writer._json(ds.to_dict()) if ds else None,
            self._writer._json([f.to_dict() for f in result.top_findings]),
            self._writer._json(result.to_dict()),
        )
        self._writer._batch(INSERT_MINING_RUN_SQL, [row])

    def fetch_mining_result(self, run_id: str) -> Optional[Dict[str, Any]]:
        """查询单次挖掘结果。"""
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(FETCH_MINING_RUN_SQL, (run_id,))
            rows = cur.fetchall()
        if not rows:
            return None
        return self._row_to_dict(rows[0], include_full=True)

    def list_mining_runs(
        self, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """查询挖掘运行列表（按时间倒序）。"""
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(LIST_MINING_RUNS_SQL, (limit, offset))
            rows = cur.fetchall()
        return [self._row_to_dict(row, include_full=False) for row in rows]

    @staticmethod
    def _row_to_dict(row: tuple, *, include_full: bool = False) -> Dict[str, Any]:
        cols = [
            "run_id",
            "experiment_id",
            "created_at",
            "symbol",
            "timeframe",
            "start_time",
            "end_time",
            "n_bars",
            "status",
            "data_summary",
            "top_findings",
        ]
        if include_full:
            cols.append("full_result")

        d: Dict[str, Any] = {}
        for i, col in enumerate(cols):
            if i >= len(row):
                break
            val = row[i]
            if col in ("created_at", "start_time", "end_time") and val is not None:
                d[col] = val.isoformat()
            elif col in ("data_summary", "top_findings", "full_result"):
                d[col] = val  # JSONB → Python dict/list
            else:
                d[col] = val
        return d
