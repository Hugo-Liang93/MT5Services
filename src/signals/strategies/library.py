from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional, Protocol

from ..models import SignalContext, SignalDecision
from ..evaluation.regime import RegimeType

logger = logging.getLogger(__name__)


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


class SmaTrendStrategy:
    """Simple trend signal based on fast/slow SMA relation.

    Uses bar-close snapshots only.  SMA/EMA crossovers are meaningful only
    when a bar has *closed* — intrabar MA values oscillate continuously as
    the live price moves, generating excessive false crossover noise.
    """

    name = "sma_trend"
    required_indicators = ("sma20", "ema50")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING:  1.00,  # MA 金叉/死叉在趋势中最可靠
        RegimeType.RANGING:   0.20,  # 震荡市 MA 频繁交叉，产生大量虚假信号
        RegimeType.BREAKOUT:  0.50,  # 突破初期 MA 尚未跟上，信号滞后
        RegimeType.UNCERTAIN: 0.50,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        fast, fast_name = _resolve_indicator_value(
            context.indicators,
            (
                ("sma_fast", "value"),
                ("sma_fast", "sma"),
                ("sma20", "sma"),
                ("sma20", "value"),
            ),
        )
        slow, slow_name = _resolve_indicator_value(
            context.indicators,
            (
                ("sma_slow", "value"),
                ("sma_slow", "sma"),
                ("ema50", "ema"),
                ("ema50", "value"),
            ),
        )
        used_indicators = [name for name in (fast_name, slow_name) if name]
        if fast is None or slow is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used_indicators or ["sma20", "ema50"],
            )

        spread = fast - slow
        if spread > 0:
            action = "buy"
        elif spread < 0:
            action = "sell"
        else:
            action = "hold"

        relative_spread = spread / slow if slow else 0.0
        confidence = min(abs(relative_spread) * 100, 1.0)
        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=f"sma_spread={spread:.6f},relative={relative_spread:.6f}",
            used_indicators=used_indicators or ["sma20", "ema50"],
            metadata={
                "spread": spread,
                "relative_spread": relative_spread,
                "fast_indicator": fast_name or "sma20",
                "slow_indicator": slow_name or "ema50",
            },
        )


class RsiReversionStrategy:
    """Mean reversion signal based on RSI overbought/oversold zones.

    Receives both intrabar and confirmed snapshots.  RSI extreme readings
    (≤30 oversold, ≥70 overbought) are meaningful in real time — the deepest
    extreme often occurs mid-bar before price reverts.  Bar-close confirmation
    verifies the reading was sustained through candle close.
    """

    name = "rsi_reversion"
    required_indicators = ("rsi14",)
    preferred_scopes = ("intrabar", "confirmed")
    regime_affinity = {
        RegimeType.TRENDING:  0.25,  # 强趋势中 RSI 可长时间维持极值，逆势信号危险
        RegimeType.RANGING:   1.00,  # 震荡区间均值回归是 RSI 的核心应用场景
        RegimeType.BREAKOUT:  0.35,  # 突破初期 RSI 极值往往持续，不宜逆势
        RegimeType.UNCERTAIN: 0.60,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        rsi_value, rsi_name = _resolve_indicator_value(
            context.indicators,
            (
                ("rsi", "value"),
                ("rsi", "rsi"),
                ("rsi14", "rsi"),
                ("rsi14", "value"),
            ),
        )
        if rsi_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:rsi",
                used_indicators=[rsi_name] if rsi_name else ["rsi14"],
            )

        rsi = rsi_value
        if rsi <= 30:
            action = "buy"
            confidence = min((30 - rsi) / 30 + 0.4, 1.0)
        elif rsi >= 70:
            action = "sell"
            confidence = min((rsi - 70) / 30 + 0.4, 1.0)
        else:
            action = "hold"
            confidence = 0.2

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=f"rsi={rsi:.2f}",
            used_indicators=[rsi_name] if rsi_name else ["rsi14"],
            metadata={"rsi": rsi, "rsi_indicator": rsi_name or "rsi14"},
        )


