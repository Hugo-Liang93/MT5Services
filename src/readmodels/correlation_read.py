"""策略相关性分析 detail 读模型（P11 Phase 3）。

数据源：`CorrelationAnalysisRepository`。装配统一 freshness 块 + 类型安全的字段输出。
支持按 analysis_id 精确查询或按 backtest_run_id 查最新。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.readmodels.freshness import build_freshness_block

logger = logging.getLogger(__name__)


# Correlation 结果一旦落库就是静态快照，与 WF 同样给宽松阈值
_STALE_AFTER_SECONDS = 3600.0 * 24.0
_MAX_AGE_SECONDS = 3600.0 * 24.0 * 30.0


class CorrelationAnalysisReadModel:
    """Correlation detail 读模型（每请求构造）。"""

    def __init__(self, *, correlation_repo: Any) -> None:
        self._repo = correlation_repo

    # ────────────────────── Public API ──────────────────────

    def build_by_analysis_id(self, analysis_id: str) -> Optional[Dict[str, Any]]:
        """按 analysis_id 精确查询。"""
        if self._repo is None:
            logger.warning("correlation_repo unavailable, cannot fetch %s", analysis_id)
            return None
        raw = self._repo.fetch(analysis_id)
        return self._assemble(raw) if raw else None

    def build_latest_for_run(self, backtest_run_id: str) -> Optional[Dict[str, Any]]:
        """按 backtest_run_id 查最新一次分析。"""
        if self._repo is None:
            return None
        raw = self._repo.fetch_latest_for_run(backtest_run_id)
        return self._assemble(raw) if raw else None

    def list_for_run(
        self,
        backtest_run_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """列出某 run 的所有相关性分析历史。"""
        if self._repo is None:
            return []
        raw_rows = self._repo.list_for_run(backtest_run_id, limit=limit, offset=offset)
        return [self._assemble(r) for r in raw_rows]

    # ────────────────────── Assembly ──────────────────────

    def _assemble(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        freshness = build_freshness_block(
            observed_at=datetime.now(timezone.utc).isoformat(),
            data_updated_at=raw.get("created_at"),
            stale_after_seconds=_STALE_AFTER_SECONDS,
            max_age_seconds=_MAX_AGE_SECONDS,
            source_kind="native",
        )
        return {
            "analysis_id": raw["analysis_id"],
            "backtest_run_id": raw["backtest_run_id"],
            "created_at": raw["created_at"],
            "correlation_threshold": raw.get("correlation_threshold") or 0.0,
            "penalty_weight": raw.get("penalty_weight") or 0.0,
            "total_bars_analyzed": int(raw.get("total_bars_analyzed") or 0),
            "strategies_analyzed": int(raw.get("strategies_analyzed") or 0),
            "high_correlation_count": int(raw.get("high_correlation_count") or 0),
            "pairs": list(raw.get("pairs") or []),
            "high_correlation_pairs": list(raw.get("high_correlation_pairs") or []),
            "strategy_weights": dict(raw.get("strategy_weights") or {}),
            "summary": dict(raw.get("summary") or {}),
            "freshness": freshness,
        }
