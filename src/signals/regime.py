"""市场状态（Regime）检测模块。

## 设计思路

不同的策略对不同的行情类型有截然不同的适配度：
  - 趋势策略（SMA Cross、EMA Ribbon、Supertrend）在震荡市中会产生大量虚假信号
  - 均值回归策略（RSI、StochRSI）在趋势市中会"越空越涨、越多越跌"
  - 突破策略（Donchian、Keltner-Squeeze）在 Squeeze 释放时最准确

本模块的职责：
  1. 根据现有指标快照识别当前市场所处的 Regime
  2. 将 Regime 传递给 SignalModule.evaluate()，由其对各策略的 confidence 施加亲和度权重
  3. 这样不需要改动状态机（runtime.py），
     只需在信号进入状态机之前完成置信度修正即可。

## Regime 分类

  TRENDING  — ADX ≥ 25，方向明确，均线对齐
  RANGING   — ADX < 20，价格在通道内震荡，没有方向
  BREAKOUT  — 布林带挤压（BB 在 KC 内），或 ADX 突然放量，波动率释放
  UNCERTAIN — 介于 TRENDING 与 RANGING 之间的过渡状态（20 ≤ ADX < 25）

## 检测逻辑（优先级从高到低）

  1. Keltner-Bollinger Squeeze（bb_upper < kc_upper AND bb_lower > kc_lower）→ BREAKOUT
  2. ADX ≥ 25 → TRENDING
  3. ADX < 20：
       a. BB 宽度 < bb_tight_pct（默认 0.5%）→ BREAKOUT（价格盘整蓄力）
       b. 否则 → RANGING
  4. 20 ≤ ADX < 25 → UNCERTAIN
  5. 无 ADX 数据 → UNCERTAIN（兜底）
"""
from __future__ import annotations

import threading
from enum import Enum
from typing import Any, Dict, Optional


class RegimeType(str, Enum):
    """市场状态枚举。

    str 混入使枚举值可直接用于 JSON / metadata 序列化：
        regime.value == "trending"
    """

    TRENDING = "trending"
    RANGING = "ranging"
    BREAKOUT = "breakout"
    UNCERTAIN = "uncertain"