class BollingerBreakoutStrategy:
    """Mean reversion signal based on Bollinger Band breakout.

    Price touching lower band -> buy (expect reversion to mean).
    Price touching upper band -> sell (expect reversion to mean).
    Band width (squeeze) is used to boost confidence on breakouts after compression.

    Receives both intrabar and confirmed snapshots.  Bollinger Bands are
    computed from historical closes so the band levels are stable intrabar;
    only the live close price varies.  Price touching the bands is a real-time
    event — waiting for bar close often means the price has already recovered
    to the middle band, losing the entry edge.
    """

    name = "bollinger_breakout"
    required_indicators = ("boll20",)
    preferred_scopes = ("intrabar", "confirmed")
    regime_affinity = {
        RegimeType.TRENDING:  0.30,  # 趋势中价格走布林带边缘属正常，触带≠反转
        RegimeType.RANGING:   0.85,  # 震荡市触及上下轨是典型均值回归机会
        RegimeType.BREAKOUT:  1.00,  # 价格突破布林带 = 波动率扩张主要信号
        RegimeType.UNCERTAIN: 0.60,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        upper, upper_name = _resolve_indicator_value(
            context.indicators,
            (
                ("boll20", "bb_upper"),
                ("bollinger20", "bb_upper"),
                ("bollinger", "bb_upper"),
            ),
        )
        lower, lower_name = _resolve_indicator_value(
            context.indicators,
            (
                ("boll20", "bb_lower"),
                ("bollinger20", "bb_lower"),
                ("bollinger", "bb_lower"),
            ),
        )
        mid, _ = _resolve_indicator_value(
            context.indicators,
            (
                ("boll20", "bb_mid"),
                ("bollinger20", "bb_mid"),
                ("bollinger", "bb_mid"),
            ),
        )
        close_value = self._get_close(context)

        used = [n for n in (upper_name, lower_name) if n]
        if upper is None or lower is None or mid is None or close_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used or ["boll20"],
            )

        band_width = (upper - lower) / mid if mid else 0.0
        squeeze_bonus = max(0.0, 0.3 - band_width * 10) if band_width < 0.03 else 0.0

        if close_value <= lower:
            action = "buy"
            penetration = (lower - close_value) / (upper - lower) if (upper - lower) else 0
            confidence = min(0.5 + penetration + squeeze_bonus, 1.0)
        elif close_value >= upper:
            action = "sell"
            penetration = (close_value - upper) / (upper - lower) if (upper - lower) else 0
            confidence = min(0.5 + penetration + squeeze_bonus, 1.0)
        else:
            action = "hold"
            confidence = 0.1

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=f"bb_close={close_value:.2f},upper={upper:.2f},lower={lower:.2f}",
            used_indicators=used or ["boll20"],
            metadata={
                "close": close_value,
                "bb_upper": upper,
                "bb_lower": lower,
                "bb_mid": mid,
                "band_width": band_width,
                "squeeze_bonus": squeeze_bonus,
            },
        )

    @staticmethod
    def _get_close(context: SignalContext) -> Optional[float]:
        """Extract close price from the boll20 indicator payload.

        bollinger() now returns {"bb_mid", "bb_upper", "bb_lower", "close"}.
        The "close" field is closes[-1] computed inside the indicator, which is
        the same price used to determine band position — guaranteed to be
        consistent with bb_upper/bb_lower.
        """
        for indicator_name in ("boll20", "bollinger20", "bollinger"):
            payload = context.indicators.get(indicator_name)
            if isinstance(payload, dict):
                val = payload.get("close")
                if val is not None:
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        continue
        return None


