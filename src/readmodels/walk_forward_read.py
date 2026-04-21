"""Walk-Forward detail 读模型（P11 Phase 2）。

从 `WalkForwardRepository` 读原始数据，装配成 `WalkForwardDetail` payload：
- `aggregate`：聚合指标（overfitting_ratio / consistency_rate / oos_sharpe 等）
- `windows`：每个 split 的 IS/OOS 指标
- `freshness`：标准 freshness 块

查询策略：
- `build_by_wf_run_id`：按 wf_run_id 直接查
- `build_by_backtest_run_id`：按 backtest_run_id 查最新一次（罕见，WF 通常独立提交）
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.readmodels.freshness import build_freshness_block

logger = logging.getLogger(__name__)


# WF 结果完成后就是静态快照，freshness 主要用于 observed_at 语义一致性
_STALE_AFTER_SECONDS = 3600.0 * 24.0
_MAX_AGE_SECONDS = 3600.0 * 24.0 * 30.0


class WalkForwardReadModel:
    """Walk-Forward detail 读模型（每请求构造）。"""

    def __init__(self, *, walk_forward_repo: Any) -> None:
        self._repo = walk_forward_repo

    # ────────────────────── Public API ──────────────────────

    def build_by_wf_run_id(self, wf_run_id: str) -> Optional[Dict[str, Any]]:
        """按 wf_run_id 查 WF detail。不存在返 None。"""
        if self._repo is None:
            logger.warning("walk_forward_repo unavailable, cannot fetch %s", wf_run_id)
            return None
        bundle = self._repo.fetch(wf_run_id)
        if bundle is None:
            return None
        return self._assemble(bundle)

    def build_by_backtest_run_id(
        self, backtest_run_id: str
    ) -> Optional[Dict[str, Any]]:
        """按 backtest_run_id 查最新一次 WF（如果有）。"""
        if self._repo is None:
            return None
        bundle = self._repo.fetch_latest_by_backtest_run(backtest_run_id)
        if bundle is None:
            return None
        return self._assemble(bundle)

    # ────────────────────── Assembly ──────────────────────

    def _assemble(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        run = bundle["run"]
        raw_windows = bundle["windows"]
        windows: List[Dict[str, Any]] = [self._serialize_window(w) for w in raw_windows]
        aggregate = {
            "overfitting_ratio": run.get("overfitting_ratio"),
            "consistency_rate": run.get("consistency_rate"),
            "oos_sharpe": run.get("oos_sharpe"),
            "oos_win_rate": run.get("oos_win_rate"),
            "oos_total_trades": run.get("oos_total_trades"),
            "oos_total_pnl": run.get("oos_total_pnl"),
        }

        freshness = build_freshness_block(
            observed_at=datetime.now(timezone.utc).isoformat(),
            data_updated_at=run.get("completed_at") or run.get("created_at"),
            stale_after_seconds=_STALE_AFTER_SECONDS,
            max_age_seconds=_MAX_AGE_SECONDS,
            source_kind="native",
        )

        return {
            "wf_run_id": run["wf_run_id"],
            "backtest_run_id": run.get("backtest_run_id"),
            "created_at": run["created_at"],
            "completed_at": run.get("completed_at"),
            "status": run.get("status") or "completed",
            "duration_ms": run.get("duration_ms"),
            "error": run.get("error"),
            "experiment_id": run.get("experiment_id"),
            "n_splits": int(run.get("n_splits") or len(windows)),
            "config": self._ensure_dict(run.get("config")),
            "aggregate": aggregate,
            "windows": windows,
            "freshness": freshness,
        }

    @staticmethod
    def _serialize_window(raw: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "split_index": int(raw["split_index"]),
            "window_label": raw.get("window_label") or "",
            "train_start": raw.get("train_start"),
            "train_end": raw.get("train_end"),
            "test_start": raw.get("test_start"),
            "test_end": raw.get("test_end"),
            "best_params": WalkForwardReadModel._ensure_dict(raw.get("best_params")),
            "is_pnl": raw.get("is_pnl"),
            "oos_pnl": raw.get("oos_pnl"),
            "is_sharpe": raw.get("is_sharpe"),
            "oos_sharpe": raw.get("oos_sharpe"),
            "is_win_rate": raw.get("is_win_rate"),
            "oos_win_rate": raw.get("oos_win_rate"),
            "oos_max_drawdown": raw.get("oos_max_drawdown"),
            "oos_trade_count": raw.get("oos_trade_count"),
        }

    @staticmethod
    def _ensure_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}
