"""Backtest detail 读口子包（Phase 1 P11）。

按资源 + 视角分文件承载：
- metrics_summary.py — 运行级总指标（前端 summary 板）
- equity_curve.py    — 净值 + 回撤时间序列
- monthly_returns.py — 月度收益（热力图）

此子包只承载"已完成回测的 detail 读口"，不掺入任务提交 / 推荐 / 执行逻辑。
后续 Phase 2/3/4 的 walk_forward.py / correlation.py / execution_realism.py /
trade_structure.py 按同样规范扩展。
"""

from __future__ import annotations

from fastapi import APIRouter

from .correlation import router as correlation_router
from .equity_curve import router as equity_curve_router
from .execution_realism import router as execution_realism_router
from .metrics_summary import router as metrics_summary_router
from .monthly_returns import router as monthly_returns_router
from .trade_structure import router as trade_structure_router
from .walk_forward import router as walk_forward_router

router = APIRouter()
router.include_router(metrics_summary_router)
router.include_router(equity_curve_router)
router.include_router(monthly_returns_router)
router.include_router(walk_forward_router)
router.include_router(correlation_router)
router.include_router(trade_structure_router)
router.include_router(execution_realism_router)

__all__ = ["router"]