class MultiTimeframeConfirmStrategy:
    """Confirms signals when direction aligns across timeframes.

    Uses existing indicator snapshots to check if the same directional signal
    exists on a higher timeframe. Only produces signals when lower TF and
    higher TF agree on direction.

    Uses bar-close snapshots only.  The higher-timeframe direction is read from
    HTFStateCache, which is populated by consensus signals from the higher TF.

    ## 注入方式

    HTFStateCache 通过构造参数注入：
        strategy = MultiTimeframeConfirmStrategy(htf_cache=htf_cache)
    HTFStateCache.attach(runtime) 后会自动监听 consensus 信号并填充缓存。
    """

    name = "mtf_confirm"
    required_indicators = ("sma20", "ema50")
    preferred_scopes = ("confirmed",)
    # MTF 对齐在趋势市效果最佳（双重确认）；震荡市 HTF 方向不稳定
    regime_affinity = {
        RegimeType.TRENDING:  1.00,  # 趋势市 LTF+HTF 双向一致，最可靠
        RegimeType.RANGING:   0.30,  # 震荡市 HTF 方向频繁反转，信号不稳
        RegimeType.BREAKOUT:  0.70,  # 突破初期 HTF 确认方向有价值
        RegimeType.UNCERTAIN: 0.50,
    }

    def __init__(
        self,
        *,
        state_reader: Optional[Any] = None,
        htf_cache: Optional[Any] = None,
    ):
        self._state_reader = state_reader
        self._htf_cache = htf_cache
        if htf_cache is None and state_reader is None:
            logger.warning(
                "MultiTimeframeConfirmStrategy: 未传入 htf_cache 或 state_reader，"
                "_get_htf_direction() 将始终返回 None，策略将固定输出 hold(0.1)。"
                "生产环境请通过 htf_cache= 参数注入 HTFStateCache 实例。"
            )

    def evaluate(self, context: SignalContext) -> SignalDecision:
        fast, fast_name = _resolve_indicator_value(
            context.indicators,
            (("sma_fast", "value"), ("sma_fast", "sma"), ("sma20", "sma"), ("sma20", "value")),
        )
        slow, slow_name = _resolve_indicator_value(
            context.indicators,
            (("sma_slow", "value"), ("sma_slow", "sma"), ("ema50", "ema"), ("ema50", "value")),
        )
        used = [n for n in (fast_name, slow_name) if n]
        if fast is None or slow is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used or ["sma20", "ema50"],
            )

        local_direction = "buy" if fast > slow else ("sell" if fast < slow else "hold")
        htf_direction = self._get_htf_direction(context)

        if local_direction == "hold" or htf_direction is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.1,
                reason=f"local={local_direction},htf={htf_direction or 'unknown'}",
                used_indicators=used or ["sma20", "ema50"],
                metadata={"local_direction": local_direction, "htf_direction": htf_direction},
            )

        if local_direction == htf_direction:
            relative_spread = (fast - slow) / slow if slow else 0
            confidence = min(abs(relative_spread) * 150, 1.0)
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action=local_direction,
                confidence=max(confidence, 0.6),
                reason=f"mtf_aligned:{local_direction},htf={htf_direction}",
                used_indicators=used or ["sma20", "ema50"],
                metadata={
                    "local_direction": local_direction,
                    "htf_direction": htf_direction,
                    "relative_spread": relative_spread,
                },
            )

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action="hold",
            confidence=0.15,
            reason=f"mtf_conflict:local={local_direction},htf={htf_direction}",
            used_indicators=used or ["sma20", "ema50"],
            metadata={"local_direction": local_direction, "htf_direction": htf_direction},
        )

    def _get_htf_direction(self, context: SignalContext) -> Optional[str]:
        """Read higher timeframe direction from HTFStateCache, metadata, or state_reader."""
        # 优先从 metadata 直接读取（测试/手动注入场景）
        htf = context.metadata.get("htf_direction")
        if htf in ("buy", "sell", "hold"):
            return htf
        # 从 HTFStateCache 读取（生产路径）
        if self._htf_cache is not None:
            try:
                direction = self._htf_cache.get_htf_direction(
                    context.symbol, context.timeframe
                )
                if direction is not None:
                    return direction
            except Exception:
                pass
        # 兼容旧的 state_reader 注入方式
        if self._state_reader is None:
            return None
        try:
            states = self._state_reader
            htf_key = context.metadata.get("htf_key")
            if htf_key and isinstance(states, dict):
                state = states.get(htf_key)
                if state:
                    confirmed = getattr(state, "confirmed_state", "idle")
                    if "buy" in confirmed:
                        return "buy"
                    if "sell" in confirmed:
                        return "sell"
        except Exception:
            pass
        return None


