"""Lab Impact 读模型（P10.5 / ADR-010）。

替代 QuantX `Lab` 工作台的 walk-forward + recommendations + demo validation 多路拼接，
单端点 `/v1/lab/impact` 返回：
- walk_forward_snapshots：最近 WF 结果（含分阶段指标）
- recommendations：含 lifecycle + linked demo validation windows
- demo_validation_windows：按 (strategy, time_window) 聚合 db.demo.trade_outcomes
- experiment_links：按 experiment_id 聚合研究链路（贯通跳转）

数据源（ADR-010 后）：
- walk_forward_results / recommendations 从 BacktestRuntimeStore 内存读取
- demo_validation_windows 从 db.demo.trade_outcomes 聚合（替代旧 paper_trading_sessions）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence


class LabImpactReadModel:
    """Lab Impact 聚合读模型（P10.5 / ADR-010）。"""

    DEFAULT_WF_LIMIT: int = 10
    DEFAULT_RECOMMENDATION_LIMIT: int = 20
    DEFAULT_DEMO_VALIDATION_WINDOW_LIMIT: int = 20

    def __init__(
        self,
        *,
        backtest_store: Any,
        trade_outcome_repo: Any = None,
        backtest_repo: Any = None,
    ) -> None:
        self._backtest_store = backtest_store
        # ADR-010: paper_trading_repo 删除，改用 trade_outcome_repo 聚合 demo trade_outcomes
        self._trade_outcome_repo = trade_outcome_repo
        self._backtest_repo = backtest_repo

    def build_impact(
        self,
        *,
        wf_limit: int = DEFAULT_WF_LIMIT,
        recommendation_limit: int = DEFAULT_RECOMMENDATION_LIMIT,
        demo_validation_window_limit: int = DEFAULT_DEMO_VALIDATION_WINDOW_LIMIT,
    ) -> dict[str, Any]:
        observed_at = datetime.now(timezone.utc).isoformat()

        wf_snapshots = self._collect_walk_forwards(wf_limit)
        recommendations = self._collect_recommendations(recommendation_limit)
        demo_validation_windows = self._collect_demo_validation_windows(
            demo_validation_window_limit
        )
        experiment_links = self._build_experiment_links(
            wf_snapshots=wf_snapshots,
            recommendations=recommendations,
        )
        return {
            "observed_at": observed_at,
            "walk_forward_snapshots": wf_snapshots,
            "recommendations": recommendations,
            "demo_validation_windows": demo_validation_windows,
            "experiment_links": experiment_links,
            "freshness": {
                "observed_at": observed_at,
                "source_kind": "native",
                "fallback_applied": False,
                "fallback_reason": None,
            },
        }

    # ────────────── Walk-Forward ──────────────
    def _collect_walk_forwards(self, limit: int) -> list[dict[str, Any]]:
        store = self._backtest_store
        if store is None:
            return []
        with getattr(store, "walk_forward_lock", _noop_lock()):
            items = list(store.walk_forward_results.items())
        snapshots: list[dict[str, Any]] = []
        for run_id, result in items[-limit:]:
            snapshots.append(self._serialize_walk_forward(run_id, result))
        snapshots.reverse()
        return snapshots

    @staticmethod
    def _serialize_walk_forward(run_id: str, result: Any) -> dict[str, Any]:
        if hasattr(result, "to_dict"):
            payload = result.to_dict()
        elif isinstance(result, dict):
            payload = result
        else:
            payload = {"raw": str(result)}
        phases: list[dict[str, Any]] = []
        raw_phases = payload.get("phases") or payload.get("windows") or []
        for phase in raw_phases:
            if not isinstance(phase, dict):
                continue
            phases.append(
                {
                    "window_id": phase.get("window_id") or phase.get("name"),
                    "train_period": phase.get("train_period"),
                    "validation_period": phase.get("validation_period"),
                    "is_sharpe": phase.get("is_sharpe"),
                    "oos_sharpe": phase.get("oos_sharpe"),
                    "is_win_rate": phase.get("is_win_rate"),
                    "oos_win_rate": phase.get("oos_win_rate"),
                    "trade_count": phase.get("trade_count"),
                    "overfitting_ratio": phase.get("overfitting_ratio"),
                }
            )
        return {
            "run_id": run_id,
            "source_run_id": payload.get("source_run_id") or run_id,
            "status": payload.get("status"),
            "overfitting_ratio": payload.get("overfitting_ratio"),
            "consistency_rate": payload.get("consistency_rate"),
            "oos_sharpe": payload.get("oos_sharpe"),
            "oos_win_rate": payload.get("oos_win_rate"),
            "oos_total_trades": payload.get("oos_total_trades"),
            "phases": phases,
            "submitted_at": payload.get("submitted_at") or payload.get("started_at"),
            "completed_at": payload.get("completed_at"),
            "experiment_id": payload.get("experiment_id"),
        }

    # ────────────── Recommendations ──────────────
    def _collect_recommendations(self, limit: int) -> list[dict[str, Any]]:
        """合并内存 store + DB 回读，按 rec_id 去重，created_at 倒序。

        ADR-010 后：linked_paper_sessions 字段移除（paper trading 已废弃），
        QuantX Lab 前端需改读顶层 demo_validation_windows 字段。
        """
        collected: dict[str, dict[str, Any]] = {}

        store = self._backtest_store
        if store is not None:
            with getattr(store, "recommendation_lock", _noop_lock()):
                items = list(store.recommendations.items())
            for rec_id, recommendation in items:
                payload = self._serialize_recommendation(rec_id, recommendation)
                if payload:
                    collected[str(payload.get("rec_id") or rec_id)] = payload

        if self._backtest_repo is not None:
            try:
                db_recs = self._backtest_repo.fetch_recommendations(limit=limit)
            except Exception:
                db_recs = []
            for recommendation in db_recs or []:
                payload = self._serialize_recommendation(None, recommendation)
                if not payload:
                    continue
                rec_id = str(payload.get("rec_id") or "")
                if rec_id and rec_id not in collected:
                    collected[rec_id] = payload

        ordered = sorted(
            collected.values(),
            key=lambda p: str(p.get("created_at") or ""),
            reverse=True,
        )[:limit]
        return ordered

    @staticmethod
    def _serialize_recommendation(
        rec_id: Optional[str], recommendation: Any
    ) -> Optional[dict[str, Any]]:
        if recommendation is None:
            return None
        if hasattr(recommendation, "to_dict"):
            payload = dict(recommendation.to_dict())
        elif isinstance(recommendation, dict):
            payload = dict(recommendation)
        else:
            return None
        if rec_id is not None:
            payload.setdefault("rec_id", rec_id)
        return payload

    # ────────────── Demo Validation Windows ──────────────
    def _collect_demo_validation_windows(self, limit: int) -> list[dict[str, Any]]:
        """从 demo trade_outcomes 按 (strategy, timeframe, time_window) 聚合（ADR-010）。

        当前实现：返回最近 N 个 strategy×timeframe 窗口（按 7 天滚动），
        每窗口包含 trades 数 / win_rate / total_pnl / avg_slippage 摘要。
        当 trade_outcome_repo 不可用时返回空列表（前端需处理空数据）。
        """
        repo = self._trade_outcome_repo
        if repo is None:
            return []
        # repo.fetch_recent_windows() 还未实现——预留接口以待 Phase 6+ 跨域整合时补齐
        # 目前返回空数组，前端 lab 页可显示"暂无 demo validation 数据"
        fetcher = getattr(repo, "fetch_demo_validation_windows", None)
        if fetcher is None:
            return []
        try:
            window_end = datetime.now(timezone.utc)
            window_start = window_end - timedelta(days=7)
            return list(
                fetcher(
                    window_start=window_start,
                    window_end=window_end,
                    limit=limit,
                )
                or []
            )
        except Exception:
            return []

    # ────────────── Experiment Links ──────────────
    @staticmethod
    def _build_experiment_links(
        *,
        wf_snapshots: Sequence[dict[str, Any]],
        recommendations: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        experiment_map: dict[str, dict[str, Any]] = {}

        def _bucket(exp_id: Optional[str]) -> dict[str, Any]:
            key = str(exp_id or "").strip() or "__unbound__"
            return experiment_map.setdefault(
                key,
                {
                    "experiment_id": key if key != "__unbound__" else None,
                    "walk_forward_run_ids": [],
                    "recommendation_ids": [],
                    "backtest_run_ids": [],
                },
            )

        for wf in wf_snapshots:
            bucket = _bucket(wf.get("experiment_id"))
            bucket["walk_forward_run_ids"].append(wf.get("run_id"))
            source_run = wf.get("source_run_id")
            if source_run and source_run not in bucket["backtest_run_ids"]:
                bucket["backtest_run_ids"].append(source_run)
        for rec in recommendations:
            bucket = _bucket(rec.get("experiment_id"))
            rec_id = rec.get("rec_id")
            if rec_id and rec_id not in bucket["recommendation_ids"]:
                bucket["recommendation_ids"].append(rec_id)
            source_run = rec.get("source_run_id")
            if source_run and source_run not in bucket["backtest_run_ids"]:
                bucket["backtest_run_ids"].append(source_run)
        return [
            {
                **bucket,
                "count": {
                    "walk_forward": len(bucket["walk_forward_run_ids"]),
                    "recommendations": len(bucket["recommendation_ids"]),
                    "backtest_runs": len(bucket["backtest_run_ids"]),
                },
            }
            for bucket in experiment_map.values()
        ]


class _NullLock:
    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _noop_lock() -> _NullLock:
    return _NullLock()
