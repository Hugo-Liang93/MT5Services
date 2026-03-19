from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Protocol

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision


class SignalStrategy(Protocol):
    """所有信号策略必须实现的协议。

    类属性说明
    ----------
    name:
        唯一字符串标识，用于注册、日志、API 路由。
    required_indicators:
        策略评估所需的指标名称元组，与 config/indicators.json 中的 name 对应。
    preferred_scopes:
        接收快照的 scope 范围。
        "confirmed" = 仅 bar 收盘快照（指标完整）；
        "intrabar"  = 实时盘中快照（仅 intrabar_eligible 指标）。
        默认两者都接收。
    regime_affinity:
        该策略在不同市场状态（Regime）下的置信度乘数（0.0–1.0）。
        此值由 SignalModule.evaluate() 在策略返回决策后自动施加：
          adjusted_confidence = decision.confidence × affinity[regime]
        乘数语义：
          1.0 → 完全采信，不衰减
          0.5 → 信号减半，高于默认阈值(0.55)的信号可能恰好被压制
          0.1 → 几乎完全压制，仅极高置信度信号才能通过
        缺少某个 Regime 键时默认使用 0.5（中性）。
        ⚠️ 新增策略时**必须**填写此属性，参见 CLAUDE.md §Adding New Signal Strategies。
    """

    name: str
    required_indicators: tuple[str, ...]
    preferred_scopes: tuple[str, ...]
    regime_affinity: Dict[RegimeType, float]

    def evaluate(self, context: SignalContext) -> SignalDecision:
        ...


def _resolve_indicator_value(
    indicators: Dict[str, Dict[str, Any]],
    candidates: Iterable[tuple[str, str]],
) -> tuple[float | None, str | None]:
    for indicator_name, field_name in candidates:
        payload = indicators.get(indicator_name)
        if not isinstance(payload, dict):
            continue
        value = payload.get(field_name)
        if value is None:
            continue
        try:
            return float(value), indicator_name
        except (TypeError, ValueError):
            continue
    return None, None
