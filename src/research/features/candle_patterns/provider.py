"""src/research/features/candle_patterns/provider.py

CandlePatternFeatureProvider — K 线形态 binary feature 集。

复用已注册 `candle_pattern` indicator 的 8 个 directional 字段
（hammer / engulfing / doji / pin_bar / inside_bar / three_method /
rejection / consecutive_dir），拆成 13 个 mining feature：

  binary（0/1）— 11 项：
    hammer_bullish      hammer == +1
    hammer_bearish      hammer == -1
    engulfing_bullish   engulfing == +1
    engulfing_bearish   engulfing == -1
    pin_bar_bullish     pin_bar == +1
    pin_bar_bearish     pin_bar == -1
    rejection_bullish   rejection == +1
    rejection_bearish   rejection == -1
    doji                doji == 1
    inside_bar          inside_bar == 1
    three_soldiers      three_method == +1
    three_crows         three_method == -1

  signed（保留正负数）— 1 项：
    consecutive_dir     正=连续阳线数，负=连续阴线数

设计动机：mining 用 IC / 命中率 / barrier predictive power 评估每个 feature
对未来收益的预测力。directional 值（hammer=+1 vs -1）混合时 mean ≈ 0，IC
被消除——必须拆方向。`structured_price_action` 当前用 hammer/pin_bar/engulf
等是手编码，引入这些为 mining feature 后可挖出"哪些形态在 trending vs
ranging 真有 edge"等数据驱动的发现。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.research.features.protocol import (
    FeatureProvider,
    FeatureRole,
    ProviderDataRequirement,
)

_GROUP = "candle_patterns"
_INDICATOR = "candle_pattern"


# (input_field, output_feature, target_value) 的配置表
_DIRECTIONAL_SPLIT: Tuple[Tuple[str, str, float], ...] = (
    ("hammer", "hammer_bullish", 1.0),
    ("hammer", "hammer_bearish", -1.0),
    ("engulfing", "engulfing_bullish", 1.0),
    ("engulfing", "engulfing_bearish", -1.0),
    ("pin_bar", "pin_bar_bullish", 1.0),
    ("pin_bar", "pin_bar_bearish", -1.0),
    ("rejection", "rejection_bullish", 1.0),
    ("rejection", "rejection_bearish", -1.0),
    ("three_method", "three_soldiers", 1.0),
    ("three_method", "three_crows", -1.0),
)

# (input_field, output_feature) — 直接映射（不分方向）
_BINARY_PASSTHROUGH: Tuple[Tuple[str, str], ...] = (
    ("doji", "doji"),
    ("inside_bar", "inside_bar"),
)

# 保留 signed 数值的字段
_SIGNED_PASSTHROUGH: Tuple[Tuple[str, str], ...] = (
    ("consecutive_dir", "consecutive_dir"),
)


def _split_directional(
    series: Optional[List[Optional[float]]],
    target: float,
    n_bars: int,
) -> List[float]:
    """把 directional series 按 target 值拆成 binary：等于 target → 1.0，否则 0.0。

    None / 缺失 / NaN → 0.0（warmup 期）。"""
    if series is None:
        return [0.0] * n_bars
    out: List[float] = []
    for i in range(n_bars):
        v = series[i] if i < len(series) else None
        if v is None:
            out.append(0.0)
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            out.append(0.0)
            continue
        out.append(1.0 if fv == target else 0.0)
    return out


def _passthrough_signed(
    series: Optional[List[Optional[float]]],
    n_bars: int,
) -> List[float]:
    """保留 signed 数值（None → 0.0）。"""
    if series is None:
        return [0.0] * n_bars
    out: List[float] = []
    for i in range(n_bars):
        v = series[i] if i < len(series) else None
        if v is None:
            out.append(0.0)
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


class CandlePatternFeatureProvider:
    """复用 candle_pattern indicator 的 K 线形态 binary feature 计算器。

    满足 FeatureProvider Protocol。本 provider 不做 K 线检测——直接读
    `matrix.indicator_series` 中已计算好的 `candle_pattern.*` 字段，按方向
    拆成 binary 特征。indicator 注册见 `config/indicators.json`。
    """

    @property
    def name(self) -> str:
        return _GROUP

    @property
    def feature_count(self) -> int:
        return (
            len(_DIRECTIONAL_SPLIT)
            + len(_BINARY_PASSTHROUGH)
            + len(_SIGNED_PASSTHROUGH)
        )

    def required_columns(self) -> List[Tuple[str, str]]:
        seen: List[Tuple[str, str]] = []
        for field, _out, _target in _DIRECTIONAL_SPLIT:
            key = (_INDICATOR, field)
            if key not in seen:
                seen.append(key)
        for field, _out in _BINARY_PASSTHROUGH:
            seen.append((_INDICATOR, field))
        for field, _out in _SIGNED_PASSTHROUGH:
            seen.append((_INDICATOR, field))
        return seen

    def required_extra_data(self) -> Optional[ProviderDataRequirement]:
        return None

    def role_mapping(self) -> Dict[str, FeatureRole]:
        roles: Dict[str, FeatureRole] = {}
        # 所有形态信号默认 WHEN（入场触发）
        for _field, out, _target in _DIRECTIONAL_SPLIT:
            roles[out] = FeatureRole.WHEN
        for _field, out in _BINARY_PASSTHROUGH:
            roles[out] = FeatureRole.WHEN
        # consecutive_dir 是趋势确认特征 → WHY
        for _field, out in _SIGNED_PASSTHROUGH:
            roles[out] = FeatureRole.WHY
        return roles

    def compute(
        self,
        matrix: Any,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[Tuple[str, str], List[Optional[float]]]:
        n: int = matrix.n_bars
        if n == 0:
            return self._empty_result()

        result: Dict[Tuple[str, str], List[Optional[float]]] = {}

        for field, out_name, target in _DIRECTIONAL_SPLIT:
            series = matrix.indicator_series.get((_INDICATOR, field))
            result[(_GROUP, out_name)] = _split_directional(series, target, n)

        for field, out_name in _BINARY_PASSTHROUGH:
            series = matrix.indicator_series.get((_INDICATOR, field))
            # doji / inside_bar 已是 0/1，直接 cast 到 float
            result[(_GROUP, out_name)] = _passthrough_signed(series, n)

        for field, out_name in _SIGNED_PASSTHROUGH:
            series = matrix.indicator_series.get((_INDICATOR, field))
            result[(_GROUP, out_name)] = _passthrough_signed(series, n)

        return result

    def _empty_result(self) -> Dict[Tuple[str, str], List[Optional[float]]]:
        keys: List[str] = []
        for _field, out, _target in _DIRECTIONAL_SPLIT:
            keys.append(out)
        for _field, out in _BINARY_PASSTHROUGH:
            keys.append(out)
        for _field, out in _SIGNED_PASSTHROUGH:
            keys.append(out)
        return {(_GROUP, k): [] for k in keys}