class SupertrendStrategy:
    """基于 Supertrend 的趋势跟踪策略。

    Supertrend 是黄金日内交易最有效的趋势指标之一：
    - 当价格从 Supertrend 下方突破至上方（direction -1→1）→ BUY
    - 当价格从 Supertrend 上方跌破至下方（direction 1→-1）→ SELL
    - 结合 ADX 过滤，只在趋势明确时（ADX > adx_threshold）发出信号

    仅在 bar 收盘时评估（confirmed scope），Supertrend 方向在 bar 中间频繁
    翻转会产生大量假信号。
    """

    name = "supertrend"
    required_indicators = ("supertrend14", "adx14")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING:  1.00,  # Supertrend 专为趋势行情设计
        RegimeType.RANGING:   0.30,  # 震荡市 Supertrend 方向频繁翻转，假信号多
        RegimeType.BREAKOUT:  0.60,  # 突破时可以确认方向，但信号有一定滞后
        RegimeType.UNCERTAIN: 0.55,
    }

    def __init__(self, *, adx_threshold: float = 20.0) -> None:
        self._adx_threshold = adx_threshold

    def evaluate(self, context: SignalContext) -> SignalDecision:
        st_direction, st_name = _resolve_indicator_value(
            context.indicators,
            (
                ("supertrend14", "direction"),
                ("supertrend", "direction"),
            ),
        )
        st_value, _ = _resolve_indicator_value(
            context.indicators,
            (
                ("supertrend14", "supertrend"),
                ("supertrend", "supertrend"),
            ),
        )
        adx_value, adx_name = _resolve_indicator_value(
            context.indicators,
            (
                ("adx14", "adx"),
                ("adx", "adx"),
            ),
        )
        used = [n for n in (st_name, adx_name) if n]

        if st_direction is None or st_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:supertrend",
                used_indicators=used or ["supertrend14"],
            )

        # ADX 过滤：趋势强度不足时不交易（减少震荡行情假信号）
        adx = adx_value if adx_value is not None else 0.0
        if adx < self._adx_threshold:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.1,
                reason=f"adx_too_low:{adx:.1f}<{self._adx_threshold}",
                used_indicators=used or ["supertrend14", "adx14"],
                metadata={"adx": adx, "st_direction": st_direction},
            )

        # direction: 1.0 = 多头, -1.0 = 空头
        if st_direction > 0:
            action = "buy"
        elif st_direction < 0:
            action = "sell"
        else:
            action = "hold"

        # 置信度：ADX 越强信号越可靠，上限 1.0
        adx_confidence = min((adx - self._adx_threshold) / 30.0 + 0.6, 1.0)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=adx_confidence,
            reason=f"supertrend_direction={st_direction:.0f},adx={adx:.1f}",
            used_indicators=used or ["supertrend14", "adx14"],
            metadata={
                "supertrend": st_value,
                "direction": st_direction,
                "adx": adx,
            },
        )