def _safe_float(value: Any) -> Optional[float]:
    """安全地将 value 转为 float，失败返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MarketRegimeDetector:
    """从指标快照中推断当前市场状态。

    所有参数在构造时配置，运行时不维护任何可变状态（线程安全）。

    参数
    ----
    adx_trending_threshold:
        ADX 超过此值判定为趋势行情（默认 25.0）。
    adx_ranging_threshold:
        ADX 低于此值判定为震荡行情（默认 20.0）。
    bb_tight_pct:
        布林带宽度（(upper-lower)/mid）低于此百分比时，
        即使 ADX 未进入 Squeeze 也视为 BREAKOUT（预突破蓄力）。
        默认 0.005（0.5%），对黄金 H1 大约对应 ±$10 的极窄通道。
    """

    def __init__(
        self,
        *,
        adx_trending_threshold: float = 25.0,
        adx_ranging_threshold: float = 20.0,
        bb_tight_pct: float = 0.005,
    ) -> None:
        self._adx_trending = adx_trending_threshold
        self._adx_ranging = adx_ranging_threshold
        self._bb_tight_pct = bb_tight_pct

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self, indicators: Dict[str, Dict[str, Any]]) -> RegimeType:
        """根据指标快照返回当前市场状态。

        只读取已计算好的指标值，不做任何新的计算，开销极低。

        所需指标（可选，缺失时降级到 UNCERTAIN）：
          - adx14 → {"adx": float}
          - boll20 → {"bb_upper": float, "bb_lower": float, "bb_mid": float}
          - keltner20 → {"kc_upper": float, "kc_lower": float}
        """
        bb_upper, bb_lower, bb_mid = self._extract_bb(indicators)
        kc_upper, kc_lower = self._extract_kc(indicators)
        adx = self._extract_adx(indicators)

        # ── 步骤 1：Keltner-Bollinger Squeeze ──────────────────────────
        # 布林带完全在肯特纳通道内 → 波动率极度压缩，等待方向性释放
        if (
            bb_upper is not None
            and bb_lower is not None
            and kc_upper is not None
            and kc_lower is not None
            and bb_upper < kc_upper
            and bb_lower > kc_lower
        ):
            return RegimeType.BREAKOUT

        # ── 步骤 2：ADX 主判 ────────────────────────────────────────────
        if adx is not None:
            if adx >= self._adx_trending:
                return RegimeType.TRENDING

            if adx < self._adx_ranging:
                # ADX 低但布林带已经非常窄 → 盘整蓄力，即将突破
                if (
                    bb_upper is not None
                    and bb_lower is not None
                    and bb_mid is not None
                    and bb_mid > 0
                    and (bb_upper - bb_lower) / bb_mid < self._bb_tight_pct
                ):
                    return RegimeType.BREAKOUT
                return RegimeType.RANGING

            # 20 ≤ ADX < 25：趋势与震荡之间的过渡区
            return RegimeType.UNCERTAIN

        # ── 步骤 3：没有 ADX 数据，根据 BB 宽度粗判 ──────────────────
        if (
            bb_upper is not None
            and bb_lower is not None
            and bb_mid is not None
            and bb_mid > 0
        ):
            bb_width_pct = (bb_upper - bb_lower) / bb_mid
            if bb_width_pct < self._bb_tight_pct:
                return RegimeType.BREAKOUT

        return RegimeType.UNCERTAIN

    def detect_with_detail(
        self, indicators: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        """返回 Regime 以及检测时使用的中间数值，方便调试和日志。"""
        bb_upper, bb_lower, bb_mid = self._extract_bb(indicators)
        kc_upper, kc_lower = self._extract_kc(indicators)
        adx = self._extract_adx(indicators)

        regime = self.detect(indicators)

        bb_width_pct: Optional[float] = None
        if bb_upper is not None and bb_lower is not None and bb_mid and bb_mid > 0:
            bb_width_pct = (bb_upper - bb_lower) / bb_mid

        is_squeeze: Optional[bool] = None
        if (
            bb_upper is not None
            and bb_lower is not None
            and kc_upper is not None
            and kc_lower is not None
        ):
            is_squeeze = bb_upper < kc_upper and bb_lower > kc_lower

        return {
            "regime": regime.value,
            "adx": adx,
            "bb_width_pct": bb_width_pct,
            "is_kc_bb_squeeze": is_squeeze,
            "adx_trending_threshold": self._adx_trending,
            "adx_ranging_threshold": self._adx_ranging,
            "bb_tight_pct": self._bb_tight_pct,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_adx(
        indicators: Dict[str, Dict[str, Any]]
    ) -> Optional[float]:
        for name in ("adx14", "adx"):
            payload = indicators.get(name)
            if isinstance(payload, dict):
                v = _safe_float(payload.get("adx"))
                if v is not None:
                    return v
        return None

    @staticmethod
    def _extract_bb(
        indicators: Dict[str, Dict[str, Any]]
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        for name in ("boll20", "bollinger20", "bollinger"):
            payload = indicators.get(name)
            if isinstance(payload, dict):
                upper = _safe_float(payload.get("bb_upper"))
                lower = _safe_float(payload.get("bb_lower"))
                mid = _safe_float(payload.get("bb_mid"))
                if upper is not None and lower is not None:
                    return upper, lower, mid
        return None, None, None

    @staticmethod
    def _extract_kc(
        indicators: Dict[str, Dict[str, Any]]
    ) -> tuple[Optional[float], Optional[float]]:
        for name in ("keltner20", "keltner"):
            payload = indicators.get(name)
            if isinstance(payload, dict):
                upper = _safe_float(payload.get("kc_upper"))
                lower = _safe_float(payload.get("kc_lower"))
                if upper is not None and lower is not None:
                    return upper, lower
        return None, None


class RegimeTracker:
    """跟踪连续相同 Regime 的持续性，提供稳定性加成乘数。

    当市场连续多根 bar 停留在同一 Regime 时，说明行情类型确立，
    当前行情下擅长的策略所发出的信号更可靠，故给予置信度加成。

    乘数从 1.0 线性增长至 max_multiplier，
    经过 min_bars_for_full_stability 根 bar 后达到最大值。

    ## 用法

    每根 bar 收盘（或每次 snapshot 传入新 Regime）时调用 ``update()``，
    然后将返回的乘数应用于 consensus 置信度：
        multiplier = tracker.update(regime)
        consensus.confidence = min(1.0, consensus.confidence * multiplier)

    ## 线程安全

    内部使用 Lock，可安全地在 SignalRuntime 后台线程中调用。
    """

    def __init__(
        self,
        *,
        min_bars_for_full_stability: int = 3,
        max_multiplier: float = 1.20,
    ) -> None:
        if min_bars_for_full_stability < 1:
            raise ValueError("min_bars_for_full_stability must be >= 1")
        if not (1.0 <= max_multiplier <= 2.0):
            raise ValueError("max_multiplier must be in [1.0, 2.0]")
        self._min_bars = min_bars_for_full_stability
        self._max_multiplier = max_multiplier
        self._current_regime: Optional[RegimeType] = None
        self._consecutive_bars: int = 0
        self._lock = threading.Lock()

    def update(self, regime: RegimeType) -> float:
        """更新 Regime 状态并返回当前稳定性乘数（≥ 1.0）。

        连续 bars 数量越多、乘数越高（最大不超过 max_multiplier）。
        Regime 切换时重置计数，乘数回到 1.0。
        """
        with self._lock:
            if regime == self._current_regime:
                self._consecutive_bars += 1
            else:
                self._current_regime = regime
                self._consecutive_bars = 1
            return self._compute_multiplier(self._consecutive_bars)

    def stability_multiplier(self) -> float:
        """返回当前稳定性乘数（不更新状态）。"""
        with self._lock:
            return self._compute_multiplier(self._consecutive_bars)

    def _compute_multiplier(self, bars: int) -> float:
        ratio = min(1.0, bars / self._min_bars)
        return 1.0 + (self._max_multiplier - 1.0) * ratio

    def describe(self) -> Dict[str, Any]:
        """返回当前跟踪状态，用于监控端点。"""
        with self._lock:
            return {
                "current_regime": self._current_regime.value if self._current_regime else None,
                "consecutive_bars": self._consecutive_bars,
                "stability_multiplier": self._compute_multiplier(self._consecutive_bars),
                "min_bars_for_full_stability": self._min_bars,
                "max_multiplier": self._max_multiplier,
            }
