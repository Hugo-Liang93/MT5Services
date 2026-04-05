"""置信度后处理纯逻辑：intrabar 衰减、HTF 方向对齐修正。

这些函数从 SignalRuntime._evaluate_strategies() 提取，
可被实盘 SignalRuntime 和回测 BacktestEngine 共用。

设计原则：回测使用实盘方法，不重新实现，避免模拟失真。
"""

from __future__ import annotations

import dataclasses
import math
from typing import Optional

from .models import SignalDecision


def _safe_confidence(value: float) -> float:
    """确保 confidence 是有限浮点数，NaN/Inf 降级为 0.0。"""
    if math.isfinite(value):
        return value
    return 0.0


def apply_intrabar_decay(
    decision: SignalDecision,
    scope: str,
    decay: float,
) -> SignalDecision:
    """对 intrabar scope 的信号施加置信度衰减。

    实盘中：SignalRuntime._evaluate_strategies() 第 1049-1058 行。

    Args:
        decision: 策略评估结果
        scope: "confirmed" 或 "intrabar"
        decay: 衰减系数（默认 0.85，< 1.0 时生效）

    Returns:
        修改后的 SignalDecision（不可变替换）
    """
    if scope != "intrabar" or decay >= 1.0:
        return decision
    return dataclasses.replace(
        decision,
        confidence=_safe_confidence(decision.confidence * decay),
    )


def apply_htf_alignment(
    decision: SignalDecision,
    htf_direction: Optional[str],
    alignment_boost: float = 1.10,
    conflict_penalty: float = 0.70,
) -> SignalDecision:
    """根据 HTF 方向对齐/冲突修正置信度。

    实盘中：SignalRuntime._evaluate_strategies() 第 1059-1073 行。

    简化版：不含 strength/stability 加权（需要 HTFStateCache 上下文）。
    回测中 HTF 数据来自同一回测 session 的高时间框架计算结果。

    Args:
        decision: 策略评估结果
        htf_direction: HTF 信号方向（"buy" / "sell" / None）
        alignment_boost: 对齐时的置信度乘数（默认 1.10）
        conflict_penalty: 冲突时的置信度乘数（默认 0.70）

    Returns:
        修改后的 SignalDecision（不可变替换）
    """
    if decision.direction not in ("buy", "sell"):
        return decision
    if htf_direction is None:
        return decision

    if htf_direction == decision.direction:
        multiplier = alignment_boost
    else:
        multiplier = conflict_penalty

    return dataclasses.replace(
        decision,
        confidence=_safe_confidence(min(1.0, decision.confidence * multiplier)),
        metadata={
            **decision.metadata,
            "htf_direction": htf_direction,
            "htf_alignment": "aligned" if multiplier >= 1.0 else "conflict",
            "htf_confidence_multiplier": multiplier,
        },
    )