class StochRsiStrategy:
    """基于 Stochastic RSI 的超买超卖策略。

    Stochastic RSI 比普通 RSI 更敏感，能提前捕捉黄金动量耗尽：
    - StochRSI_K < 20 且 K > D（死叉结束，动量向上）→ BUY
    - StochRSI_K > 80 且 K < D（金叉结束，动量向下）→ SELL
    - 过滤掉震荡信号：只有 K 线与 D 线有明确交叉方向时才发信号

    支持 intrabar 快照——Stochastic RSI 的极值常发生在 bar 中间。
    """

    name = "stoch_rsi"
    required_indicators = ("stoch_rsi14",)
    preferred_scopes = ("intrabar", "confirmed")
    regime_affinity = {
        RegimeType.TRENDING:  0.25,  # 趋势中随机指标长时间嵌顶/嵌底，逆势陷阱
        RegimeType.RANGING:   1.00,  # 振荡市 K/D 交叉的核心使用场景
        RegimeType.BREAKOUT:  0.30,  # 突破前 StochRSI 往往已超买/超卖，不可逆势
        RegimeType.UNCERTAIN: 0.60,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        k_value, k_name = _resolve_indicator_value(
            context.indicators,
            (
                ("stoch_rsi14", "stoch_rsi_k"),
                ("stoch_rsi", "stoch_rsi_k"),
            ),
        )
        d_value, _ = _resolve_indicator_value(
            context.indicators,
            (
                ("stoch_rsi14", "stoch_rsi_d"),
                ("stoch_rsi", "stoch_rsi_d"),
            ),
        )
        used = [k_name] if k_name else ["stoch_rsi14"]

        if k_value is None or d_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:stoch_rsi",
                used_indicators=used,
            )

        k = k_value
        d = d_value

        if k < 20 and k > d:
            # 超卖区域，K 线上穿 D 线：动量由空转多
            action = "buy"
            depth = (20.0 - k) / 20.0          # 越深越强
            cross_strength = min((k - d) / 5.0, 1.0)
            confidence = min(0.5 + depth * 0.3 + cross_strength * 0.2, 1.0)
        elif k > 80 and k < d:
            # 超买区域，K 线下穿 D 线：动量由多转空
            action = "sell"
            depth = (k - 80.0) / 20.0
            cross_strength = min((d - k) / 5.0, 1.0)
            confidence = min(0.5 + depth * 0.3 + cross_strength * 0.2, 1.0)
        else:
            action = "hold"
            confidence = 0.15

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=f"stoch_rsi_k={k:.2f},d={d:.2f}",
            used_indicators=used,
            metadata={"stoch_rsi_k": k, "stoch_rsi_d": d},
        )


class MacdMomentumStrategy:
    """基于 MACD 柱状图动量的趋势策略。

    黄金趋势行情中 MACD 柱状图的方向翻转是重要的动量确认信号：
    - hist 由负转正（从负值区域向上穿越零轴）→ BUY
    - hist 由正转负（从正值区域向下穿越零轴）→ SELL
    - hist 绝对值越大，动量越强，置信度越高
    - 附加过滤：macd 线与 signal 线方向一致时才发信号

    仅在 bar 收盘时评估（confirmed scope）。
    """

    name = "macd_momentum"
    required_indicators = ("macd",)
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING:  1.00,  # MACD 柱状图反映趋势动量，趋势中最准确
        RegimeType.RANGING:   0.30,  # 震荡市 MACD 来回交叉，产生频繁假信号
        RegimeType.BREAKOUT:  0.70,  # 突破初期 MACD 柱常领先确认方向
        RegimeType.UNCERTAIN: 0.55,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        macd_val, macd_name = _resolve_indicator_value(
            context.indicators,
            (
                ("macd", "macd"),
                ("macd12_26_9", "macd"),
            ),
        )
        signal_val, _ = _resolve_indicator_value(
            context.indicators,
            (
                ("macd", "signal"),
                ("macd12_26_9", "signal"),
            ),
        )
        hist_val, _ = _resolve_indicator_value(
            context.indicators,
            (
                ("macd", "hist"),
                ("macd12_26_9", "hist"),
            ),
        )
        used = [macd_name] if macd_name else ["macd"]

        if macd_val is None or signal_val is None or hist_val is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:macd",
                used_indicators=used,
            )

        hist = hist_val
        macd_line = macd_val
        signal_line = signal_val

        # MACD 线在 signal 线上方 = 多头动量，反之空头
        if hist > 0 and macd_line > signal_line:
            action = "buy"
            # 柱状图强度（归一化：用柱状图值除以较大的 MACD 值幅度）
            magnitude = abs(hist) / (abs(macd_line) + 1e-10)
            confidence = min(0.45 + min(magnitude * 2.0, 0.45), 0.9)
        elif hist < 0 and macd_line < signal_line:
            action = "sell"
            magnitude = abs(hist) / (abs(macd_line) + 1e-10)
            confidence = min(0.45 + min(magnitude * 2.0, 0.45), 0.9)
        else:
            action = "hold"
            confidence = 0.1

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=f"macd={macd_val:.4f},signal={signal_val:.4f},hist={hist_val:.4f}",
            used_indicators=used,
            metadata={
                "macd": macd_val,
                "signal": signal_val,
                "hist": hist_val,
            },
        )


