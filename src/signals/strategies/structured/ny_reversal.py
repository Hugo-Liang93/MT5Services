"""StructuredNYReversal — 美盘开盘第一两根 H1 + 反转 K 线 → 反向交易。

设计理由（day-trading edge，纯 metadata 驱动，无新 indicator 依赖）：
    NY session 开盘后 30-60min 是经典 reversal 时段——欧盘在亚洲后期/欧美
    交接形成的趋势经常因美盘流动性进入而反转（机构 desk 接手、re-pricing）。
    单纯靠 session-aware logic + ema21/55 cross + 反转 K 线即可识别。

入场逻辑：
    - 必须条件：bar_time.hour ∈ {13, 14} (UTC) 且 SESSION_BUCKETS 含 "new_york"
    - 上行结构（ema21 > ema55 + 差值显著）+ 看跌反转 K 线 → sell（反转上涨）
    - 下行结构（ema21 < ema55）+ 看涨反转 K 线 → buy（反转下跌）
    - 反转 K 线需与方向匹配；不匹配视作延续不入场

风险逻辑：紧 SL 1.0 ATR；TP 2.0 ATR；time 8 bars（NY session 半天约 8 H1 bars）。

Regime affinity：trending/ranging 中性，breakout 弱（突破时反转概率低）。

为何不需要新 indicator：
    - bar_time.hour 直接从 metadata[MK.BAR_TIME] (ISO) parse
    - 当前 session 从 metadata[MK.SESSION_BUCKETS] 取
    - 方向用 ema21/ema55 已存在的 indicator
    - 设计自觉避免补丁式新增（参 §0dj 反补丁纪律）
"""

from __future__ import annotations

from typing import Optional, Tuple

from src.utils.timezone import parse_iso_to_utc

from ...evaluation.regime import RegimeType
from ...metadata_keys import MetadataKey as MK
from ...models import SignalContext
from ..base import get_tf_param
from .base import ExitMode, ExitSpec, HtfPolicy, StructuredStrategyBase

# NY session 开盘"第一两根 H1 bar"的 UTC hour 集合。
# NY 开盘 13:30 UTC（春令 EDT-04）/ 14:30 UTC（冬令 EST-05）；
# 取 hour ∈ {13, 14} 兼顾两季 + first 1-2h reversal 窗口。
_NY_OPEN_HOURS_UTC = frozenset({13, 14})


