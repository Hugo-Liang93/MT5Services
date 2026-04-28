"""src/research/features/candle_patterns/__init__.py

K 线形态 Provider 包。把已注册的 `candle_pattern` indicator 的 directional
输出（hammer / engulfing / pin_bar / rejection / three_method 各 1/-1/0）
拆成 binary mining feature，让 mining 体系（IC / barrier / rule）能为
每个形态独立评估 edge。
"""

from __future__ import annotations

from src.research.features.candle_patterns.provider import CandlePatternFeatureProvider

__all__ = ["CandlePatternFeatureProvider"]
