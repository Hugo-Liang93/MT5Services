from __future__ import annotations

from typing import Any, List, Optional

from ...evaluation.regime import RegimeType
from ...models import SignalContext, SignalDecision
from ..base import _resolve_indicator_value, get_tf_param


def _recent_bars(context: SignalContext) -> list[Any]:
    payload = context.metadata.get("recent_bars")
    return list(payload) if isinstance(payload, list) else []


def _bar_value(bar: Any, field: str) -> Optional[float]:
    value = getattr(bar, field, None)
    if value is None and isinstance(bar, dict):
        value = bar.get(field)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ohlc(bar: Any) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    return (
        _bar_value(bar, "open"),
        _bar_value(bar, "high"),
        _bar_value(bar, "low"),
        _bar_value(bar, "close"),
    )


class PriceActionReversal:
    """Detect simple price-action reversal patterns normalized by ATR."""

    name = "price_action_reversal"
    category = "price_action"
    required_indicators = ("atr14",)
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 0.30,
        RegimeType.RANGING: 0.90,
        RegimeType.BREAKOUT: 0.60,
        RegimeType.UNCERTAIN: 0.70,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        atr, atr_name = _resolve_indicator_value(
            context.indicators, (("atr14", "atr"), ("atr", "atr"))
        )
        bars = _recent_bars(context)
        used = [atr_name] if atr_name else ["atr14"]

        if atr is None or atr <= 0 or not bars:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_price_action_context",
                used_indicators=used,
            )

        candidates: list[tuple[str, float, str, dict[str, Any]]] = []
        current = bars[-1]
        current_open, current_high, current_low, current_close = _ohlc(current)
        if None in {current_open, current_high, current_low, current_close}:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_bar_ohlc",
                used_indicators=used,
            )

        body = abs(current_close - current_open)
        upper_wick = max(current_high - max(current_open, current_close), 0.0)
        lower_wick = max(min(current_open, current_close) - current_low, 0.0)

        if lower_wick >= max(body * 2.0, atr * 0.6):
            candidates.append(
                (
                    "buy",
                    min(0.52 + min(lower_wick / atr, 2.0) * 0.15, 0.9),
                    "bullish_pin_bar",
                    {"body": body, "lower_wick": lower_wick, "upper_wick": upper_wick},
                )
            )
        if upper_wick >= max(body * 2.0, atr * 0.6):
            candidates.append(
                (
                    "sell",
                    min(0.52 + min(upper_wick / atr, 2.0) * 0.15, 0.9),
                    "bearish_pin_bar",
                    {"body": body, "lower_wick": lower_wick, "upper_wick": upper_wick},
                )
            )

        if len(bars) >= 2:
            prev_open, _prev_high, _prev_low, prev_close = _ohlc(bars[-2])
            if None not in {prev_open, prev_close}:
                if (
                    prev_close < prev_open
                    and current_close > current_open
                    and current_open <= prev_close
                    and current_close >= prev_open
                ):
                    candidates.append(
                        (
                            "buy",
                            0.72,
                            "bullish_engulfing",
                            {"prev_open": prev_open, "prev_close": prev_close},
                        )
                    )
                if (
                    prev_close > prev_open
                    and current_close < current_open
                    and current_open >= prev_close
                    and current_close <= prev_open
                ):
                    candidates.append(
                        (
                            "sell",
                            0.72,
                            "bearish_engulfing",
                            {"prev_open": prev_open, "prev_close": prev_close},
                        )
                    )

        if len(bars) >= 3:
            _mother_open, mother_high, mother_low, _mother_close = _ohlc(bars[-3])
            _prev_open, prev_high, prev_low, _prev_close = _ohlc(bars[-2])
            if None not in {mother_high, mother_low, prev_high, prev_low}:
                inside_bar = prev_high <= mother_high and prev_low >= mother_low
                if inside_bar and current_close > mother_high:
                    candidates.append(
                        (
                            "buy",
                            0.6,
                            "inside_bar_breakout_up",
                            {"mother_high": mother_high, "mother_low": mother_low},
                        )
                    )
                if inside_bar and current_close < mother_low:
                    candidates.append(
                        (
                            "sell",
                            0.6,
                            "inside_bar_breakout_down",
                            {"mother_high": mother_high, "mother_low": mother_low},
                        )
                    )

        if not candidates:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.1,
                reason="no_price_action_pattern",
                used_indicators=used,
                metadata={"atr": atr},
            )

        action, confidence, pattern, extra = max(candidates, key=lambda item: item[1])
        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=pattern,
            used_indicators=used,
            metadata={
                "atr": atr,
                "pattern": pattern,
                "open": current_open,
                "high": current_high,
                "low": current_low,
                "close": current_close,
                **extra,
            },
        )


