"""波动率结构 + 箱体策略

基于价格行为和波动率结构的独立信号源，与传统趋势指标（SMA/Supertrend）低相关。

- RangeBoxBreakout:   箱体识别 + 突破入场（覆盖市场阶段 ①压缩 → ②突破）
- BarMomentumSurge:   大 bar 动量确认（覆盖 ②突破 → ③趋势延续）
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision

logger = logging.getLogger(__name__)


class RangeBoxBreakout:
    """箱体突破策略。

    检测 Donchian 通道收窄（价格在窄幅区间横盘），
    当 close 方向性突破 + ADX 从低位启动时入场。

    与 Chandelier Exit 高度适配：
      - 突破后趋势发展 → Chandelier 锁利
      - 假突破 → 初始 SL 紧（箱体级别），快速止损
    """

    name = "range_box_breakout"
    category = "breakout"
    required_indicators = ("donchian20", "atr14", "adx14", "boll20")
    preferred_scopes = ("confirmed",)

    regime_affinity = {
        RegimeType.TRENDING:  0.60,
        RegimeType.RANGING:   0.30,
        RegimeType.BREAKOUT:  1.00,
        RegimeType.UNCERTAIN: 0.50,
    }

    # 箱体参数（放宽后适配 XAUUSD 波动特性）
    _range_atr_threshold: float = 4.0   # Donchian 宽度 < ATR × 此值 = 箱体
    _adx_low: float = 16.0              # ADX < 此值 = 无趋势确认，不入场
    _adx_activation: float = 20.0       # ADX > 此值给额外 bonus
    _offset_threshold: float = 0.12     # BB middle 偏移 > 此值确认方向

    def evaluate(self, context: SignalContext) -> SignalDecision:
        indicators = context.indicators
        used = ["donchian20", "atr14", "adx14", "boll20"]

        donchian = indicators.get("donchian20", {})
        don_high = donchian.get("donchian_upper")
        don_low = donchian.get("donchian_lower")

        atr_val = (indicators.get("atr14") or {}).get("atr")
        adx_val = (indicators.get("adx14") or {}).get("adx")

        boll = indicators.get("boll20", {})
        bb_middle = boll.get("bb_mid")

        if don_high is None or don_low is None or atr_val is None or atr_val <= 0:
            return self._decision("hold", 0.0, "missing_indicator:donchian_or_atr", used)
        if bb_middle is None:
            return self._decision("hold", 0.0, "missing_indicator:boll20", used)

        don_width = don_high - don_low

        # ① 箱体识别：Donchian 通道收窄
        if don_width > atr_val * self._range_atr_threshold:
            return self._decision("hold", 0.0,
                f"no_range:width={don_width:.1f}>atr*{self._range_atr_threshold}", used)

        # ② 方向检测：BB middle 相对于 Donchian 中轴的偏移
        don_mid = (don_high + don_low) / 2.0
        offset_ratio = (bb_middle - don_mid) / don_width if don_width > 0 else 0.0

        if abs(offset_ratio) < self._offset_threshold:
            return self._decision("hold", 0.0,
                f"no_breakout:offset={offset_ratio:.3f}", used)

        action = "buy" if offset_ratio > 0 else "sell"

        # ③ ADX 过滤
        if adx_val is not None and adx_val < self._adx_low:
            return self._decision("hold", 0.0,
                f"adx_too_low:{adx_val:.1f}", used)

        adx_bonus = 0.0
        if adx_val is not None and adx_val > self._adx_activation:
            adx_bonus = min((adx_val - self._adx_activation) / 25.0 * 0.15, 0.15)

        # ④ Confidence
        compression = 1.0 - (don_width / (atr_val * self._range_atr_threshold))
        compression_bonus = min(compression * 0.20, 0.20)
        direction_bonus = min(abs(offset_ratio) * 0.30, 0.15)

        confidence = min(0.50 + compression_bonus + direction_bonus + adx_bonus, 0.90)

        return self._decision(action, confidence,
            f"box_break_{action}:don_w={don_width:.1f},offset={offset_ratio:.3f},adx={adx_val:.1f}",
            used, metadata={
                "don_width": don_width, "offset_ratio": offset_ratio,
                "adx": adx_val, "compression": compression,
            })

    def _decision(
        self, direction: str, confidence: float, reason: str,
        used: list[str], metadata: dict[str, Any] | None = None,
    ) -> SignalDecision:
        return SignalDecision(
            strategy=self.name, symbol="", timeframe="",
            direction=direction, confidence=confidence,
            reason=reason, used_indicators=used, metadata=metadata or {},
        )


class BarMomentumSurge:
    """大 bar 动量确认策略。

    纯价格行为策略。消费 bar_stats20 指标检测"市场突然达成共识"的时刻：
    当前 bar 实体显著大于历史均值 + 收盘在 bar 极端位置确认方向。

    与 supertrend 的相关性：低（捕捉瞬时 bar 级动量爆发，与趋势状态判断维度不同）
    """

    name = "bar_momentum_surge"
    category = "trend"
    required_indicators = ("bar_stats20", "rsi14")
    preferred_scopes = ("confirmed",)

    regime_affinity = {
        RegimeType.TRENDING:  0.90,
        RegimeType.RANGING:   0.40,
        RegimeType.BREAKOUT:  1.00,
        RegimeType.UNCERTAIN: 0.60,
    }

    _body_multiplier: float = 1.8          # body_ratio > 此值 = 大 bar
    _close_position_threshold: float = 0.70 # 收盘在 bar range 的上/下 N%
    _rsi_overbought: float = 78.0
    _rsi_oversold: float = 22.0

    def evaluate(self, context: SignalContext) -> SignalDecision:
        indicators = context.indicators
        used = ["bar_stats20", "rsi14"]

        bs = indicators.get("bar_stats20", {})
        body_ratio = bs.get("body_ratio")
        close_position = bs.get("close_position")
        is_bullish = bs.get("is_bullish")

        if body_ratio is None or close_position is None:
            return self._decision("hold", 0.0, "missing_indicator:bar_stats20", used)

        rsi_val = (indicators.get("rsi14") or {}).get("rsi")

        # ① 大 bar 检测
        if body_ratio < self._body_multiplier:
            return self._decision("hold", 0.0,
                f"small_bar:ratio={body_ratio:.2f}<{self._body_multiplier}", used)

        # ② 方向确认：收盘在 bar range 的极端位置
        if close_position >= self._close_position_threshold:
            action = "buy"
        elif close_position <= (1.0 - self._close_position_threshold):
            action = "sell"
        else:
            return self._decision("hold", 0.0,
                f"no_direction:close_pos={close_position:.2f}", used)

        # ③ RSI 过热过滤
        if rsi_val is not None:
            if action == "buy" and rsi_val > self._rsi_overbought:
                return self._decision("hold", 0.0,
                    f"rsi_overbought:{rsi_val:.1f}", used)
            if action == "sell" and rsi_val < self._rsi_oversold:
                return self._decision("hold", 0.0,
                    f"rsi_oversold:{rsi_val:.1f}", used)

        # ④ Confidence
        volume_bonus = min((body_ratio - self._body_multiplier) / 3.0 * 0.25, 0.25)
        if action == "buy":
            position_bonus = min((close_position - self._close_position_threshold) / 0.30 * 0.15, 0.15)
        else:
            position_bonus = min(((1.0 - self._close_position_threshold) - close_position) / 0.30 * 0.15, 0.15)

        confidence = min(0.50 + volume_bonus + position_bonus, 0.90)

        return self._decision(action, confidence,
            f"momentum_{action}:body_ratio={body_ratio:.2f},close_pos={close_position:.2f}"
            + (f",rsi={rsi_val:.1f}" if rsi_val else ""),
            used, metadata={
                "body_ratio": body_ratio, "close_position": close_position,
                "is_bullish": is_bullish, "rsi": rsi_val,
            })

    def _decision(
        self, direction: str, confidence: float, reason: str,
        used: list[str], metadata: dict[str, Any] | None = None,
    ) -> SignalDecision:
        return SignalDecision(
            strategy=self.name, symbol="", timeframe="",
            direction=direction, confidence=confidence,
            reason=reason, used_indicators=used, metadata=metadata or {},
        )
