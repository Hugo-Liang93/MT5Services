"""Lab Evaluation 单端点聚合读模型（P11 Phase 5）。

对标 `LabImpactReadModel` 范式：单端点 `/v1/lab/evaluations/{run_id}` 替代前端
对 4~7 个 detail 端点的拼接，降低 Next.js BFF 复杂度，天然支持 freshness 汇总 +
`capability` 可用性状态。

组合下列 4 个子 ReadModel：
- BacktestDetailReadModel → job / metrics_summary / equity_curve / monthly_returns
                           / trade_structure / execution_realism
- WalkForwardReadModel → walk_forward
- CorrelationAnalysisReadModel → correlation_analysis

前端删除 `adapter.ts` 中全部 alias fallback 分支后，仅消费此单端点即可渲染
策略评估页全部面板。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.readmodels.freshness import age_seconds, build_freshness_block

logger = logging.getLogger(__name__)


# Envelope 级 freshness 阈值（与子模型一致）
_STALE_AFTER_SECONDS = 3600.0 * 24.0
_MAX_AGE_SECONDS = 3600.0 * 24.0 * 30.0


# capability 状态常量
_CAPABILITY_READY = "ready"
_CAPABILITY_PARTIAL = "partial"
_CAPABILITY_MISSING = "missing"


class LabEvaluationReadModel:
    """Lab 评估页单端点聚合（P11 Phase 5）。

    每请求构造，轻量组合层。子 ReadModel 由 DI 注入，失败时该块标 missing 并
    在顶层 capability 中记录，其他块不受影响。
    """

    def __init__(
        self,
        *,
        backtest_detail: Any,
        walk_forward: Any,
        correlation: Any,
    ) -> None:
        self._backtest_detail = backtest_detail
        self._walk_forward = walk_forward
        self._correlation = correlation

    def build(self, run_id: str) -> Optional[Dict[str, Any]]:
        """聚合装配 LabEvaluationEnvelope。

        Returns:
            None 当 run_id 对应的 backtest_run 不存在（404）；否则返回完整 envelope。
        """
        # 1) metrics_summary 作为主锚点 —— 不存在视为 run 不存在
        metrics = self._safe_call(
            lambda: self._backtest_detail.build_metrics_summary(run_id),
            "metrics_summary",
        )
        if metrics is None:
            return None

        # 2) 并发装配其余块（任一失败不影响其他）
        equity_curve = self._safe_call(
            lambda: self._backtest_detail.build_equity_curve(run_id),
            "equity_curve",
        )
        monthly_returns = self._safe_call(
            lambda: self._backtest_detail.build_monthly_returns(run_id),
            "monthly_returns",
        )
        trade_structure = self._safe_call(
            lambda: self._backtest_detail.build_trade_structure(run_id),
            "trade_structure",
        )
        execution_realism = self._safe_call(
            lambda: self._backtest_detail.build_execution_realism(run_id),
            "execution_realism",
        )
        walk_forward = self._safe_call(
            lambda: (
                self._walk_forward.build_by_wf_run_id(run_id)
                or self._walk_forward.build_by_backtest_run_id(run_id)
            ),
            "walk_forward",
        )
        correlation_analysis = self._safe_call(
            lambda: self._correlation.build_latest_for_run(run_id),
            "correlation_analysis",
        )

        # 3) capability 映射（前端按 state 决定 UI 表现）
        capability = {
            "metrics_summary": _CAPABILITY_READY,  # 主锚点非空才到这里
            "equity_curve": self._capability_for(equity_curve, "points"),
            "monthly_returns": self._capability_for(monthly_returns, "months"),
            "trade_structure": self._capability_trade_structure(trade_structure),
            "execution_realism": self._capability_execution_realism(execution_realism),
            "walk_forward": _CAPABILITY_READY if walk_forward else _CAPABILITY_MISSING,
            "correlation_analysis": (
                _CAPABILITY_READY if correlation_analysis else _CAPABILITY_MISSING
            ),
        }

        # 4) 汇总 freshness：取所有子块中最老的 data_updated_at
        envelope_freshness = self._aggregate_freshness(
            [
                metrics.get("freshness"),
                equity_curve.get("freshness") if equity_curve else None,
                monthly_returns.get("freshness") if monthly_returns else None,
                trade_structure.get("freshness") if trade_structure else None,
                execution_realism.get("freshness") if execution_realism else None,
                walk_forward.get("freshness") if walk_forward else None,
                correlation_analysis.get("freshness") if correlation_analysis else None,
            ]
        )

        return {
            "run_id": run_id,
            "observed_at": envelope_freshness["observed_at"],
            "freshness": envelope_freshness,
            "capability": capability,
            "metrics_summary": metrics,
            "equity_curve": equity_curve,
            "monthly_returns": monthly_returns,
            "trade_structure": trade_structure,
            "execution_realism": execution_realism,
            "walk_forward": walk_forward,
            "correlation_analysis": correlation_analysis,
        }

    # ────────────────────── Helpers ──────────────────────

    @staticmethod
    def _safe_call(fn: Any, label: str) -> Optional[Dict[str, Any]]:
        """子 ReadModel 调用失败时标 missing（不让整个 envelope 崩溃）。"""
        try:
            result = fn()
        except Exception:
            logger.warning(
                "LabEvaluation subblock '%s' failed; marking missing",
                label,
                exc_info=True,
            )
            return None
        return result if isinstance(result, dict) else None

    @staticmethod
    def _capability_for(payload: Optional[Dict[str, Any]], list_key: str) -> str:
        """通用列表型块的 capability：空返 partial；完全没 payload 返 missing。"""
        if payload is None:
            return _CAPABILITY_MISSING
        items = payload.get(list_key) or []
        return _CAPABILITY_READY if items else _CAPABILITY_PARTIAL

    @staticmethod
    def _capability_trade_structure(
        payload: Optional[Dict[str, Any]],
    ) -> str:
        if payload is None:
            return _CAPABILITY_MISSING
        if payload.get("total_trades", 0) == 0:
            return _CAPABILITY_PARTIAL
        if payload.get("partial_data"):
            return _CAPABILITY_PARTIAL
        return _CAPABILITY_READY

    @staticmethod
    def _capability_execution_realism(
        payload: Optional[Dict[str, Any]],
    ) -> str:
        if payload is None:
            return _CAPABILITY_MISSING
        return _CAPABILITY_READY if payload.get("available") else _CAPABILITY_MISSING

    @staticmethod
    def _aggregate_freshness(blocks: list) -> Dict[str, Any]:
        """取所有子块中最老的 data_updated_at，重组 envelope 级 freshness。"""
        observed_at = datetime.now(timezone.utc).isoformat()
        oldest: Optional[str] = None
        oldest_age: Optional[float] = None
        for b in blocks:
            if not isinstance(b, dict):
                continue
            ts = b.get("data_updated_at")
            if ts is None:
                continue
            age = age_seconds(ts)
            if age is None:
                continue
            if oldest_age is None or age > oldest_age:
                oldest_age = age
                oldest = ts
        return build_freshness_block(
            observed_at=observed_at,
            data_updated_at=oldest,
            stale_after_seconds=_STALE_AFTER_SECONDS,
            max_age_seconds=_MAX_AGE_SECONDS,
            source_kind="native",
        )
