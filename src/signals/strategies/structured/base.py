"""结构化策略基类 — 标准化数据访问 + Where(软)/When(硬)/Why(硬) 评估框架。

所有结构化策略继承 StructuredStrategyBase，只需实现 _why() / _when()。
_where() 和 _volume_bonus() 可选实现。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...metadata_keys import MetadataKey as MK
from ...models import SignalContext, SignalDecision
from ..base import get_tf_param

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  策略输出契约（类型安全的 entry/exit 规格）
#
#  ADR-013：信号策略只输出 "方向 + 信心 + 出场契约 + 信号语义元数据"，不再
#  决定入场。EntryType / EntrySpec 已搬迁到 src.trading.entry_policy.specs。
# ---------------------------------------------------------------------------


class StructureBias(str, Enum):
    """市场结构偏向枚举（MarketStructureAnalyzer._resolve_structure_bias 输出）。"""

    BULLISH_BREAKOUT = "bullish_breakout"
    BEARISH_BREAKOUT = "bearish_breakout"
    BULLISH_PULLBACK = "bullish_pullback"
    BEARISH_PULLBACK = "bearish_pullback"
    BULLISH_RECLAIM = "bullish_reclaim"
    BEARISH_RECLAIM = "bearish_reclaim"
    BULLISH_SWEEP_CONFIRMED = "bullish_sweep_confirmed"
    BEARISH_SWEEP_CONFIRMED = "bearish_sweep_confirmed"
    COMPRESSION = "compression"
    EXPANSION = "expansion"
    NEUTRAL = "neutral"


# 分组常量，供策略 _where() 使用
BULLISH_BIASES = frozenset(
    {
        StructureBias.BULLISH_BREAKOUT,
        StructureBias.BULLISH_PULLBACK,
        StructureBias.BULLISH_RECLAIM,
        StructureBias.BULLISH_SWEEP_CONFIRMED,
    }
)
BEARISH_BIASES = frozenset(
    {
        StructureBias.BEARISH_BREAKOUT,
        StructureBias.BEARISH_PULLBACK,
        StructureBias.BEARISH_RECLAIM,
        StructureBias.BEARISH_SWEEP_CONFIRMED,
    }
)


class ExitMode(str, Enum):
    """Exit 模型分派枚举。

    CHANDELIER — 传统路径：ATR trailing + breakeven + signal reversal + timeout
                 由 aggression α 驱动；未声明或未知值时默认。
    BARRIER    — Triple-Barrier 路径（F-12a/b）：固定 SL/TP/Time 三条 barrier
                 先碰哪个就退出。要求同时声明 sl_atr / tp_atr / time_bars。
                 用于挖掘产出的"固定 RR"类规则，保持实盘 exit 与挖掘
                 forward_return 的语义一致。
    """

    CHANDELIER = "chandelier"
    BARRIER = "barrier"


class ExitSpec:
    """策略出场规格（不可变值对象）。

    mode=CHANDELIER 时 aggression 驱动 trailing 参数，sl_atr/tp_atr 为 None。
    mode=BARRIER 时必须声明 sl_atr / tp_atr / time_bars 三者，aggression 被忽略。
    """

    __slots__ = ("mode", "aggression", "sl_atr", "tp_atr", "time_bars")

    def __init__(
        self,
        aggression: float = 0.50,
        sl_atr: Optional[float] = None,
        tp_atr: Optional[float] = None,
        *,
        mode: ExitMode | str = ExitMode.CHANDELIER,
        time_bars: Optional[int] = None,
    ) -> None:
        if not 0.0 <= aggression <= 1.0:
            raise ValueError(f"aggression must be 0.0~1.0, got {aggression}")
        self.mode = ExitMode(mode)  # 校验合法性
        if self.mode is ExitMode.BARRIER:
            if sl_atr is None or sl_atr <= 0:
                raise ValueError("BARRIER mode requires sl_atr > 0")
            if tp_atr is None or tp_atr <= 0:
                raise ValueError("BARRIER mode requires tp_atr > 0")
            if time_bars is None or time_bars < 1:
                raise ValueError("BARRIER mode requires time_bars >= 1")
        self.aggression = aggression
        self.sl_atr = sl_atr
        self.tp_atr = tp_atr
        self.time_bars = time_bars

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "aggression": self.aggression,
            "sl_atr": self.sl_atr,
            "tp_atr": self.tp_atr,
            "time_bars": self.time_bars,
        }


class HtfPolicy(str, Enum):
    """HTF（高一级时间框架）校验策略。

    声明式元数据，标注策略对 HTF 方向数据的依赖方式：
      HARD_GATE  — HTF 方向/ADX 缺失或冲突时拒绝信号（如趋势延续）
      SOFT_GATE  — HTF 冲突时拒绝，缺失时放行（如突破跟随）
      SOFT_BONUS — HTF 一致加分，冲突降分但不拒绝（如时段突破、趋势线触碰）
      NONE       — 不使用 HTF 数据（如反转、极端 bar 策略）
    """

    HARD_GATE = "hard_gate"
    SOFT_GATE = "soft_gate"
    SOFT_BONUS = "soft_bonus"
    NONE = "none"


class StructuredStrategyBase:
    """结构化策略基类。

    子类必须设置类属性：name, category, htf_policy, required_indicators, regime_affinity
    使用 HTF 数据的子类须设置：htf_required_indicators（声明跨 TF 引用的指标）
    子类必须实现：_why(), _when(), _entry_spec(), _exit_spec()
    子类可选实现：_where(), _volume_bonus()

    评估流程（信号决策）：
        1. Why  → 确定方向 + 确认条件（硬门控, score 0~1）
        2. When → 入场时机（硬门控, score 0~1）
        3. Where → 结构位加分（软门控, score 0~1）
        4. Volume → 量能加分（软门控, score 0~1）
        5. 合并置信度 = base + Σ(score × budget)

    执行规格（信号通过后）：
        6. _entry_spec() → 入场方式（market/limit/stop + 价格）
        7. _exit_spec()  → 出场参数（aggression α 或显式 profile）
    """

    name: str = ""
    category: str = ""
    htf_policy: HtfPolicy = HtfPolicy.NONE
    required_indicators: Tuple[str, ...] = ()
    htf_required_indicators: Dict[str, str] = {}
    preferred_scopes: Tuple[str, ...] = ("confirmed",)
    market_data_requirements: Tuple[str, ...] = ()
    regime_affinity: Dict[RegimeType, float] = {}
    promoted_indicator_lineage: Tuple[str, ...] = ()
    research_provenance_refs: Tuple[str, ...] = ()

    _base_confidence: float = 0.50

    # 统一评分预算：每层 score 0~1，乘以预算得到 confidence 贡献
    _WHY_BUDGET: float = 0.15
    _WHEN_BUDGET: float = 0.15
    _WHERE_BUDGET: float = 0.10
    _VOL_BUDGET: float = 0.05

    def __init__(self, name: Optional[str] = None, htf: Optional[str] = None) -> None:
        if name:
            self.name = name
        if htf and self.__class__.htf_required_indicators:
            # 批量覆盖所有 HTF 指标的来源 TF（用于策略变体，如 trend_h4）
            self.htf_required_indicators = {
                ind: htf for ind in self.__class__.htf_required_indicators
            }

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
        """跨 TF 聚合 HTF 指标，按 htf_required_indicators 声明从对应 TF 取值。"""
        result: Dict[str, Any] = {}
        for ind, tf in self.htf_required_indicators.items():
            tf_data = ctx.htf_indicators.get(tf, {})
            if ind in tf_data:
                result[ind] = tf_data[ind]
        return result

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
        """方向确认（硬门控）。返回 (ok, direction, score, reason)。
        score: 0.0~1.0 方向确认质量（× _WHY_BUDGET 得到 confidence 贡献）。
        """
        raise NotImplementedError

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        """入场时机（硬门控）。返回 (ok, score, reason)。
        score: 0.0~1.0 时机精度（× _WHEN_BUDGET 得到 confidence 贡献）。
        """
        raise NotImplementedError

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        """结构位加分（软门控）。返回 (score, reason)。默认 0。
        score: 0.0~1.0 结构位质量（× _WHERE_BUDGET 得到 confidence 贡献）。
        """
        return 0.0, ""

    @staticmethod
    def _linear_score(value: Optional[float], low: float, high: float) -> float:
        """线性插值评分：value < low → 0.0, value > high → 1.0, 中间线性。"""
        if value is None:
            return 0.0
        if high <= low:
            return 1.0 if value >= low else 0.0
        return min(1.0, max(0.0, (value - low) / (high - low)))

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        """量能加分（软门控）。默认 0。
        返回 0.0~1.0 量能确认质量（× _VOL_BUDGET 得到 confidence 贡献）。
        """
        return 0.0

    def _post_confidence_modifier(self, ctx: SignalContext, direction: str) -> float:
        """T9 hook: 信号 confidence 的最终乘性修正（默认 noop 1.0）。

        子类可 override 此方法，根据外部上下文（如 tick_features）降权。
        返回乘数 ∈ [0, 1]——1.0 不改、<1 降权、=0 完全压制。
        """
        return 1.0

    def _detect_pattern(
        self, ctx: SignalContext, direction: str, when_reason: str
    ) -> "PatternType":
        """从 candle_pattern 指标 + when_reason 推 PatternType（ADR-013）。

        基类提供默认实现：扫描 candle_pattern 字段（pin_bar / engulfing / hammer
        / three_method / rejection）+ bar_stats20.body_ratio，按方向匹配返回
        最强形态。子类可覆写补充自家形态识别（如 sweep_reversal、trendline_touch）。
        """
        from src.trading.entry_policy.pattern import PatternType

        candle = ctx.indicators.get("candle_pattern", {})
        stats = ctx.indicators.get("bar_stats20", {})

        pin = candle.get("pin_bar", 0.0) or 0.0
        eng = candle.get("engulfing", 0.0) or 0.0
        ham = candle.get("hammer", 0.0) or 0.0
        three = candle.get("three_method", 0.0) or 0.0
        rej = candle.get("rejection", 0.0) or 0.0
        body_ratio = stats.get("body_ratio", 0.0) or 0.0
        close_pos = stats.get("close_position", 0.5) or 0.5

        if direction == "buy":
            if pin > 0:
                return PatternType.PIN_BULL
            if eng > 0:
                return PatternType.ENGULFING_BULL
            if ham > 0:
                return PatternType.HAMMER
            if three > 0:
                return PatternType.THREE_SOLDIERS
            if body_ratio >= 1.5 and close_pos > 0.7:
                return PatternType.BIG_BAR_BULL
            if rej > 0:
                return PatternType.REJECTION_BULL
        else:  # sell
            if pin < 0:
                return PatternType.PIN_BEAR
            if eng < 0:
                return PatternType.ENGULFING_BEAR
            if ham < 0:
                return PatternType.SHOOTING_STAR
            if three < 0:
                return PatternType.THREE_CROWS
            if body_ratio >= 1.5 and close_pos < 0.3:
                return PatternType.BIG_BAR_BEAR
            if rej < 0:
                return PatternType.REJECTION_BEAR
        return PatternType.NONE

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        """出场规格（子类必须实现）。返回 ExitSpec 值对象。"""
        raise NotImplementedError

    # ── 评估框架 ──

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used = list(self.required_indicators)

        why_ok, direction, why_score, why_reason = self._why(context)
        if not why_ok or direction is None:
            return self._hold(why_reason, used)

        when_ok, when_score, when_reason = self._when(context, direction)
        if not when_ok:
            return self._hold(when_reason, used)

        where_score, where_info = self._where(context, direction)
        vol_score = self._volume_bonus(context, direction)

        # 统一评分：base + 各层 score(0~1) × 预算
        why_s = min(why_score, 1.0)
        when_s = min(when_score, 1.0)
        where_s = min(where_score, 1.0)
        vol_s = min(vol_score, 1.0)
        confidence = (
            self._base_confidence
            + why_s * self._WHY_BUDGET
            + when_s * self._WHEN_BUDGET
            + where_s * self._WHERE_BUDGET
            + vol_s * self._VOL_BUDGET
        )

        # 置信度修正审计链
        trace: list[tuple[str, float]] = [
            ("base", round(self._base_confidence, 4)),
            ("why", round(self._base_confidence + why_s * self._WHY_BUDGET, 4)),
            (
                "when",
                round(
                    self._base_confidence
                    + why_s * self._WHY_BUDGET
                    + when_s * self._WHEN_BUDGET,
                    4,
                ),
            ),
            ("where", round(confidence - vol_s * self._VOL_BUDGET, 4)),
            ("vol", round(confidence, 4)),
        ]

        # T9: 子类可选 hook — tick_features / 其他外部上下文修正 confidence。
        # 默认 noop (1.0)。返回乘数 ∈ [0, 1]，<1 表示降权。
        post_modifier = self._post_confidence_modifier(context, direction)
        if post_modifier != 1.0:
            confidence = max(0.0, min(confidence * post_modifier, 1.0))
            trace.append(("post_modifier", round(confidence, 4)))

        # 信号等级：A(四层全有) / B(三层) / C(仅硬门控)
        grade = (
            "A"
            if where_score > 0 and vol_score > 0
            else "B" if where_score > 0 or vol_score > 0 else "C"
        )

        reason = f"{self.name}_{direction}:{why_reason},{when_reason}"
        if where_info:
            reason += f",{where_info}"

        # ADR-013: 不再调 _entry_spec；只输出语义元数据 ENTRY_INTENT + PATTERN_TYPE。
        # 下游 trading/backtest 持有 EntryPolicyRegistry，按 (strategy, tf) 解析
        # policy 后调 derive() 拿 EntrySpecGroup 进入挂单流程。
        pattern_type = self._detect_pattern(context, direction, when_reason)
        exit_spec = self._exit_spec(context, direction)

        entry_intent: Dict[str, Any] = {
            "strategy_name": self.name,
            "timeframe": context.timeframe,
            "direction": direction,
            "pattern_type": pattern_type.value,
            "why_reason": why_reason,
            "when_reason": when_reason,
            "where_info": where_info,
        }

        return self._make_decision(
            direction,
            confidence,
            reason,
            used,
            metadata={
                "why": why_reason,
                "when": when_reason,
                "why_score": round(why_s, 3),
                "when_score": round(when_s, 3),
                "where_score": round(where_s, 3),
                "vol_score": round(vol_s, 3),
                "signal_grade": grade,
                MK.ENTRY_INTENT: entry_intent,
                MK.PATTERN_TYPE: pattern_type.value,
                "exit_spec": exit_spec.to_dict(),
                MK.PROMOTED_INDICATORS: list(self.promoted_indicator_lineage),
                MK.RESEARCH_PROVENANCE: list(self.research_provenance_refs),
            },
            confidence_trace=trace,
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
        confidence_trace: Optional[List[Tuple[str, float]]] = None,
    ) -> SignalDecision:
        capped = min(max(confidence, 0.0), 0.90)
        trace = list(confidence_trace or [])
        if capped != confidence:
            trace.append(("cap_0.90", round(capped, 4)))
        return SignalDecision(
            strategy=self.name,
            symbol="",
            timeframe="",
            direction=direction,
            confidence=capped,
            reason=reason,
            used_indicators=used,
            metadata=metadata or {},
            confidence_trace=trace,
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
    """返回 0~1 的结构位质量分。"""
    bias = ms.get("structure_bias", StructureBias.NEUTRAL)

    if direction == "buy" and bias in BULLISH_BIASES:
        return 0.8, f"struct={bias}"
    if direction == "sell" and bias in BEARISH_BIASES:
        return 0.8, f"struct={bias}"

    fp = ms.get("first_pullback_state", "none")
    if direction == "buy" and str(fp).startswith("bullish_first_pullback"):
        return 1.0, f"first_pb={fp}"
    if direction == "sell" and str(fp).startswith("bearish_first_pullback"):
        return 1.0, f"first_pb={fp}"

    return 0.0, ""
