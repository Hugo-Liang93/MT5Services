"""策略相关性分析结果持久化仓储（P11 Phase 3）。

对应表：`backtest_correlation_analyses`。每次 POST 触发的 correlation
计算结果都会生成一条新记录，前端可用 `analysis_id` 精确回查，或用
`backtest_run_id` 查该回测的最新一次分析。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.persistence.schema.correlation import (
    DDL,
    FETCH_BY_ID_SQL,
    FETCH_LATEST_BY_RUN_SQL,
    INSERT_SQL,
    LIST_BY_RUN_SQL,
)

if TYPE_CHECKING:
    from src.backtesting.analysis.correlation import CorrelationAnalysis
    from src.persistence.db import TimescaleWriter

logger = logging.getLogger(__name__)


class CorrelationAnalysisRepository:
    """策略相关性分析仓储。

    共享 TimescaleWriter 连接池（ADR-006），禁止独立构造 pg 连接。
    """

    def __init__(self, writer: "TimescaleWriter") -> None:
        self._writer = writer

    @property
    def writer(self) -> "TimescaleWriter":
        return self._writer

    def ensure_schema(self) -> None:
        try:
            with self._writer.connection() as conn, conn.cursor() as cur:
                cur.execute(DDL)
            logger.info("Correlation analysis schema ensured")
        except Exception:
            logger.warning("Failed to ensure correlation schema", exc_info=True)

    # ────────────────────── Write ──────────────────────

    @staticmethod
    def generate_analysis_id() -> str:
        """生成 analysis_id（UUID12）。对齐 rec_id / run_id 命名风格。"""
        return f"corr_{uuid.uuid4().hex[:12]}"

    def save(
        self,
        analysis: "CorrelationAnalysis",
        *,
        backtest_run_id: str,
        penalty_weight: float,
        analysis_id: Optional[str] = None,
    ) -> str:
        """落库一次相关性分析结果。

        Args:
            analysis: CorrelationAnalysis 对象
            backtest_run_id: 关联的回测 run_id（必填）
            penalty_weight: 调用参数快照
            analysis_id: 可选预生成 ID；未传则自动生成

        Returns:
            落库后的 analysis_id
        """
        aid = analysis_id or self.generate_analysis_id()
        pairs_serialized = [
            {
                "strategy_a": p.strategy_a,
                "strategy_b": p.strategy_b,
                "correlation": float(p.correlation),
                "overlap_count": int(p.overlap_count),
                "agreement_rate": float(p.agreement_rate),
            }
            for p in analysis.pairs
        ]
        high_pairs_serialized = [
            {
                "strategy_a": p.strategy_a,
                "strategy_b": p.strategy_b,
                "correlation": float(p.correlation),
                "overlap_count": int(p.overlap_count),
                "agreement_rate": float(p.agreement_rate),
            }
            for p in analysis.high_correlation_pairs
        ]
        strategies_analyzed = len(analysis.strategy_weights)

        summary = {
            "all_pairs_count": len(analysis.pairs),
            "strategies": sorted(analysis.strategy_weights.keys()),
        }

        row = (
            aid,
            backtest_run_id,
            datetime.now(timezone.utc),
            float(analysis.correlation_threshold),
            float(penalty_weight),
            int(analysis.total_bars_analyzed),
            strategies_analyzed,
            len(high_pairs_serialized),
            self._writer._json(pairs_serialized),
            self._writer._json(high_pairs_serialized),
            self._writer._json(dict(analysis.strategy_weights)),
            self._writer._json(summary),
        )

        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(INSERT_SQL, row)

        logger.info(
            "Correlation analysis %s persisted (run=%s, pairs=%d, high=%d)",
            aid,
            backtest_run_id,
            len(pairs_serialized),
            len(high_pairs_serialized),
        )
        return aid

    # ────────────────────── Read ──────────────────────

    def fetch(self, analysis_id: str) -> Optional[Dict[str, Any]]:
        """按 analysis_id 查询。"""
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(FETCH_BY_ID_SQL, (analysis_id,))
            rows = cur.fetchall()
        return self._row_to_dict(rows[0]) if rows else None

    def fetch_latest_for_run(self, backtest_run_id: str) -> Optional[Dict[str, Any]]:
        """按 backtest_run_id 查最新一次分析。"""
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(FETCH_LATEST_BY_RUN_SQL, (backtest_run_id,))
            rows = cur.fetchall()
        return self._row_to_dict(rows[0]) if rows else None

    def list_for_run(
        self,
        backtest_run_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """列出某 run 的所有 correlation 分析（按 created_at 倒序）。"""
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(LIST_BY_RUN_SQL, (backtest_run_id, limit, offset))
            rows = cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ────────────────────── Helpers ──────────────────────

    @staticmethod
    def _row_to_dict(row: tuple) -> Dict[str, Any]:
        return {
            "analysis_id": row[0],
            "backtest_run_id": row[1],
            "created_at": (
                row[2].isoformat() if isinstance(row[2], datetime) else row[2]
            ),
            "correlation_threshold": (float(row[3]) if row[3] is not None else None),
            "penalty_weight": float(row[4]) if row[4] is not None else None,
            "total_bars_analyzed": int(row[5]) if row[5] is not None else 0,
            "strategies_analyzed": int(row[6]) if row[6] is not None else 0,
            "high_correlation_count": (int(row[7]) if row[7] is not None else 0),
            "pairs": row[8] if isinstance(row[8], list) else [],
            "high_correlation_pairs": (row[9] if isinstance(row[9], list) else []),
            "strategy_weights": row[10] if isinstance(row[10], dict) else {},
            "summary": row[11] if isinstance(row[11], dict) else {},
        }
