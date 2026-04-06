"""结构化策略基类 — 标准化数据访问 + Where(软)/When(硬)/Why(硬) 评估框架。

所有结构化策略继承 StructuredStrategyBase，只需实现 _why() / _when()。
_where() 和 _volume_bonus() 可选实现。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext, SignalDecision
from ..base import get_tf_param

logger = logging.getLogger(__name__)


class StructuredStrategyBase:
    """结构化策略基类。

    子类必须设置类属性：name, category, required_indicators, regime_affinity
    子类必须实现：_when(), _why()
    子类可选实现：_where()（默认返回 0 加分）

    评估流程：
        1. Why → 确定方向 + 确认条件（硬门控）
        2. When → 入场时机（硬门控）
        3. Where → 结构位加分（软门控）
        4. Volume → 量能加分（软门控）
        5. 合并置信度
    """

    name: str = ""
    category: str = ""
    required_indicators: Tuple[str, ...] = ()
    preferred_scopes: Tuple[str, ...] = ("confirmed",)
    regime_affinity: Dict[RegimeType, float] = {}

    _htf: str = "H1"
    _base_confidence: float = 0.50

    def __init__(self, name: Optional[str] = None, htf: str = "H1") -> None:
        if name:
            self.name = name
        self._htf = htf

    # ── 标准数据访问 ──

    def _close(self, ctx: SignalContext) -> Optional[float]:
        for ind in ("boll20", "donchian20"):
            v = ctx.indicators.get(ind, {}).get("close")
            if v is not None:
                return float(v)
        cp = ctx.metadata.get("market_structure", {}).get("close_price")
        return float(cp) if cp is not None else None

    def _atr(self, ctx: SignalContext) -> Optional[float]:
        v = ctx.indicators.get("atr14", {}).get("atr")
        return float(v) if v is not None else None

    def _rsi(self, ctx: SignalContext) -> Tuple[Optional[float], Optional[float]]:
        data = ctx.indicators.get("rsi14", {})
        rsi = data.get("rsi")
        d3 = data.get("rsi_d3")
        return (
            float(rsi) if rsi is not None else None,
            float(d3) if d3 is not None else None,
        )

    def _adx_full(self, ctx: SignalContext) -> Dict[str, Optional[float]]:
        data = ctx.indicators.get("adx14", {})
        return {
            "adx": float(data["adx"]) if data.get("adx") is not None else None,
            "adx_d3": float(data["adx_d3"]) if data.get("adx_d3") is not None else None,
            "plus_di": (
                float(data["plus_di"]) if data.get("plus_di") is not None else None
            ),
            "minus_di": (
                float(data["minus_di"]) if data.get("minus_di") is not None else None
            ),
        }

    def _ms(self, ctx: SignalContext) -> Dict[str, Any]:
        return ctx.metadata.get("market_structure", {})

    def _htf_data(self, ctx: SignalContext) -> Dict[str, Any]:
        return ctx.htf_indicators.get(self._htf, {})

    def _volume_ratio(self, ctx: SignalContext) -> Optional[float]:
        v = ctx.indicators.get("volume_ratio20", {}).get("volume_ratio")
        return float(v) if v is not None else None

    def _mfi(self, ctx: SignalContext) -> Optional[float]:
        v = ctx.indicators.get("mfi14", {}).get("mfi")
        return float(v) if v is not None else None

    def _bb_position(self, ctx: SignalContext) -> Optional[float]:
        boll = ctx.indicators.get("boll20", {})
        upper, lower, close = (
            boll.get("bb_upper"),
            boll.get("bb_lower"),
            boll.get("close"),
        )
        if upper is None or lower is None or close is None:
            return None
        w = float(upper) - float(lower)
        return (float(close) - float(lower)) / w if w > 0 else 0.5

    # ── 子类必须实现 ──

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        """方向确认（硬门控）。返回 (ok, direction, conf_bonus, reason)。"""
        raise NotImplementedError

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        """入场时机（硬门控）。返回 (ok, conf_bonus, reason)。"""
        raise NotImplementedError

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        """结构位加分（软门控）。返回 (bonus, reason)。默认 0。"""
        return 0.0, ""

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        """量能加分（软门控）。默认 0。"""
        return 0.0

    # ── 评估框架 ──

    # regime affinity 低于此值时跳过全部三层评估（计算优化）
    _regime_fast_reject: float = 0.10

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used = list(self.required_indicators)

        # 快速拒绝：regime affinity 极低时不浪费计算
        regime_str = context.metadata.get("_regime")
        if regime_str:
            try:
                aff = self.regime_affinity.get(RegimeType(regime_str), 0.5)
                if aff < self._regime_fast_reject:
                    return self._hold(f"regime_reject:{regime_str}", used)
            except ValueError:
                pass

        why_ok, direction, why_conf, why_reason = self._why(context)
        if not why_ok or direction is None:
            return self._hold(why_reason, used)

        when_ok, when_conf, when_reason = self._when(context, direction)
        if not when_ok:
            return self._hold(when_reason, used)

        where_bonus, where_info = self._where(context, direction)
        vol_bonus = self._volume_bonus(context, direction)

        confidence = (
            self._base_confidence + why_conf + when_conf + where_bonus + vol_bonus
        )

        # 信号等级：A(三层+量能) / B(两层) / C(仅硬门控)
        grade = (
            "A"
            if where_bonus > 0 and vol_bonus > 0
            else "B" if where_bonus > 0 or vol_bonus > 0 else "C"
        )

        reason = f"{self.name}_{direction}:{why_reason},{when_reason}"
        if where_info:
            reason += f",{where_info}"

        return self._make_decision(
            direction,
            confidence,
            reason,
            used,
            metadata={
                "why": why_reason,
                "when": when_reason,
                "where_bonus": where_bonus,
                "vol_bonus": vol_bonus,
                "signal_grade": grade,
            },
        )

    # ── 输出构造 ──

    def _hold(self, reason: str, used: List[str]) -> SignalDecision:
        return SignalDecision(
            strategy=self.name,
            symbol="",
            timeframe="",
            direction="hold",
            confidence=0.0,
            reason=reason,
            used_indicators=used,
        )

    def _make_decision(
        self,
        direction: str,
        confidence: float,
        reason: str,
        used: List[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SignalDecision:
        return SignalDecision(
            strategy=self.name,
            symbol="",
            timeframe="",
            direction=direction,
            confidence=min(max(confidence, 0.0), 0.90),
            reason=reason,
            used_indicators=used,
            metadata=metadata or {},
        )


# ---------------------------------------------------------------------------
#  结构位加分辅助（Where 层公用）
# ---------------------------------------------------------------------------

_STRUCTURE_LEVELS = (
    "previous_day_high",
    "previous_day_low",
    "asia_range_high",
    "asia_range_low",
    "london_open_high",
    "london_open_low",
    "new_york_open_high",
    "new_york_open_low",
)


def _near_structure_level(
    close: float,
    ms: Dict[str, Any],
    atr: float,
    max_atr: float = 1.0,
) -> bool:
    threshold = max_atr * atr
    for name in _STRUCTURE_LEVELS:
        val = ms.get(name)
        if val is not None and abs(close - float(val)) <= threshold:
            return True
    return False


def _structure_bias_bonus(ms: Dict[str, Any], direction: str) -> Tuple[float, str]:
    bias = ms.get("structure_bias", "neutral")
    bullish_biases = (
        "bullish_breakout",
        "bullish_pullback",
        "bullish_reclaim",
        "bullish_sweep_confirmed",
    )
    bearish_biases = (
        "bearish_breakout",
        "bearish_pullback",
        "bearish_reclaim",
        "bearish_sweep_confirmed",
    )

    if direction == "buy" and bias in bullish_biases:
        return 0.10, f"struct={bias}"
    if direction == "sell" and bias in bearish_biases:
        return 0.10, f"struct={bias}"

    fp = ms.get("first_pullback_state", "none")
    if direction == "buy" and str(fp).startswith("bullish_first_pullback"):
        return 0.12, f"first_pb={fp}"
    if direction == "sell" and str(fp).startswith("bearish_first_pullback"):
        return 0.12, f"first_pb={fp}"

    return 0.0, ""
