"""波动率结构 + 箱体 + 价格结构策略

基于价格行为和波动率结构的独立信号源，与传统趋势指标（SMA/Supertrend）低相关。

- RangeBoxBreakout:     箱体识别 + 突破入场（覆盖市场阶段 ①压缩 → ②突破）
- BarMomentumSurge:     大 bar 动量确认（覆盖 ②突破 → ③趋势延续）
- AtrRegimeShift:       波动率压缩→扩张转换入场（覆盖 ①→② 转换时刻）
- SwingStructureBreak:  高低点结构突破 + 回调入场（覆盖 ③趋势延续中的回调）
- RangeMeanReversion:   箱体内均值回归（覆盖 ④ranging 市回归中轴）
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from ...evaluation.regime import RegimeType
from ...models import SignalContext, SignalDecision
from ..base import _resolve_indicator_value, get_tf_param

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
        RegimeType.TRENDING: 0.60,
        RegimeType.RANGING: 0.30,
        RegimeType.BREAKOUT: 1.00,
        RegimeType.UNCERTAIN: 0.50,
    }

    # 箱体参数（放宽后适配 XAUUSD 波动特性）
    _range_atr_threshold: float = 4.0  # Donchian 宽度 < ATR × 此值 = 箱体
    _adx_low: float = 16.0  # ADX < 此值 = 无趋势确认，不入场
    _adx_activation: float = 20.0  # ADX > 此值给额外 bonus
    _offset_threshold: float = 0.12  # BB middle 偏移 > 此值确认方向

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
            return self._decision(
                "hold", 0.0, "missing_indicator:donchian_or_atr", used
            )
        if bb_middle is None:
            return self._decision("hold", 0.0, "missing_indicator:boll20", used)

        don_width = don_high - don_low

        # ① 箱体识别：Donchian 通道收窄
        if don_width > atr_val * self._range_atr_threshold:
            return self._decision(
                "hold",
                0.0,
                f"no_range:width={don_width:.1f}>atr*{self._range_atr_threshold}",
                used,
            )

        # ② 方向检测：BB middle 相对于 Donchian 中轴的偏移
        don_mid = (don_high + don_low) / 2.0
        offset_ratio = (bb_middle - don_mid) / don_width if don_width > 0 else 0.0

        if abs(offset_ratio) < self._offset_threshold:
            return self._decision(
                "hold", 0.0, f"no_breakout:offset={offset_ratio:.3f}", used
            )

        action = "buy" if offset_ratio > 0 else "sell"

        # ③ ADX 过滤
        if adx_val is not None and adx_val < self._adx_low:
            return self._decision("hold", 0.0, f"adx_too_low:{adx_val:.1f}", used)

        adx_bonus = 0.0
        if adx_val is not None and adx_val > self._adx_activation:
            adx_bonus = min((adx_val - self._adx_activation) / 25.0 * 0.15, 0.15)

        # ④ Confidence
        compression = 1.0 - (don_width / (atr_val * self._range_atr_threshold))
        compression_bonus = min(compression * 0.20, 0.20)
        direction_bonus = min(abs(offset_ratio) * 0.30, 0.15)

        confidence = min(0.50 + compression_bonus + direction_bonus + adx_bonus, 0.90)

        return self._decision(
            action,
            confidence,
            f"box_break_{action}:don_w={don_width:.1f},offset={offset_ratio:.3f},adx={adx_val:.1f}",
            used,
            metadata={
                "don_width": don_width,
                "offset_ratio": offset_ratio,
                "adx": adx_val,
                "compression": compression,
            },
        )

    def _decision(
        self,
        direction: str,
        confidence: float,
        reason: str,
        used: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> SignalDecision:
        return SignalDecision(
            strategy=self.name,
            symbol="",
            timeframe="",
            direction=direction,
            confidence=confidence,
            reason=reason,
            used_indicators=used,
            metadata=metadata or {},
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
        RegimeType.TRENDING: 0.90,
        RegimeType.RANGING: 0.40,
        RegimeType.BREAKOUT: 1.00,
        RegimeType.UNCERTAIN: 0.60,
    }

    _body_multiplier: float = 1.8  # body_ratio > 此值 = 大 bar
    _close_position_threshold: float = 0.70  # 收盘在 bar range 的上/下 N%
    _rsi_overbought: float = 78.0
    _rsi_oversold: float = 22.0

    def evaluate(self, context: SignalContext) -> SignalDecision:
        indicators = context.indicators
        tf = context.timeframe
        used = ["bar_stats20", "rsi14"]

        bs = indicators.get("bar_stats20", {})
        body_ratio = bs.get("body_ratio")
        close_position = bs.get("close_position")
        is_bullish = bs.get("is_bullish")

        if body_ratio is None or close_position is None:
            return self._decision("hold", 0.0, "missing_indicator:bar_stats20", used)

        rsi_val = (indicators.get("rsi14") or {}).get("rsi")

        # per-TF 参数
        body_mult = get_tf_param(self, "body_multiplier", tf, self._body_multiplier)
        close_thresh = get_tf_param(
            self, "close_position_threshold", tf, self._close_position_threshold
        )
        rsi_ob = get_tf_param(self, "rsi_overbought", tf, self._rsi_overbought)
        rsi_os = get_tf_param(self, "rsi_oversold", tf, self._rsi_oversold)

        # ① 大 bar 检测
        if body_ratio < body_mult:
            return self._decision(
                "hold", 0.0, f"small_bar:ratio={body_ratio:.2f}<{body_mult:.1f}", used
            )

        # ② 方向确认：收盘在 bar range 的极端位置
        if close_position >= close_thresh:
            action = "buy"
        elif close_position <= (1.0 - close_thresh):
            action = "sell"
        else:
            return self._decision(
                "hold", 0.0, f"no_direction:close_pos={close_position:.2f}", used
            )

        # ③ RSI 过热过滤
        if rsi_val is not None:
            if action == "buy" and rsi_val > rsi_ob:
                return self._decision(
                    "hold", 0.0, f"rsi_overbought:{rsi_val:.1f}", used
                )
            if action == "sell" and rsi_val < rsi_os:
                return self._decision("hold", 0.0, f"rsi_oversold:{rsi_val:.1f}", used)

        # ④ Confidence
        volume_bonus = min((body_ratio - body_mult) / 3.0 * 0.25, 0.25)
        if action == "buy":
            position_bonus = min((close_position - close_thresh) / 0.30 * 0.15, 0.15)
        else:
            position_bonus = min(
                ((1.0 - close_thresh) - close_position) / 0.30 * 0.15, 0.15
            )

        confidence = min(0.50 + volume_bonus + position_bonus, 0.90)

        return self._decision(
            action,
            confidence,
            f"momentum_{action}:body_ratio={body_ratio:.2f},close_pos={close_position:.2f}"
            + (f",rsi={rsi_val:.1f}" if rsi_val else ""),
            used,
            metadata={
                "body_ratio": body_ratio,
                "close_position": close_position,
                "is_bullish": is_bullish,
                "rsi": rsi_val,
            },
        )

    def _decision(
        self,
        direction: str,
        confidence: float,
        reason: str,
        used: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> SignalDecision:
        return SignalDecision(
            strategy=self.name,
            symbol="",
            timeframe="",
            direction=direction,
            confidence=confidence,
            reason=reason,
            used_indicators=used,
            metadata=metadata or {},
        )


# ---------------------------------------------------------------------------
#  辅助函数
# ---------------------------------------------------------------------------


def _make_decision(
    name: str,
    direction: str,
    confidence: float,
    reason: str,
    used: list[str],
    metadata: dict[str, Any] | None = None,
) -> SignalDecision:
    return SignalDecision(
        strategy=name,
        symbol="",
        timeframe="",
        direction=direction,
        confidence=confidence,
        reason=reason,
        used_indicators=used,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
#  ATR Regime Shift — 波动率压缩 → 扩张转换
# ---------------------------------------------------------------------------


class AtrRegimeShift:
    """波动率状态转换策略。

    捕捉 ADX 从低位上升的时刻（市场从"平静"转为"活跃"），
    同时要求 BB 宽度处于压缩态，确认近期确实横盘。

    与 RangeBoxBreakout 的区别：
      - RangeBoxBreakout：Donchian 通道收窄 + BB 偏移确认突破（价格已突破箱体）
      - AtrRegimeShift：ADX delta 上升 + BB 压缩 + DI 方向（更早介入转换时刻）

    指标依赖：adx14（ADX + plus_di + minus_di + adx_d3）、boll20（BB 宽度）、atr14
    """

    name = "atr_regime_shift"
    category = "breakout"
    required_indicators = ("adx14", "boll20", "atr14")
    preferred_scopes = ("confirmed",)

    regime_affinity = {
        RegimeType.TRENDING: 0.50,
        RegimeType.RANGING: 0.40,
        RegimeType.BREAKOUT: 1.00,
        RegimeType.UNCERTAIN: 0.70,
    }

    _adx_ceiling: float = 25.0  # ADX > 此值 = 趋势已确立，不是"转换"
    _adx_floor: float = 14.0  # ADX < 此值 = 太弱，无方向性
    _adx_d3_min: float = 2.0  # ADX 3-bar delta 最小上升速度
    _bb_narrow_pct: float = 0.012  # BB 宽度 / BB mid < 此值 = 压缩态
    _di_diff_min: float = 5.0  # |DI+ - DI-| 最小差值确认方向

    def evaluate(self, context: SignalContext) -> SignalDecision:
        indicators = context.indicators
        tf = context.timeframe
        used = ["adx14", "boll20", "atr14"]

        adx_data = indicators.get("adx14", {})
        adx_val = adx_data.get("adx")
        plus_di = adx_data.get("plus_di")
        minus_di = adx_data.get("minus_di")
        adx_d3 = adx_data.get("adx_d3")

        boll = indicators.get("boll20", {})
        bb_upper = boll.get("bb_upper")
        bb_lower = boll.get("bb_lower")
        bb_mid = boll.get("bb_mid")

        if adx_val is None or plus_di is None or minus_di is None:
            return _make_decision(self.name, "hold", 0.0, "missing:adx14", used)
        if bb_upper is None or bb_lower is None or bb_mid is None or bb_mid <= 0:
            return _make_decision(self.name, "hold", 0.0, "missing:boll20", used)

        adx_ceil = get_tf_param(self, "adx_ceiling", tf, self._adx_ceiling)
        adx_flr = get_tf_param(self, "adx_floor", tf, self._adx_floor)
        d3_min = get_tf_param(self, "adx_d3_min", tf, self._adx_d3_min)
        bb_narrow = get_tf_param(self, "bb_narrow_pct", tf, self._bb_narrow_pct)
        di_diff = get_tf_param(self, "di_diff_min", tf, self._di_diff_min)

        # ① ADX 在"低→中"区间
        if adx_val > adx_ceil:
            return _make_decision(
                self.name,
                "hold",
                0.0,
                f"adx_too_high:{adx_val:.1f}>{adx_ceil:.0f}",
                used,
            )
        if adx_val < adx_flr:
            return _make_decision(
                self.name, "hold", 0.0, f"adx_too_low:{adx_val:.1f}<{adx_flr:.0f}", used
            )

        # ② ADX 在上升
        if adx_d3 is None or adx_d3 < d3_min:
            return _make_decision(
                self.name, "hold", 0.0, f"adx_not_rising:d3={adx_d3}", used
            )

        # ③ BB 宽度确认压缩
        bb_width_pct = (bb_upper - bb_lower) / bb_mid
        if bb_width_pct > bb_narrow:
            return _make_decision(
                self.name,
                "hold",
                0.0,
                f"no_compression:bb_pct={bb_width_pct:.4f}>{bb_narrow:.3f}",
                used,
            )

        # ④ DI 方向
        di_spread = plus_di - minus_di
        if abs(di_spread) < di_diff:
            return _make_decision(
                self.name, "hold", 0.0, f"no_direction:di_diff={di_spread:.1f}", used
            )

        action = "buy" if di_spread > 0 else "sell"

        # ⑤ Confidence
        rise_bonus = min((adx_d3 - d3_min) / 6.0 * 0.20, 0.20)
        di_bonus = min((abs(di_spread) - di_diff) / 20.0 * 0.15, 0.15)
        compress_bonus = min((bb_narrow - bb_width_pct) / bb_narrow * 0.10, 0.10)

        confidence = min(0.50 + rise_bonus + di_bonus + compress_bonus, 0.90)

        return _make_decision(
            self.name,
            action,
            confidence,
            f"regime_shift_{action}:adx={adx_val:.1f},d3={adx_d3:.1f},"
            f"di={di_spread:+.1f},bb_pct={bb_width_pct:.4f}",
            used,
            metadata={
                "adx": adx_val,
                "adx_d3": adx_d3,
                "plus_di": plus_di,
                "minus_di": minus_di,
                "bb_width_pct": bb_width_pct,
            },
        )


# ---------------------------------------------------------------------------
#  Swing Structure Break — 高低点结构突破
# ---------------------------------------------------------------------------


class SwingStructureBreak:
    """Swing 结构突破策略。

    检测价格突破近期 swing high/low 的结构性突破：
      - close > 20-bar high → BUY（突破上方阻力）
      - close < 20-bar low → SELL（跌破下方支撑）

    使用 Donchian 通道边界作为 swing 点代理，
    结合 RSI 过滤（非极端区才入场）和 ADX 趋势确认。

    与趋势策略的差异：不依赖 MA/Supertrend 状态，纯价格结构判断。
    """

    name = "swing_structure_break"
    category = "trend"
    required_indicators = ("donchian20", "atr14", "rsi14", "adx14")
    preferred_scopes = ("confirmed",)

    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.RANGING: 0.15,
        RegimeType.BREAKOUT: 0.70,
        RegimeType.UNCERTAIN: 0.40,
    }

    _close_breakout_pct: float = 0.002  # close 超过 swing 点的最小比例
    _rsi_lower: float = 30.0
    _rsi_upper: float = 70.0
    _adx_min: float = 18.0

    def evaluate(self, context: SignalContext) -> SignalDecision:
        indicators = context.indicators
        tf = context.timeframe
        used = ["donchian20", "atr14", "rsi14", "adx14"]

        donchian = indicators.get("donchian20", {})
        don_high = donchian.get("donchian_upper")
        don_low = donchian.get("donchian_lower")
        close = donchian.get("close")

        atr_val = (indicators.get("atr14") or {}).get("atr")
        rsi_val = (indicators.get("rsi14") or {}).get("rsi")
        adx_val = (indicators.get("adx14") or {}).get("adx")

        if don_high is None or don_low is None or close is None:
            return _make_decision(self.name, "hold", 0.0, "missing:donchian20", used)
        if atr_val is None or atr_val <= 0:
            return _make_decision(self.name, "hold", 0.0, "missing:atr14", used)

        breakout_pct = get_tf_param(
            self, "close_breakout_pct", tf, self._close_breakout_pct
        )
        rsi_lo = get_tf_param(self, "rsi_lower", tf, self._rsi_lower)
        rsi_hi = get_tf_param(self, "rsi_upper", tf, self._rsi_upper)
        adx_min = get_tf_param(self, "adx_min", tf, self._adx_min)

        # ① ADX 过滤
        if adx_val is not None and adx_val < adx_min:
            return _make_decision(
                self.name, "hold", 0.0, f"adx_low:{adx_val:.1f}<{adx_min:.0f}", used
            )

        # ② 结构突破检测
        don_range = don_high - don_low
        if don_range <= 0:
            return _make_decision(self.name, "hold", 0.0, "flat_range", used)

        up_break = (close - don_high) / don_high if don_high > 0 else 0.0
        dn_break = (don_low - close) / don_low if don_low > 0 else 0.0

        if up_break >= breakout_pct:
            action = "buy"
            break_magnitude = up_break
        elif dn_break >= breakout_pct:
            action = "sell"
            break_magnitude = dn_break
        else:
            return _make_decision(
                self.name,
                "hold",
                0.0,
                f"no_break:up={up_break:.4f},dn={dn_break:.4f}",
                used,
            )

        # ③ RSI 过热过滤
        if rsi_val is not None:
            if action == "buy" and rsi_val > rsi_hi:
                return _make_decision(
                    self.name, "hold", 0.0, f"rsi_overbought:{rsi_val:.1f}", used
                )
            if action == "sell" and rsi_val < rsi_lo:
                return _make_decision(
                    self.name, "hold", 0.0, f"rsi_oversold:{rsi_val:.1f}", used
                )

        # ④ Confidence
        magnitude_bonus = min(break_magnitude / 0.01 * 0.15, 0.20)
        adx_bonus = 0.0
        if adx_val is not None and adx_val > 23.0:
            adx_bonus = min((adx_val - 23.0) / 20.0 * 0.15, 0.15)
        rsi_bonus = 0.0
        if rsi_val is not None:
            rsi_center_dist = abs(rsi_val - 50.0)
            if rsi_center_dist < 15.0:
                rsi_bonus = (15.0 - rsi_center_dist) / 15.0 * 0.05

        confidence = min(0.50 + magnitude_bonus + adx_bonus + rsi_bonus, 0.90)

        return _make_decision(
            self.name,
            action,
            confidence,
            f"swing_{action}:break={break_magnitude:.4f},adx={adx_val},rsi={rsi_val}",
            used,
            metadata={
                "break_magnitude": break_magnitude,
                "don_high": don_high,
                "don_low": don_low,
                "close": close,
                "adx": adx_val,
                "rsi": rsi_val,
            },
        )


# ---------------------------------------------------------------------------
#  Range Mean Reversion — 箱体内均值回归
# ---------------------------------------------------------------------------


class RangeMeanReversion:
    """箱体内均值回归策略。

    当市场处于 ranging 状态（ADX 低、BB 宽度适中），价格触及 BB 带边缘时
    反向入场，目标回归 BB 中轴。

    与 RSI Reversion 的差异：
      - RSI Reversion 依赖动量振荡器（数学抽象）
      - 本策略直接基于价格与统计通道的物理距离

    与 Chandelier Exit 适配：
      - 回归交易 SL 紧（BB 外 + ATR margin），快速认错
      - 盈利目标明确（BB mid），锁利快
    """

    name = "range_mean_reversion"
    category = "reversion"
    required_indicators = ("boll20", "atr14", "adx14", "rsi14")
    preferred_scopes = ("confirmed", "intrabar")

    regime_affinity = {
        RegimeType.TRENDING: 0.10,
        RegimeType.RANGING: 1.00,
        RegimeType.BREAKOUT: 0.15,
        RegimeType.UNCERTAIN: 0.60,
    }

    _adx_max: float = 22.0
    _bb_touch_pct: float = 0.90
    _bb_touch_pct_low: float = 0.10
    _rsi_extreme_high: float = 75.0
    _rsi_extreme_low: float = 25.0
    _min_bb_width_atr: float = 1.5

    def evaluate(self, context: SignalContext) -> SignalDecision:
        indicators = context.indicators
        tf = context.timeframe
        used = ["boll20", "atr14", "adx14", "rsi14"]

        boll = indicators.get("boll20", {})
        bb_upper = boll.get("bb_upper")
        bb_lower = boll.get("bb_lower")
        bb_mid = boll.get("bb_mid")
        close = boll.get("close")

        atr_val = (indicators.get("atr14") or {}).get("atr")
        adx_val = (indicators.get("adx14") or {}).get("adx")
        rsi_val = (indicators.get("rsi14") or {}).get("rsi")

        if bb_upper is None or bb_lower is None or bb_mid is None or close is None:
            return _make_decision(self.name, "hold", 0.0, "missing:boll20", used)
        if atr_val is None or atr_val <= 0:
            return _make_decision(self.name, "hold", 0.0, "missing:atr14", used)

        adx_max = get_tf_param(self, "adx_max", tf, self._adx_max)
        touch_hi = get_tf_param(self, "bb_touch_pct", tf, self._bb_touch_pct)
        touch_lo = get_tf_param(self, "bb_touch_pct_low", tf, self._bb_touch_pct_low)
        min_width = get_tf_param(self, "min_bb_width_atr", tf, self._min_bb_width_atr)

        # ① ADX 过滤
        if adx_val is not None and adx_val > adx_max:
            return _make_decision(
                self.name,
                "hold",
                0.0,
                f"trending:adx={adx_val:.1f}>{adx_max:.0f}",
                used,
            )

        # ② BB 宽度检查
        bb_width = bb_upper - bb_lower
        if bb_width < atr_val * min_width:
            return _make_decision(
                self.name,
                "hold",
                0.0,
                f"too_narrow:bb_w={bb_width:.1f}<atr*{min_width:.1f}",
                used,
            )

        # ③ 价格在 BB 中的位置
        bb_position = (close - bb_lower) / bb_width if bb_width > 0 else 0.5

        if bb_position >= touch_hi:
            action = "sell"
        elif bb_position <= touch_lo:
            action = "buy"
        else:
            return _make_decision(
                self.name, "hold", 0.0, f"mid_zone:bb_pos={bb_position:.2f}", used
            )

        # ④ Confidence
        if action == "sell":
            edge_bonus = min((bb_position - touch_hi) / (1.0 - touch_hi) * 0.20, 0.20)
        else:
            edge_bonus = (
                min((touch_lo - bb_position) / touch_lo * 0.20, 0.20)
                if touch_lo > 0
                else 0.0
            )

        rsi_bonus = 0.0
        if rsi_val is not None:
            if action == "buy" and rsi_val < self._rsi_extreme_low:
                rsi_bonus = min((self._rsi_extreme_low - rsi_val) / 25.0 * 0.10, 0.10)
            elif action == "sell" and rsi_val > self._rsi_extreme_high:
                rsi_bonus = min((rsi_val - self._rsi_extreme_high) / 25.0 * 0.10, 0.10)

        adx_bonus = 0.0
        if adx_val is not None and adx_val < 18.0:
            adx_bonus = min((18.0 - adx_val) / 10.0 * 0.05, 0.05)

        confidence = min(0.50 + edge_bonus + rsi_bonus + adx_bonus, 0.90)

        return _make_decision(
            self.name,
            action,
            confidence,
            f"revert_{action}:bb_pos={bb_position:.2f},adx={adx_val},rsi={rsi_val}",
            used,
            metadata={
                "bb_position": bb_position,
                "bb_width": bb_width,
                "adx": adx_val,
                "rsi": rsi_val,
            },
        )