class OrderBlockEntryStrategy:
    """Order Block 回踩入场策略 — 智能资金概念（SMC）核心模型。

    Order Block（OB）是强势位移（displacement）发生前的最后一根反向 bar，
    代表机构在此价位建仓的痕迹。价格回踩 OB 区间时顺势入场是高概率交易。

    检测逻辑：
    1. 在 recent_bars 中寻找"强势位移"：连续 2+ 根同向 bar 且合计 close 变动 > displacement_atr×ATR
    2. 位移前的最后一根反向 bar = Order Block（其 high~low 为 OB 区间）
    3. 当前 bar 的 close 回踩到 OB 区间内 → 顺势入场

    仅在 confirmed scope 评估（需要完整 bar 确认回踩）。
    """

    name = "order_block_entry"
    category = "price_action"
    required_indicators = ("atr14",)
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 0.85,
        RegimeType.RANGING: 0.40,
        RegimeType.BREAKOUT: 0.70,
        RegimeType.UNCERTAIN: 0.50,
    }

    # Per-TF 可调参数默认值
    _displacement_atr: float = 1.5  # 位移强度（合计 close 变动需 >= N×ATR）
    _min_displacement_bars: int = 2  # 位移至少持续的 bar 数
    _ob_max_age_bars: int = 30  # OB 最多在多少根 bar 前仍有效
    _ob_body_min_atr: float = 0.3  # OB bar 的实体至少 N×ATR（过滤弱 bar）

    def __init__(self) -> None:
        self.recent_bars_depth: int = 50

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used: List[str] = ["atr14"]
        hold = SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="hold",
            confidence=0.0,
            reason="no_ob_setup",
            used_indicators=used,
        )

        atr, _ = _resolve_indicator_value(
            context.indicators, (("atr14", "atr"), ("atr", "atr"))
        )
        if atr is None or atr <= 0:
            return hold

        bars = _recent_bars(context)
        if len(bars) < 10:
            return hold

        tf = context.timeframe
        disp_atr = get_tf_param(self, "displacement_atr", tf, self._displacement_atr)
        min_disp_bars = int(get_tf_param(self, "min_displacement_bars", tf, float(self._min_displacement_bars)))
        ob_max_age = int(get_tf_param(self, "ob_max_age_bars", tf, float(self._ob_max_age_bars)))
        ob_body_min = get_tf_param(self, "ob_body_min_atr", tf, self._ob_body_min_atr)

        # 当前 bar
        current = bars[-1]
        cur_close = _bar_value(current, "close")
        if cur_close is None:
            return hold

        # 从最近的 bar 向前扫描，寻找 OB
        best_ob: Optional[dict[str, Any]] = None
        best_confidence: float = 0.0

        # 搜索范围：排除最近 2 根 bar（需要回踩空间）
        search_end = len(bars) - 2
        search_start = max(0, search_end - ob_max_age)

        for i in range(search_end - 1, search_start, -1):
            # 检查从 i+1 开始是否有强势位移
            displacement = self._detect_displacement(
                bars, i + 1, search_end, atr, disp_atr, min_disp_bars
            )
            if displacement is None:
                continue

            disp_direction = displacement["direction"]  # "bullish" or "bearish"

            # i 是位移前的最后一根 bar，检查它是否是合格的 OB
            ob_bar = bars[i]
            ob_open = _bar_value(ob_bar, "open")
            ob_high = _bar_value(ob_bar, "high")
            ob_low = _bar_value(ob_bar, "low")
            ob_close = _bar_value(ob_bar, "close")
            if None in {ob_open, ob_high, ob_low, ob_close}:
                continue

            ob_body = abs(ob_close - ob_open)
            if ob_body < ob_body_min * atr:
                continue

            # OB 必须是反向 bar（bullish displacement 前的 bearish bar, 反之亦然）
            ob_is_bearish = ob_close < ob_open
            ob_is_bullish = ob_close > ob_open
            if disp_direction == "bullish" and not ob_is_bearish:
                continue
            if disp_direction == "bearish" and not ob_is_bullish:
                continue

            # 检查当前 bar 是否回踩 OB 区间
            if disp_direction == "bullish":
                # Bullish OB: 价格回落到 OB 区间（ob_low ~ ob_high）后做多
                if ob_low <= cur_close <= ob_high:
                    age = len(bars) - 1 - i
                    age_decay = max(0.0, 1.0 - age / (ob_max_age * 1.5))
                    conf = 0.55 + 0.25 * min(displacement["strength"], 1.0) * age_decay
                    if conf > best_confidence:
                        best_confidence = conf
                        best_ob = {
                            "direction": "buy",
                            "ob_high": ob_high, "ob_low": ob_low,
                            "ob_index": i, "age_bars": age,
                            "displacement_strength": displacement["strength"],
                            "displacement_bars": displacement["bars"],
                        }

            elif disp_direction == "bearish":
                # Bearish OB: 价格反弹到 OB 区间后做空
                if ob_low <= cur_close <= ob_high:
                    age = len(bars) - 1 - i
                    age_decay = max(0.0, 1.0 - age / (ob_max_age * 1.5))
                    conf = 0.55 + 0.25 * min(displacement["strength"], 1.0) * age_decay
                    if conf > best_confidence:
                        best_confidence = conf
                        best_ob = {
                            "direction": "sell",
                            "ob_high": ob_high, "ob_low": ob_low,
                            "ob_index": i, "age_bars": age,
                            "displacement_strength": displacement["strength"],
                            "displacement_bars": displacement["bars"],
                        }

            # 只取第一个（最近的）有效 OB
            if best_ob is not None:
                break

        if best_ob is None:
            return hold

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=best_ob["direction"],
            confidence=min(best_confidence, 0.90),
            reason=(
                f"ob_retest:{best_ob['direction']},"
                f"age={best_ob['age_bars']}bars,"
                f"strength={best_ob['displacement_strength']:.2f}"
            ),
            used_indicators=used,
            metadata={
                "atr": atr,
                "close": cur_close,
                **best_ob,
            },
        )

    @staticmethod
    def _detect_displacement(
        bars: list[Any],
        start: int,
        end: int,
        atr: float,
        disp_atr: float,
        min_bars: int,
    ) -> Optional[dict[str, Any]]:
        """检测从 bars[start] 到 bars[end-1] 是否存在强势位移。"""
        if end - start < min_bars:
            return None

        total_move = 0.0
        consecutive = 0
        direction: Optional[str] = None

        for j in range(start, min(end, len(bars))):
            bar_open = _bar_value(bars[j], "open")
            bar_close = _bar_value(bars[j], "close")
            if bar_open is None or bar_close is None:
                break
            move = bar_close - bar_open
            if abs(move) < 1e-9:
                break

            bar_dir = "bullish" if move > 0 else "bearish"
            if direction is None:
                direction = bar_dir
            if bar_dir != direction:
                break

            total_move += move
            consecutive += 1

        if consecutive < min_bars or direction is None:
            return None

        strength = abs(total_move) / (atr * disp_atr) if atr > 0 else 0.0
        if strength < 1.0:
            return None

        return {
            "direction": direction,
            "strength": strength,
            "bars": consecutive,
            "total_move": total_move,
        }
