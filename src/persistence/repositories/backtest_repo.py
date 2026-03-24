"""回测数据仓储：回测运行、交易、信号评估和参数推荐记录的持久化。

将回测结果落表到 TimescaleDB，对应四张表：
- backtest_runs: 回测运行摘要
- backtest_trades: 回测交易明细
- backtest_signal_evaluations: 信号评估明细（含过滤统计）
- backtest_recommendations: 参数推荐记录
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.persistence.schema.backtest import (
    DDL,
    INSERT_EVALUATION_SQL,
    INSERT_RUN_SQL,
    INSERT_TRADE_SQL,
)
from src.persistence.schema.recommendation import DDL as RECOMMENDATION_DDL
from src.persistence.schema.recommendation import (
    FETCH_RECOMMENDATION_SQL,
    INSERT_RECOMMENDATION_SQL,
    LIST_RECOMMENDATIONS_SQL,
    UPDATE_RECOMMENDATION_STATUS_SQL,
)

if TYPE_CHECKING:
    from src.backtesting.models import (
        BacktestResult,
        Recommendation,
        SignalEvaluation,
        TradeRecord,
    )
    from src.persistence.db import TimescaleWriter

logger = logging.getLogger(__name__)


class BacktestRepository:
    """回测结果持久化仓储。"""

    def __init__(self, writer: "TimescaleWriter") -> None:
        self._writer = writer

    def ensure_schema(self) -> None:
        """确保回测相关表结构已创建（含推荐表）。"""
        try:
            with self._writer.connection() as conn, conn.cursor() as cur:
                cur.execute(DDL)
                cur.execute(RECOMMENDATION_DDL)
            logger.info("Backtest schema ensured (incl. recommendations)")
        except Exception:
            logger.warning("Failed to ensure backtest schema", exc_info=True)

    def save_result(self, result: "BacktestResult") -> None:
        """持久化完整的回测结果（运行记录 + 交易明细 + 信号评估）。"""
        self._save_run(result)
        self._save_trades(result.run_id, result.trades)
        if result.signal_evaluations:
            self._save_evaluations(result.run_id, result.signal_evaluations)
        logger.info(
            "Backtest %s persisted: %d trades, %d evaluations",
            result.run_id,
            len(result.trades),
            len(result.signal_evaluations or []),
        )

    def _save_run(self, result: "BacktestResult") -> None:
        """保存回测运行摘要到 backtest_runs。"""
        config_dict = asdict(result.config)
        config_dict["start_time"] = result.config.start_time.isoformat()
        config_dict["end_time"] = result.config.end_time.isoformat()

        metrics_dict = asdict(result.metrics)
        metrics_by_regime = {k: asdict(v) for k, v in result.metrics_by_regime.items()}
        metrics_by_strategy = {
            k: asdict(v) for k, v in result.metrics_by_strategy.items()
        }

        equity_curve_ser = [(t.isoformat(), v) for t, v in result.equity_curve]

        duration_ms = int(
            (result.completed_at - result.started_at).total_seconds() * 1000
        )

        row = (
            result.run_id,
            result.started_at,
            self._writer._json(config_dict),
            self._writer._json(result.param_set),
            self._writer._json(metrics_dict),
            self._writer._json(metrics_by_regime),
            self._writer._json(metrics_by_strategy),
            self._writer._json(equity_curve_ser),
            "completed",
            duration_ms,
            self._writer._json(result.filter_stats),
        )
        self._writer._batch(INSERT_RUN_SQL, [row])

    def _save_trades(self, run_id: str, trades: List["TradeRecord"]) -> None:
        """批量保存交易记录到 backtest_trades。"""
        if not trades:
            return
        rows = []
        for t in trades:
            rows.append(
                (
                    run_id,
                    t.strategy,
                    t.action,
                    t.entry_time,
                    t.entry_price,
                    t.exit_time,
                    t.exit_price,
                    t.stop_loss,
                    t.take_profit,
                    t.position_size,
                    t.pnl,
                    t.pnl_pct,
                    t.bars_held,
                    t.regime,
                    t.confidence,
                    t.exit_reason,
                    getattr(t, "slippage_cost", 0.0),
                    getattr(t, "commission_cost", 0.0),
                )
            )
        self._writer._batch(INSERT_TRADE_SQL, rows, page_size=200)

    def _save_evaluations(
        self, run_id: str, evaluations: List["SignalEvaluation"]
    ) -> None:
        """批量保存信号评估记录到 backtest_signal_evaluations。"""
        if not evaluations:
            return
        rows = []
        for ev in evaluations:
            rows.append(
                (
                    run_id,
                    ev.bar_time,
                    ev.strategy,
                    ev.action,
                    ev.confidence,
                    ev.regime,
                    ev.price_at_signal,
                    ev.price_after_n_bars,
                    ev.bars_to_evaluate,
                    ev.won,
                    ev.pnl_pct,
                    ev.filtered,
                    ev.filter_reason or None,
                    getattr(ev, "incomplete", False),
                )
            )
        self._writer._batch(INSERT_EVALUATION_SQL, rows, page_size=500)

    def _query(self, sql: str, params: tuple) -> list:
        """执行查询并返回所有行。"""
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetch_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """查询单次回测运行结果。"""
        sql = """
        SELECT run_id, created_at, config, param_set, metrics,
               metrics_by_regime, metrics_by_strategy, equity_curve,
               status, duration_ms, filter_stats
        FROM backtest_runs WHERE run_id = %s
        """
        rows = self._query(sql, (run_id,))
        if not rows:
            return None
        return self._run_row_to_dict(rows[0])

    def fetch_runs(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """查询回测运行列表（按时间倒序）。"""
        sql = """
        SELECT run_id, created_at, config, param_set, metrics,
               metrics_by_regime, metrics_by_strategy, equity_curve,
               status, duration_ms, filter_stats
        FROM backtest_runs
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
        """
        rows = self._query(sql, (limit, offset))
        return [self._run_row_to_dict(r) for r in rows]

    def fetch_trades(self, run_id: str) -> List[Dict[str, Any]]:
        """查询某次回测的交易明细。"""
        sql = """
        SELECT id, run_id, strategy, action, entry_time, entry_price,
               exit_time, exit_price, stop_loss, take_profit,
               position_size, pnl, pnl_pct, bars_held, regime,
               confidence, exit_reason
        FROM backtest_trades
        WHERE run_id = %s
        ORDER BY entry_time
        """
        rows = self._query(sql, (run_id,))
        result = []
        for r in rows:
            result.append(
                {
                    "id": r[0],
                    "run_id": r[1],
                    "strategy": r[2],
                    "action": r[3],
                    "entry_time": (
                        r[4].isoformat() if isinstance(r[4], datetime) else r[4]
                    ),
                    "entry_price": r[5],
                    "exit_time": (
                        r[6].isoformat() if isinstance(r[6], datetime) else r[6]
                    ),
                    "exit_price": r[7],
                    "stop_loss": r[8],
                    "take_profit": r[9],
                    "position_size": r[10],
                    "pnl": r[11],
                    "pnl_pct": r[12],
                    "bars_held": r[13],
                    "regime": r[14],
                    "confidence": r[15],
                    "exit_reason": r[16],
                }
            )
        return result

    def fetch_evaluations(
        self,
        run_id: str,
        strategy: Optional[str] = None,
        filtered_only: bool = False,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """查询信号评估明细。"""
        conditions = ["run_id = %s"]
        params: list[Any] = [run_id]

        if strategy:
            conditions.append("strategy = %s")
            params.append(strategy)
        if filtered_only:
            conditions.append("filtered = TRUE")

        params.append(limit)

        sql = f"""
        SELECT id, run_id, bar_time, strategy, action, confidence,
               regime, price_at_signal, price_after_n_bars,
               bars_to_evaluate, won, pnl_pct, filtered, filter_reason
        FROM backtest_signal_evaluations
        WHERE {' AND '.join(conditions)}
        ORDER BY bar_time
        LIMIT %s
        """
        rows = self._query(sql, tuple(params))
        result = []
        for r in rows:
            result.append(
                {
                    "id": r[0],
                    "run_id": r[1],
                    "bar_time": (
                        r[2].isoformat() if isinstance(r[2], datetime) else r[2]
                    ),
                    "strategy": r[3],
                    "action": r[4],
                    "confidence": r[5],
                    "regime": r[6],
                    "price_at_signal": r[7],
                    "price_after_n_bars": r[8],
                    "bars_to_evaluate": r[9],
                    "won": r[10],
                    "pnl_pct": r[11],
                    "filtered": r[12],
                    "filter_reason": r[13],
                }
            )
        return result

    def fetch_evaluation_summary(self, run_id: str) -> Dict[str, Any]:
        """查询信号评估汇总统计。"""
        sql = """
        SELECT
            strategy,
            action,
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE filtered) as filtered_count,
            COUNT(*) FILTER (WHERE won = TRUE) as won_count,
            COUNT(*) FILTER (WHERE won = FALSE) as lost_count,
            AVG(confidence) FILTER (WHERE NOT filtered) as avg_confidence,
            AVG(pnl_pct) FILTER (WHERE won IS NOT NULL) as avg_pnl_pct
        FROM backtest_signal_evaluations
        WHERE run_id = %s
        GROUP BY strategy, action
        ORDER BY strategy, action
        """
        rows = self._query(sql, (run_id,))
        result: Dict[str, Any] = {"by_strategy_action": []}
        for r in rows:
            result["by_strategy_action"].append(
                {
                    "strategy": r[0],
                    "action": r[1],
                    "total": r[2],
                    "filtered_count": r[3],
                    "won_count": r[4],
                    "lost_count": r[5],
                    "avg_confidence": round(float(r[6]), 4) if r[6] else None,
                    "avg_pnl_pct": round(float(r[7]), 4) if r[7] else None,
                }
            )
        return result

    @staticmethod
    def _run_row_to_dict(row: tuple) -> Dict[str, Any]:
        """将 backtest_runs 行转为字典。"""
        return {
            "run_id": row[0],
            "created_at": (
                row[1].isoformat() if isinstance(row[1], datetime) else row[1]
            ),
            "config": row[2],
            "param_set": row[3],
            "metrics": row[4],
            "metrics_by_regime": row[5],
            "metrics_by_strategy": row[6],
            "equity_curve": row[7],
            "status": row[8],
            "duration_ms": row[9],
            "filter_stats": row[10] if len(row) > 10 else None,
        }

    # ── 参数推荐 CRUD ─────────────────────────────────────────────────

    def save_recommendation(self, rec: "Recommendation") -> None:
        """持久化参数推荐记录。"""
        changes_json = [
            {
                "section": c.section,
                "key": c.key,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "change_pct": c.change_pct,
            }
            for c in rec.changes
        ]
        row = (
            rec.rec_id,
            rec.source_run_id,
            rec.created_at,
            rec.status.value,
            rec.overfitting_ratio,
            rec.consistency_rate,
            rec.oos_sharpe,
            rec.oos_win_rate,
            rec.oos_total_trades,
            self._writer._json(changes_json),
            rec.rationale,
        )
        self._writer._batch(INSERT_RECOMMENDATION_SQL, [row])
        logger.info("Recommendation %s persisted", rec.rec_id)

    def update_recommendation(self, rec: "Recommendation") -> None:
        """更新推荐记录的状态字段。"""
        row = (
            rec.status.value,
            rec.approved_at,
            rec.applied_at,
            rec.rolled_back_at,
            rec.backup_path,
            rec.rec_id,
        )
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(UPDATE_RECOMMENDATION_STATUS_SQL, row)
        logger.info(
            "Recommendation %s updated: status=%s", rec.rec_id, rec.status.value
        )

    def fetch_recommendation(self, rec_id: str) -> Optional["Recommendation"]:
        """查询单条推荐记录。"""
        rows = self._query(FETCH_RECOMMENDATION_SQL, (rec_id,))
        if not rows:
            return None
        return self._rec_row_to_model(rows[0])

    def fetch_recommendations(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List["Recommendation"]:
        """查询推荐记录列表。"""
        if status:
            where_clause = "WHERE status = %s"
            params: tuple = (status, limit, offset)
        else:
            where_clause = ""
            params = (limit, offset)

        sql = LIST_RECOMMENDATIONS_SQL.format(where_clause=where_clause)
        rows = self._query(sql, params)
        return [self._rec_row_to_model(r) for r in rows]

    @staticmethod
    def _rec_row_to_model(row: tuple) -> "Recommendation":
        """将 backtest_recommendations 行转为 Recommendation 模型。"""
        from src.backtesting.models import (
            ParamChange,
            Recommendation,
            RecommendationStatus,
        )

        changes_raw = row[9] if isinstance(row[9], list) else json.loads(row[9])
        changes = [
            ParamChange(
                section=c["section"],
                key=c["key"],
                old_value=c.get("old_value"),
                new_value=c["new_value"],
                change_pct=c.get("change_pct", 0.0),
            )
            for c in changes_raw
        ]

        return Recommendation(
            rec_id=row[0],
            source_run_id=row[1],
            created_at=(
                row[2]
                if isinstance(row[2], datetime)
                else datetime.fromisoformat(row[2])
            ),
            status=RecommendationStatus(row[3]),
            overfitting_ratio=row[4] or 0.0,
            consistency_rate=row[5] or 0.0,
            oos_sharpe=row[6] or 0.0,
            oos_win_rate=row[7] or 0.0,
            oos_total_trades=row[8] or 0,
            changes=changes,
            rationale=row[10] or "",
            approved_at=row[11],
            applied_at=row[12],
            rolled_back_at=row[13],
            backup_path=row[14],
        )