class KeltnerBollingerSqueezeStrategy:
    """Keltner-Bollinger 挤压突破策略（John Carter Squeeze 变体）。

    核心逻辑：
    - 挤压（Squeeze）= 布林带完全在肯特纳通道内部（波动率极度压缩）
    - 突破（Breakout）= close 突破布林带上/下轨
    - 挤压后的突破具有更高的爆发动能，置信度加成

    适合黄金的原因：
    XAUUSD 在经济数据发布前或亚洲时段末尾经常进入 Squeeze 状态，
    欧洲开盘或数据落地时形成方向性突破，捕捉这类行情收益较高。

    支持 intrabar：带宽是历史 close 计算的，实时稳定，
    close 突破带宽是实时事件，intrabar 可以比 bar 收盘提前发现。
    """

    name = "keltner_bb_squeeze"
    required_indicators = ("boll20", "keltner20")
    preferred_scopes = ("intrabar", "confirmed")
    regime_affinity = {
        RegimeType.TRENDING:  0.35,  # 趋势中 Squeeze 出现频率低，信号意义有限
        RegimeType.RANGING:   0.55,  # 震荡末期常出现 Squeeze，可提前布局
        RegimeType.BREAKOUT:  1.00,  # Squeeze 释放正是这个策略的核心场景
        RegimeType.UNCERTAIN: 0.65,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        bb_upper, bb_name = _resolve_indicator_value(
            context.indicators, (("boll20", "bb_upper"),)
        )
        bb_lower, _ = _resolve_indicator_value(
            context.indicators, (("boll20", "bb_lower"),)
        )
        bb_mid, _ = _resolve_indicator_value(
            context.indicators, (("boll20", "bb_mid"),)
        )
        bb_close, _ = _resolve_indicator_value(
            context.indicators, (("boll20", "close"),)
        )
        kc_upper, kc_name = _resolve_indicator_value(
            context.indicators, (("keltner20", "kc_upper"),)
        )
        kc_lower, _ = _resolve_indicator_value(
            context.indicators, (("keltner20", "kc_lower"),)
        )
        used = [n for n in (bb_name, kc_name) if n]

        if any(v is None for v in (bb_upper, bb_lower, bb_mid, bb_close, kc_upper, kc_lower)):
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used or ["boll20", "keltner20"],
            )

        close = bb_close
        band_range = bb_upper - bb_lower  # type: ignore[operator]
        # 挤压：布林带完全在肯特纳通道内（BB 更窄，波动率压缩）
        is_squeeze = bb_upper < kc_upper and bb_lower > kc_lower  # type: ignore[operator]
        squeeze_bonus = 0.2 if is_squeeze else 0.0

        if close >= bb_upper:  # type: ignore[operator]
            action = "buy"
            excess = (close - bb_upper) / band_range if band_range > 0 else 0.0  # type: ignore[operator]
            confidence = min(0.50 + min(excess, 0.30) + squeeze_bonus, 1.0)
        elif close <= bb_lower:  # type: ignore[operator]
            action = "sell"
            excess = (bb_lower - close) / band_range if band_range > 0 else 0.0  # type: ignore[operator]
            confidence = min(0.50 + min(excess, 0.30) + squeeze_bonus, 1.0)
        else:
            action = "hold"
            confidence = 0.15 if is_squeeze else 0.1

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=(
                f"close={close:.2f},bb_upper={bb_upper:.2f},bb_lower={bb_lower:.2f},"
                f"squeeze={is_squeeze}"
            ),
            used_indicators=used or ["boll20", "keltner20"],
            metadata={
                "close": close,
                "bb_upper": bb_upper,
                "bb_lower": bb_lower,
                "kc_upper": kc_upper,
                "kc_lower": kc_lower,
                "is_squeeze": is_squeeze,
                "squeeze_bonus": squeeze_bonus,
            },
        )


