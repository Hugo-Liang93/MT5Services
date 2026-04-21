"""Walk-Forward 结果持久化仓储（P11 Phase 2）。

写入两张表：
- `backtest_walk_forward_runs`：任务级记录（聚合指标 + 状态）
- `backtest_walk_forward_windows`：每个 split 的 IS/OOS 分段结果

落库策略：`save(wf_result, wf_run_id, backtest_run_id)` 在单次事务中写 runs + windows，
确保窗口结果不会孤立存在而没有对应的 run 主记录。
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.persistence.schema.walk_forward import (
    DDL,
    FETCH_LATEST_BY_BACKTEST_RUN_SQL,
    FETCH_RUN_SQL,
    FETCH_WINDOWS_SQL,
    INSERT_RUN_SQL,
    INSERT_WINDOW_SQL,
    LIST_RUNS_SQL,
)

if TYPE_CHECKING:
    from src.backtesting.optimization.walk_forward import WalkForwardResult
    from src.persistence.db import TimescaleWriter

logger = logging.getLogger(__name__)


class WalkForwardRepository:
    """Walk-Forward 结果持久化仓储。

    依赖 `TimescaleWriter` 共享连接池（ADR-006），禁止独立构造 pg 连接。
    """

    def __init__(self, writer: "TimescaleWriter") -> None:
        self._writer = writer

    @property
    def writer(self) -> "TimescaleWriter":
        """公开 writer 端口（ADR-006 规范）。"""
        return self._writer

    def ensure_schema(self) -> None:
        """确保 walk_forward 相关表结构已创建。"""
        try:
            with self._writer.connection() as conn, conn.cursor() as cur:
                cur.execute(DDL)
            logger.info("Walk-Forward schema ensured")
        except Exception:
            logger.warning("Failed to ensure walk_forward schema", exc_info=True)

    # ────────────────────── Write ──────────────────────

    def save(
        self,
        result: "WalkForwardResult",
        *,
        wf_run_id: str,
        backtest_run_id: Optional[str] = None,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        experiment_id: Optional[str] = None,
    ) -> None:
        """事务性落库：runs 记录 + 所有 windows 同步写入。

        Args:
            result: WalkForwardResult 对象（含 splits / aggregate_metrics 等）
            wf_run_id: WF 任务 ID（= execute_walk_forward 的 run_id）
            backtest_run_id: 可选 —— 关联的 backtest_runs.run_id（独立 WF 时为空）
            started_at / completed_at: 任务生命周期时间戳
            experiment_id: 实验追踪 ID
        """
        started = started_at or datetime.now(timezone.utc)
        completed = completed_at or datetime.now(timezone.utc)
        duration_ms = int((completed - started).total_seconds() * 1000)

        config = result.config
        config_dict: Dict[str, Any] = {
            "total_start_time": config.total_start_time.isoformat(),
            "total_end_time": config.total_end_time.isoformat(),
            "train_ratio": config.train_ratio,
            "n_splits": config.n_splits,
            "anchored": config.anchored,
            "optimization_metric": config.optimization_metric,
            "symbol": config.base_config.symbol,
            "timeframe": config.base_config.timeframe,
        }

        aggregate_dict = asdict(result.aggregate_metrics)

        overfitting = self._safe_finite(result.overfitting_ratio)

        run_row = (
            wf_run_id,
            backtest_run_id,
            started,
            completed,
            self._writer._json(config_dict),
            self._writer._json(aggregate_dict),
            overfitting,
            float(result.consistency_rate),
            float(aggregate_dict.get("sharpe_ratio", 0.0) or 0.0),
            float(aggregate_dict.get("win_rate", 0.0) or 0.0),
            int(aggregate_dict.get("total_trades", 0) or 0),
            float(aggregate_dict.get("total_pnl", 0.0) or 0.0),
            len(result.splits),
            "completed",
            duration_ms,
            None,  # error
            experiment_id,
        )

        window_rows = [self._build_window_row(wf_run_id, s) for s in result.splits]

        with self._writer.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(INSERT_RUN_SQL, run_row)
                for row in window_rows:
                    cur.execute(INSERT_WINDOW_SQL, row)

        logger.info(
            "Walk-Forward %s persisted: %d windows, overfitting_ratio=%s",
            wf_run_id,
            len(window_rows),
            overfitting,
        )

    def save_failed(
        self,
        *,
        wf_run_id: str,
        error: str,
        backtest_run_id: Optional[str] = None,
        experiment_id: Optional[str] = None,
    ) -> None:
        """WF 失败时记录失败快照（方便事后回查）。"""
        now = datetime.now(timezone.utc)
        run_row = (
            wf_run_id,
            backtest_run_id,
            now,
            now,
            self._writer._json({}),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            0,
            "failed",
            0,
            error[:500] if error else None,
            experiment_id,
        )
        try:
            with self._writer.connection() as conn, conn.cursor() as cur:
                cur.execute(INSERT_RUN_SQL, run_row)
        except Exception:
            logger.warning(
                "Failed to persist failed walk-forward %s", wf_run_id, exc_info=True
            )

    # ────────────────────── Read ──────────────────────

    def fetch(self, wf_run_id: str) -> Optional[Dict[str, Any]]:
        """查询单次 WF 任务（含 windows）。

        Returns:
            None 若 wf_run_id 不存在；否则 {run: {...}, windows: [...]}
        """
        run = self._fetch_run_row(wf_run_id)
        if run is None:
            return None
        windows = self._fetch_window_rows(wf_run_id)
        return {"run": run, "windows": windows}

    def fetch_latest_by_backtest_run(
        self, backtest_run_id: str
    ) -> Optional[Dict[str, Any]]:
        """按原始 backtest_run_id 查最新一次 WF 任务。"""
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(FETCH_LATEST_BY_BACKTEST_RUN_SQL, (backtest_run_id,))
            rows = cur.fetchall()
        if not rows:
            return None
        run = self._row_to_run_dict(rows[0])
        windows = self._fetch_window_rows(run["wf_run_id"])
        return {"run": run, "windows": windows}

    def list_runs(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """列表查询（分页，按 created_at 倒序）。"""
        if status:
            where_clause = "WHERE status = %s"
            params: tuple = (status, limit, offset)
        else:
            where_clause = ""
            params = (limit, offset)
        sql = LIST_RUNS_SQL.format(where_clause=where_clause)
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_list_dict(r) for r in rows]

    # ────────────────────── Internal helpers ──────────────────────

    def _fetch_run_row(self, wf_run_id: str) -> Optional[Dict[str, Any]]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(FETCH_RUN_SQL, (wf_run_id,))
            rows = cur.fetchall()
        return self._row_to_run_dict(rows[0]) if rows else None

    def _fetch_window_rows(self, wf_run_id: str) -> List[Dict[str, Any]]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(FETCH_WINDOWS_SQL, (wf_run_id,))
            rows = cur.fetchall()
        return [self._row_to_window_dict(r) for r in rows]

    def _build_window_row(self, wf_run_id: str, split: Any) -> tuple:
        """从 WalkForwardSplit 构建 INSERT 行。"""
        is_metrics = asdict(split.in_sample_result.metrics)
        oos_metrics = asdict(split.out_of_sample_result.metrics)
        window_label = (
            f"{split.train_start.date().isoformat()}→"
            f"{split.test_end.date().isoformat()}"
        )
        return (
            wf_run_id,
            split.split_index,
            window_label,
            split.train_start,
            split.train_end,
            split.test_start,
            split.test_end,
            self._writer._json(split.best_params),
            self._writer._json(is_metrics),
            self._writer._json(oos_metrics),
            float(is_metrics.get("total_pnl", 0.0) or 0.0),
            float(oos_metrics.get("total_pnl", 0.0) or 0.0),
            float(is_metrics.get("sharpe_ratio", 0.0) or 0.0),
            float(oos_metrics.get("sharpe_ratio", 0.0) or 0.0),
            float(is_metrics.get("win_rate", 0.0) or 0.0),
            float(oos_metrics.get("win_rate", 0.0) or 0.0),
            float(oos_metrics.get("max_drawdown", 0.0) or 0.0),
            int(oos_metrics.get("total_trades", 0) or 0),
        )

    @staticmethod
    def _row_to_run_dict(row: tuple) -> Dict[str, Any]:
        return {
            "wf_run_id": row[0],
            "backtest_run_id": row[1],
            "created_at": (
                row[2].isoformat() if isinstance(row[2], datetime) else row[2]
            ),
            "completed_at": (
                row[3].isoformat() if isinstance(row[3], datetime) else row[3]
            ),
            "config": row[4],
            "aggregate_metrics": row[5],
            "overfitting_ratio": float(row[6]) if row[6] is not None else None,
            "consistency_rate": float(row[7]) if row[7] is not None else None,
            "oos_sharpe": float(row[8]) if row[8] is not None else None,
            "oos_win_rate": float(row[9]) if row[9] is not None else None,
            "oos_total_trades": int(row[10]) if row[10] is not None else None,
            "oos_total_pnl": float(row[11]) if row[11] is not None else None,
            "n_splits": int(row[12]) if row[12] is not None else 0,
            "status": row[13],
            "duration_ms": int(row[14]) if row[14] is not None else None,
            "error": row[15],
            "experiment_id": row[16],
        }

    @staticmethod
    def _row_to_list_dict(row: tuple) -> Dict[str, Any]:
        """list_runs 返回的瘦身结构。"""
        return {
            "wf_run_id": row[0],
            "backtest_run_id": row[1],
            "created_at": (
                row[2].isoformat() if isinstance(row[2], datetime) else row[2]
            ),
            "completed_at": (
                row[3].isoformat() if isinstance(row[3], datetime) else row[3]
            ),
            "overfitting_ratio": float(row[4]) if row[4] is not None else None,
            "consistency_rate": float(row[5]) if row[5] is not None else None,
            "oos_sharpe": float(row[6]) if row[6] is not None else None,
            "oos_win_rate": float(row[7]) if row[7] is not None else None,
            "oos_total_trades": int(row[8]) if row[8] is not None else None,
            "oos_total_pnl": float(row[9]) if row[9] is not None else None,
            "n_splits": int(row[10]) if row[10] is not None else 0,
            "status": row[11],
            "duration_ms": int(row[12]) if row[12] is not None else None,
            "experiment_id": row[13],
        }

    @staticmethod
    def _row_to_window_dict(row: tuple) -> Dict[str, Any]:
        return {
            "split_index": int(row[0]),
            "window_label": row[1],
            "train_start": (
                row[2].isoformat() if isinstance(row[2], datetime) else row[2]
            ),
            "train_end": row[3].isoformat() if isinstance(row[3], datetime) else row[3],
            "test_start": (
                row[4].isoformat() if isinstance(row[4], datetime) else row[4]
            ),
            "test_end": row[5].isoformat() if isinstance(row[5], datetime) else row[5],
            "best_params": row[6],
            "is_metrics": row[7],
            "oos_metrics": row[8],
            "is_pnl": float(row[9]) if row[9] is not None else None,
            "oos_pnl": float(row[10]) if row[10] is not None else None,
            "is_sharpe": float(row[11]) if row[11] is not None else None,
            "oos_sharpe": float(row[12]) if row[12] is not None else None,
            "is_win_rate": float(row[13]) if row[13] is not None else None,
            "oos_win_rate": float(row[14]) if row[14] is not None else None,
            "oos_max_drawdown": float(row[15]) if row[15] is not None else None,
            "oos_trade_count": int(row[16]) if row[16] is not None else None,
        }

    @staticmethod
    def _safe_finite(value: Any) -> Optional[float]:
        """`float('inf')` 不能写入 NUMERIC 列，转为 None（前端识别为"严重过拟合"）。"""
        if value is None:
            return None
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v
