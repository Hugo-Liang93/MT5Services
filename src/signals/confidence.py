"""置信度后处理纯逻辑：intrabar 衰减。

设计原则：回测使用实盘方法，不重新实现，避免模拟失真。
"""

from __future__ import annotations

import dataclasses
import math

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
    """对 intrabar scope 的信号施加置信度衰减。"""
    if scope != "intrabar" or decay >= 1.0:
        return decision
    return dataclasses.replace(
        decision,
        confidence=_safe_confidence(decision.confidence * decay),
    )