class DonchianBreakoutStrategy:
    """唐奇安通道突破策略（海龟交易法则变体）。

    核心逻辑：
    - close 突破 N 周期最高价 → 买入（趋势延续）
    - close 跌破 N 周期最低价 → 卖出
    - ADX 过滤：ADX < adx_min 时不交易（避免震荡市虚假突破）

    适合黄金的原因：
    XAUUSD 在日线级别和 H1/H4 上有明显的趋势延续特性，
    价格创新高往往意味着机构资金推动的趋势行情。
    ATR 倍数可通过 ADX 值动态映射置信度。

    仅在 bar 收盘时评估：价格在 bar 内触及前高/前低不算有效突破，
    需要 bar 收盘价确认。
    """

    name = "donchian_breakout"
    required_indicators = ("donchian20", "adx14")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING:  0.90,  # 趋势延续期创新高/低是高概率信号
        RegimeType.RANGING:   0.15,  # 震荡市假突破极多，即便有 ADX 过滤也危险
        RegimeType.BREAKOUT:  1.00,  # 通道突破就是为这个 Regime 而生
        RegimeType.UNCERTAIN: 0.45,
    }

    def __init__(self, *, adx_min: float = 20.0) -> None:
        self._adx_min = adx_min

    def evaluate(self, context: SignalContext) -> SignalDecision:
        d_upper, d_name = _resolve_indicator_value(
            context.indicators, (("donchian20", "donchian_upper"),)
        )
        d_lower, _ = _resolve_indicator_value(
            context.indicators, (("donchian20", "donchian_lower"),)
        )
        d_close, _ = _resolve_indicator_value(
            context.indicators, (("donchian20", "close"),)
        )
        adx_val, adx_name = _resolve_indicator_value(
            context.indicators, (("adx14", "adx"), ("adx", "adx"))
        )
        plus_di, _ = _resolve_indicator_value(
            context.indicators, (("adx14", "plus_di"),)
        )
        minus_di, _ = _resolve_indicator_value(
            context.indicators, (("adx14", "minus_di"),)
        )
        used = [n for n in (d_name, adx_name) if n]

        if d_upper is None or d_lower is None or d_close is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:donchian20",
                used_indicators=used or ["donchian20"],
            )

        adx = adx_val if adx_val is not None else 0.0

        # ADX 过滤：趋势强度不足时跳过
        if adx < self._adx_min:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.1,
                reason=f"adx_too_low:{adx:.1f}<{self._adx_min}",
                used_indicators=used or ["donchian20", "adx14"],
                metadata={"adx": adx, "donchian_upper": d_upper, "donchian_lower": d_lower},
            )

        channel_width = d_upper - d_lower

        if d_close >= d_upper:
            action = "buy"
            # DI 方向加成：+DI > -DI 表示多头占优
            di_bonus = 0.1 if (plus_di is not None and minus_di is not None and plus_di > minus_di) else 0.0
            adx_conf = min((adx - self._adx_min) / 30.0 + 0.55, 0.85)
            confidence = min(adx_conf + di_bonus, 1.0)
        elif d_close <= d_lower:
            action = "sell"
            di_bonus = 0.1 if (plus_di is not None and minus_di is not None and minus_di > plus_di) else 0.0
            adx_conf = min((adx - self._adx_min) / 30.0 + 0.55, 0.85)
            confidence = min(adx_conf + di_bonus, 1.0)
        else:
            action = "hold"
            confidence = 0.1

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=(
                f"close={d_close:.2f},d_upper={d_upper:.2f},d_lower={d_lower:.2f},"
                f"adx={adx:.1f}"
            ),
            used_indicators=used or ["donchian20", "adx14"],
            metadata={
                "close": d_close,
                "donchian_upper": d_upper,
                "donchian_lower": d_lower,
                "channel_width": channel_width,
                "adx": adx,
                "plus_di": plus_di,
                "minus_di": minus_di,
            },
        )


