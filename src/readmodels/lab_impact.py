"""Lab Impact 读模型（P10.5）。

替代 QuantX `Lab` 工作台当前对 `/v1/backtest/walk-forward + /v1/backtest/recommendations
+ /v1/paper-trading/sessions` 的多路拼接，单端点 `/v1/lab/impact` 返回：
- walk_forward_snapshots：最近 WF 结果（含分阶段指标）
- recommendations：含 lifecycle + linked paper sessions
- paper_sessions：含 recommendation_id / source_backtest_run_id / experiment_id
- experiment_links：按 experiment_id 聚合研究链路（贯通跳转）

数据源：
- walk_forward_results / recommendations 从 BacktestRuntimeStore 内存读取
- paper_sessions 从 TimescaleDB `paper_trading_sessions` 表（P10.5 迁移已加 3 列）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence


class LabImpactReadModel:
    """Lab Impact 聚合读模型（P10.5）。"""

    DEFAULT_WF_LIMIT: int = 10
    DEFAULT_RECOMMENDATION_LIMIT: int = 20
    DEFAULT_PAPER_SESSION_LIMIT: int = 20

    def __init__(
        self,
        *,
        backtest_store: Any,
        paper_trading_repo: Any,
        backtest_repo: Any = None,
    ) -> None:
        self._backtest_store = backtest_store
        self._paper_trading_repo = paper_trading_repo
        # P10.5 修复：backtest_repo 为 DB 持久化 fallback，内存 store 重启后走 DB 回读
        self._backtest_repo = backtest_repo

    def build_impact(
        self,
        *,
        wf_limit: int = DEFAULT_WF_LIMIT,
        recommendation_limit: int = DEFAULT_RECOMMENDATION_LIMIT,
        paper_session_limit: int = DEFAULT_PAPER_SESSION_LIMIT,
    ) -> dict[str, Any]:
        observed_at = datetime.now(timezone.utc).isoformat()

        wf_snapshots = self._collect_walk_forwards(wf_limit)
        recommendations = self._collect_recommendations(
            recommendation_limit,
            paper_trading_repo=self._paper_trading_repo,
        )
        paper_sessions = self._collect_paper_sessions(paper_session_limit)
        experiment_links = self._build_experiment_links(
            wf_snapshots=wf_snapshots,
            recommendations=recommendations,
            paper_sessions=paper_sessions,
        )
        return {
            "observed_at": observed_at,
            "walk_forward_snapshots": wf_snapshots,
            "recommendations": recommendations,
            "paper_sessions": paper_sessions,
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
        # 最新的排前面
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
    def _collect_recommendations(
        self,
        limit: int,
        *,
        paper_trading_repo: Any,
    ) -> list[dict[str, Any]]:
        """合并内存 store + DB 回读，按 rec_id 去重，created_at 倒序。

        修复（P10.5 回溯）：内存 store 重启丢数据；DB 的 `backtest_recommendations`
        表才是权威事实源。内存保留是为最近写入的快速访问（无 DB roundtrip）。
        """
        collected: dict[str, dict[str, Any]] = {}

        # 1) 内存 store 先扫（快）
        store = self._backtest_store
        if store is not None:
            with getattr(store, "recommendation_lock", _noop_lock()):
                items = list(store.recommendations.items())
            for rec_id, recommendation in items:
                payload = self._serialize_recommendation(rec_id, recommendation)
                if payload:
                    collected[str(payload.get("rec_id") or rec_id)] = payload

        # 2) DB 回读补充（重启后兜底）
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

        # 3) 按 created_at DESC 截断到 limit（string / datetime 兼容）
        ordered = sorted(
            collected.values(),
            key=lambda p: str(p.get("created_at") or ""),
            reverse=True,
        )[:limit]

        # 4) 补 linked_paper_sessions
        for payload in ordered:
            rec_id = str(payload.get("rec_id") or "")
            linked_sessions = self._safe_fetch_sessions_by_rec(
                paper_trading_repo, rec_id
            )
            payload["linked_paper_sessions"] = [
                {
                    "session_id": s.get("session_id"),
                    "started_at": s.get("started_at"),
                    "stopped_at": s.get("stopped_at"),
                    "total_trades": s.get("total_trades"),
                    "total_pnl": s.get("total_pnl"),
                    "sharpe_ratio": s.get("sharpe_ratio"),
                }
                for s in linked_sessions
            ]
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

    @staticmethod
    def _safe_fetch_sessions_by_rec(
        paper_trading_repo: Any,
        rec_id: str,
    ) -> list[dict[str, Any]]:
        if paper_trading_repo is None:
            return []
        try:
            return list(
                paper_trading_repo.fetch_sessions_by_recommendation(rec_id) or []
            )
        except Exception:
            return []

    # ────────────── Paper Sessions ──────────────
    def _collect_paper_sessions(self, limit: int) -> list[dict[str, Any]]:
        repo = self._paper_trading_repo
        if repo is None:
            return []
        try:
            return list(repo.fetch_sessions(limit=limit) or [])
        except Exception:
            return []

    # ────────────── Experiment Links ──────────────
    @staticmethod
    def _build_experiment_links(
        *,
        wf_snapshots: Sequence[dict[str, Any]],
        recommendations: Sequence[dict[str, Any]],
        paper_sessions: Sequence[dict[str, Any]],
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
                    "paper_session_ids": [],
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
        for session in paper_sessions:
            bucket = _bucket(session.get("experiment_id"))
            sid = session.get("session_id")
            if sid and sid not in bucket["paper_session_ids"]:
                bucket["paper_session_ids"].append(sid)
            rec_id = session.get("recommendation_id")
            if rec_id and rec_id not in bucket["recommendation_ids"]:
                bucket["recommendation_ids"].append(rec_id)
            source_run = session.get("source_backtest_run_id")
            if source_run and source_run not in bucket["backtest_run_ids"]:
                bucket["backtest_run_ids"].append(source_run)
        return [
            {
                **bucket,
                "count": {
                    "walk_forward": len(bucket["walk_forward_run_ids"]),
                    "recommendations": len(bucket["recommendation_ids"]),
                    "backtest_runs": len(bucket["backtest_run_ids"]),
                    "paper_sessions": len(bucket["paper_session_ids"]),
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