class StructuredNYReversal(StructuredStrategyBase):
    """美盘开盘反转入场（day-trading edge 3 — 手工，纯 metadata 驱动）。"""

    name = "structured_ny_reversal"
    category = "reversion"
    htf_policy = HtfPolicy.NONE
    preferred_scopes = ("confirmed",)
    required_indicators = (
        "ema21",
        "ema55",
        "candle_pattern",
        "bar_stats20",
        "atr14",
        "adx14",
    )
    regime_affinity = {
        RegimeType.TRENDING: 0.70,  # 趋势中反转 setup 频次高
        RegimeType.BREAKOUT: 0.30,  # 突破中反转概率低
        RegimeType.RANGING: 0.80,
        RegimeType.UNCERTAIN: 0.40,
    }

    # ── 触发参数 ──
    # ema21/ema55 差值的最小绝对值（避免横盘无方向时误触发）
    _min_ema_separation_atr: float = 0.2  # 差值 ≥ 0.2 ATR 才视为有方向
    _max_adx: float = 35.0  # 高 ADX 强趋势，反转概率低

    # ── Exit 参数 ──
    _sl_atr: float = 1.0
    _tp_atr: float = 2.0
    _time_bars: int = 8  # NY session 约 8 H1 bars（13~21 UTC）

    def _bar_hour(self, ctx: SignalContext) -> Optional[int]:
        bt_str = ctx.metadata.get(MK.BAR_TIME)
        if not bt_str:
            return None
        try:
            return parse_iso_to_utc(str(bt_str)).hour
        except Exception:
            return None

    def _ema_pair(
        self, ctx: SignalContext
    ) -> Tuple[Optional[float], Optional[float]]:
        ema21 = ctx.indicators.get("ema21", {}).get("ema")
        ema55 = ctx.indicators.get("ema55", {}).get("ema")
        return (
            float(ema21) if ema21 is not None else None,
            float(ema55) if ema55 is not None else None,
        )

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        # 1) NY 开盘窗口
        hour = self._bar_hour(ctx)
        if hour is None or hour not in _NY_OPEN_HOURS_UTC:
            return (
                False,
                None,
                0.0,
                f"outside_ny_open_window:hour={hour}",
            )
        sessions = ctx.metadata.get(MK.SESSION_BUCKETS, []) or []
        if "new_york" not in sessions:
            return False, None, 0.0, "session_not_new_york"

        # 2) ema cross 方向
        ema21, ema55 = self._ema_pair(ctx)
        if ema21 is None or ema55 is None:
            return False, None, 0.0, "ema_unavailable"

        atr = self._atr(ctx)
        if atr is None or atr <= 0:
            return False, None, 0.0, "no_atr"

        sep = ema21 - ema55
        min_sep = self._min_ema_separation_atr * atr
        if abs(sep) < min_sep:
            return (
                False,
                None,
                0.0,
                f"ema_too_flat:|sep|={abs(sep):.2f}<{min_sep:.2f}",
            )

        # 3) ADX 上限
        adx = self._adx_full(ctx).get("adx")
        if adx is not None and adx > self._max_adx:
            return False, None, 0.0, f"adx_too_high:{adx:.0f}"

        # 反转：ema 上行 → sell setup；ema 下行 → buy setup
        direction = "sell" if sep > 0 else "buy"
        # score: ema 差距越显著（越接近 1.0 ATR）越好
        score = min(abs(sep) / atr, 1.0)
        return True, direction, score, f"ny_open reversal_setup:ema_sep={sep:+.2f}"

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        """入场时机：方向匹配的反转 K 线（与 prior_day_retest 共享判定）。"""
        candle = ctx.indicators.get("candle_pattern", {}) or {}
        stats = ctx.indicators.get("bar_stats20", {}) or {}

        pin = candle.get("pin_bar", 0.0) or 0.0
        ham = candle.get("hammer", 0.0) or 0.0
        rej = candle.get("rejection", 0.0) or 0.0
        eng = candle.get("engulfing", 0.0) or 0.0
        close_pos = stats.get("close_position", 0.5) or 0.5

        signals: list[tuple[float, str]] = []
        if direction == "buy":
            if pin > 0:
                signals.append((1.0, "pin_bull"))
            if ham > 0:
                signals.append((0.85, "hammer"))
            if eng > 0:
                signals.append((0.85, "engulfing_bull"))
            if rej > 0 and close_pos > 0.5:
                signals.append((0.65, "rejection_bull"))
        else:
            if pin < 0:
                signals.append((1.0, "pin_bear"))
            if ham < 0:
                signals.append((0.85, "shooting_star"))
            if eng < 0:
                signals.append((0.85, "engulfing_bear"))
            if rej < 0 and close_pos < 0.5:
                signals.append((0.65, "rejection_bear"))

        if not signals:
            return False, 0.0, "no_reversal_pattern"

        score, reason = max(signals, key=lambda x: x[0])
        return True, score, reason

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        sl = get_tf_param(self, "sl_atr", ctx.timeframe, self._sl_atr)
        tp = get_tf_param(self, "tp_atr", ctx.timeframe, self._tp_atr)
        tb = int(get_tf_param(self, "time_bars", ctx.timeframe, self._time_bars))
        return ExitSpec(
            sl_atr=sl,
            tp_atr=tp,
            mode=ExitMode.BARRIER,
            time_bars=tb,
        )