class EmaRibbonStrategy:
    """EMA 带状对齐趋势策略。

    核心逻辑：
    - 三条均线（EMA9 快线、HMA20 中线、EMA50 慢线）全部同向排列时产生信号
    - 多头排列：EMA9 > HMA20 > EMA50（快 > 中 > 慢）
    - 空头排列：EMA9 < HMA20 < EMA50
    - 置信度由三条线之间的相对间距决定（间距越大趋势越强）

    适合黄金的原因：
    XAUUSD 趋势行情持续性较好，三线对齐能过滤掉震荡噪音，
    HMA 响应比 EMA 快约一半，比 WMA 平滑，是 EMA9/EMA50 之间的
    良好中间层，三者共同形成"动量梯队"信号。

    仅在 bar 收盘时评估：intrabar MA 值持续波动，噪声大。
    """

    name = "ema_ribbon"
    required_indicators = ("ema9", "hma20", "ema50")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING:  1.00,  # 三线对齐是趋势行情的黄金信号
        RegimeType.RANGING:   0.10,  # 震荡市三线互相缠绕交叉，完全无效
        RegimeType.BREAKOUT:  0.40,  # 突破初期 EMA50 还未偏转，排列不完整
        RegimeType.UNCERTAIN: 0.40,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        ema9_val, e9_name = _resolve_indicator_value(
            context.indicators, (("ema9", "ema"), ("ema9", "value"))
        )
        hma_val, hma_name = _resolve_indicator_value(
            context.indicators, (("hma20", "hma"), ("hma20", "value"))
        )
        ema50_val, e50_name = _resolve_indicator_value(
            context.indicators, (("ema50", "ema"), ("ema50", "value"))
        )
        used = [n for n in (e9_name, hma_name, e50_name) if n]

        if ema9_val is None or hma_val is None or ema50_val is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used or ["ema9", "hma20", "ema50"],
            )

        # 三线间距（归一化到慢线价格）
        slow = ema50_val
        if slow == 0:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="ema50_is_zero",
                used_indicators=used,
            )

        fast_mid_gap = abs(ema9_val - hma_val) / slow      # EMA9 vs HMA20 间距
        mid_slow_gap = abs(hma_val - ema50_val) / slow     # HMA20 vs EMA50 间距
        total_span = abs(ema9_val - ema50_val) / slow      # 快慢总间距

        if ema9_val > hma_val > ema50_val:
            # 多头完整排列
            action = "buy"
            # 间距越大 → 趋势越强；上限 0.9 留出安全边际
            confidence = min(0.5 + total_span * 80, 0.9)
        elif ema9_val < hma_val < ema50_val:
            # 空头完整排列
            action = "sell"
            confidence = min(0.5 + total_span * 80, 0.9)
        else:
            # 排列混乱，趋势不明确
            action = "hold"
            confidence = 0.1

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=(
                f"ema9={ema9_val:.2f},hma20={hma_val:.2f},ema50={ema50_val:.2f}"
            ),
            used_indicators=used or ["ema9", "hma20", "ema50"],
            metadata={
                "ema9": ema9_val,
                "hma20": hma_val,
                "ema50": ema50_val,
                "fast_mid_gap_pct": fast_mid_gap * 100,
                "mid_slow_gap_pct": mid_slow_gap * 100,
                "total_span_pct": total_span * 100,
            },
        )
