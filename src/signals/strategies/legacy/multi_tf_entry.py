"""多时间框架联动入场策略 — HTF 定方向 + LTF RSI 回调入场

利用高时间框架（HTF）趋势过滤低时间框架噪声，在当前 TF 回调到
有利位置时精确入场。

支持任意 HTF/LTF 组合（通过 __init__ 参数 + signal.ini 配置）：
  - htf_trend_pullback : H1 定向 → M5/M15/M30 回调
  - htf_h4_pullback    : H4 定向 → M30/H1 回调
  - htf_m30_pullback   : M30 定向 → M5 回调

设计原则：
  1. HTF supertrend 确定趋势方向（无方向 → hold）
  2. HTF ADX 确认趋势强度（弱趋势不参与）
  3. 当前 TF 的 RSI 判断回调深度（顺势回调到中性区间 = 入场窗口）
  4. 当前 TF 的 close 相对 HTF EMA50 做顺势确认（不做逆均线交易）
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from ...evaluation.regime import RegimeType
from ...models import SignalContext, SignalDecision
from ..base import get_tf_param

logger = logging.getLogger(__name__)


class HTFTrendPullback:
    """HTF 趋势方向 + LTF RSI 回调入场策略。

    通过 [strategy_htf] 注入 HTF 指标（supertrend14/adx14/ema50），
    当前 TF 提供 RSI 和 ATR。策略实例通过 ``htf`` 参数指定方向来源 TF。

    入场逻辑（以做多为例）：
      1. HTF supertrend direction = 1（趋势向上）
      2. HTF ADX ≥ adx_min（趋势足够强）
      3. LTF close > HTF EMA50（价格在大周期均线上方，顺势确认）
      4. LTF RSI 回调到 pullback 区间 → 入场

    优势：
      - LTF 保留活跃性（RSI 回调频繁），但只做与 HTF 同向的交易
      - 入场点在回调末尾而非趋势启动点（成本更低）
    """

    name = "htf_trend_pullback"
    category = "multi_tf"
    required_indicators = ("rsi14", "atr14")
    preferred_scopes = ("confirmed",)

    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.RANGING: 0.10,
        RegimeType.BREAKOUT: 0.60,
        RegimeType.UNCERTAIN: 0.30,
    }

    # Per-TF 可调参数默认值
    _adx_min: float = 22.0
    _rsi_pullback_buy_low: float = 38.0
    _rsi_pullback_buy_high: float = 55.0
    _rsi_pullback_sell_low: float = 45.0
    _rsi_pullback_sell_high: float = 62.0
    _min_atr_filter: float = 0.5

    def __init__(
        self,
        *,
        name: str | None = None,
        htf: str = "H1",
        regime_affinity: dict[RegimeType, float] | None = None,
    ) -> None:
        if name is not None:
            self.name = name
        self._htf = htf
        if regime_affinity is not None:
            self.regime_affinity = dict(regime_affinity)

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used = ["rsi14", "atr14"]
        hold = SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="hold",
            confidence=0.0,
            reason="no_setup",
            used_indicators=used,
        )

        # ── 1. LTF 指标（当前运行 TF） ──
        rsi_data = context.indicators.get("rsi14")
        atr_data = context.indicators.get("atr14")
        if not isinstance(rsi_data, dict) or not isinstance(atr_data, dict):
            return hold

        rsi = rsi_data.get("rsi")
        atr = atr_data.get("atr")
        if rsi is None or atr is None:
            return hold

        min_atr = get_tf_param(self, "min_atr_filter", context.timeframe, self._min_atr_filter)
        if atr < min_atr:
            return hold

        # ── 2. HTF 指标（通过 [strategy_htf] 注入） ──
        htf_data = context.htf_indicators.get(self._htf, {})
        if not htf_data:
            return hold

        # HTF supertrend 方向
        htf_st = htf_data.get("supertrend14", {})
        direction_raw = htf_st.get("direction")
        if direction_raw is None:
            return hold

        try:
            htf_direction = int(direction_raw)
        except (TypeError, ValueError):
            return hold

        # direction: 1 = bullish, -1 = bearish
        if htf_direction not in (1, -1):
            return hold

        # HTF ADX 趋势强度
        htf_adx = htf_data.get("adx14", {}).get("adx")
        adx_min = get_tf_param(self, "adx_min", context.timeframe, self._adx_min)
        if htf_adx is None or htf_adx < adx_min:
            return hold

        # HTF EMA50 顺势确认
        htf_ema = htf_data.get("ema50", {}).get("ema")

        # LTF 收盘价
        close_val = context.indicators.get("rsi14", {}).get("close")
        if close_val is None:
            close_val = context.metadata.get("close")

        # ── 3. 参数查表 ──
        pb_buy_low = get_tf_param(
            self, "rsi_pullback_buy_low", context.timeframe, self._rsi_pullback_buy_low
        )
        pb_buy_high = get_tf_param(
            self, "rsi_pullback_buy_high", context.timeframe, self._rsi_pullback_buy_high
        )
        pb_sell_low = get_tf_param(
            self, "rsi_pullback_sell_low", context.timeframe, self._rsi_pullback_sell_low
        )
        pb_sell_high = get_tf_param(
            self, "rsi_pullback_sell_high", context.timeframe, self._rsi_pullback_sell_high
        )

        # ── 4. 入场逻辑 ──
        action = "hold"
        confidence = 0.0
        reason = "no_setup"
        htf_label = self._htf.lower()

        if htf_direction == 1:
            # HTF 趋势向上 → LTF 做多回调入场
            if htf_ema is not None and close_val is not None and close_val < htf_ema:
                return hold  # 价格跌破 HTF 均线，不做多

            if pb_buy_low <= rsi <= pb_buy_high:
                action = "buy"
                adx_factor = min(htf_adx / 35.0, 1.0)
                rsi_center = (pb_buy_low + pb_buy_high) / 2
                rsi_factor = 1.0 - abs(rsi - rsi_center) / (pb_buy_high - pb_buy_low)
                confidence = 0.55 + 0.35 * adx_factor * rsi_factor
                reason = (
                    f"{htf_label}_up:adx={htf_adx:.1f},rsi={rsi:.1f},"
                    f"zone=[{pb_buy_low:.0f},{pb_buy_high:.0f}]"
                )

        elif htf_direction == -1:
            # HTF 趋势向下 → LTF 做空回调入场
            if htf_ema is not None and close_val is not None and close_val > htf_ema:
                return hold  # 价格在 HTF 均线上方，不做空

            if pb_sell_low <= rsi <= pb_sell_high:
                action = "sell"
                adx_factor = min(htf_adx / 35.0, 1.0)
                rsi_center = (pb_sell_low + pb_sell_high) / 2
                rsi_factor = 1.0 - abs(rsi - rsi_center) / (pb_sell_high - pb_sell_low)
                confidence = 0.55 + 0.35 * adx_factor * rsi_factor
                reason = (
                    f"{htf_label}_down:adx={htf_adx:.1f},rsi={rsi:.1f},"
                    f"zone=[{pb_sell_low:.0f},{pb_sell_high:.0f}]"
                )

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=reason,
            used_indicators=used,
            metadata={
                "htf": self._htf,
                "htf_direction": htf_direction,
                "htf_adx": htf_adx,
                "htf_ema50": htf_ema,
                "ltf_rsi": rsi,
                "ltf_atr": atr,
                "ltf_close": close_val,
            },
        )


class DualTFMomentum:
    """双时间框架动量共振策略。

    当 HTF 和 LTF 的 supertrend 同向 + 两者 ADX 都强时入场。
    比 HTFTrendPullback 更激进（不等回调），但要求双 TF 共振确认。

    入场条件（以做多为例）：
      1. HTF supertrend direction = 1（大周期趋势向上）
      2. HTF ADX ≥ htf_adx_min（大周期趋势够强）
      3. LTF supertrend direction = 1（当前 TF 也向上 = 共振）
      4. LTF ADX ≥ ltf_adx_min（当前 TF 趋势也够强）
      5. LTF close > HTF EMA50（价格在大周期均线上方）
    """

    name = "dual_tf_momentum"
    category = "multi_tf"
    required_indicators = ("supertrend14", "adx14")
    preferred_scopes = ("confirmed",)

    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.RANGING: 0.05,
        RegimeType.BREAKOUT: 0.70,
        RegimeType.UNCERTAIN: 0.15,
    }

    _htf_adx_min: float = 22.0
    _ltf_adx_min: float = 20.0

    def __init__(
        self,
        *,
        name: str | None = None,
        htf: str = "H1",
        regime_affinity: dict[RegimeType, float] | None = None,
    ) -> None:
        if name is not None:
            self.name = name
        self._htf = htf
        if regime_affinity is not None:
            self.regime_affinity = dict(regime_affinity)

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used = ["supertrend14", "adx14"]
        hold = SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="hold",
            confidence=0.0,
            reason="no_setup",
            used_indicators=used,
        )

        # ── 1. LTF supertrend + ADX ──
        ltf_st = context.indicators.get("supertrend14", {})
        ltf_adx_data = context.indicators.get("adx14", {})
        ltf_dir_raw = ltf_st.get("direction")
        ltf_adx = ltf_adx_data.get("adx")
        if ltf_dir_raw is None or ltf_adx is None:
            return hold

        try:
            ltf_direction = int(ltf_dir_raw)
        except (TypeError, ValueError):
            return hold

        if ltf_direction not in (1, -1):
            return hold

        ltf_adx_min = get_tf_param(self, "ltf_adx_min", context.timeframe, self._ltf_adx_min)
        if ltf_adx < ltf_adx_min:
            return hold

        # ── 2. HTF supertrend + ADX ──
        htf_data = context.htf_indicators.get(self._htf, {})
        if not htf_data:
            return hold

        htf_dir_raw = htf_data.get("supertrend14", {}).get("direction")
        htf_adx = htf_data.get("adx14", {}).get("adx")
        if htf_dir_raw is None or htf_adx is None:
            return hold

        try:
            htf_direction = int(htf_dir_raw)
        except (TypeError, ValueError):
            return hold

        htf_adx_min = get_tf_param(self, "htf_adx_min", context.timeframe, self._htf_adx_min)
        if htf_adx < htf_adx_min:
            return hold

        # ── 3. 方向共振检查 ──
        if ltf_direction != htf_direction:
            return hold  # 双 TF 方向不一致 → 不入场

        # ── 4. HTF EMA50 顺势确认（可选） ──
        htf_ema = htf_data.get("ema50", {}).get("ema")
        close_val = context.metadata.get("close")
        if htf_ema is not None and close_val is not None:
            if ltf_direction == 1 and close_val < htf_ema:
                return hold
            if ltf_direction == -1 and close_val > htf_ema:
                return hold

        # ── 5. 置信度计算 ──
        action = "buy" if ltf_direction == 1 else "sell"
        # 双 ADX 强度决定置信度
        htf_factor = min(htf_adx / 35.0, 1.0)
        ltf_factor = min(ltf_adx / 35.0, 1.0)
        confidence = 0.55 + 0.35 * htf_factor * ltf_factor
        htf_label = self._htf.lower()

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=f"{htf_label}_st={htf_direction},ltf_st={ltf_direction},"
                   f"htf_adx={htf_adx:.1f},ltf_adx={ltf_adx:.1f}",
            used_indicators=used,
            metadata={
                "htf": self._htf,
                "htf_direction": htf_direction,
                "htf_adx": htf_adx,
                "ltf_direction": ltf_direction,
                "ltf_adx": ltf_adx,
            },
        )
