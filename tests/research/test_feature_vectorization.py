"""特征向量化一致性测试：batch_func 必须与 per-bar func 等价。

任何新特征若提供 batch_func，都必须通过本测试——确保性能优化不改变语义。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from src.research.core.data_matrix import DataMatrix
from src.research.features.engineer import build_default_engineer


def _make_matrix(n: int = 30, include_events: bool = False) -> DataMatrix:
    """构造一个包含所有可能输入的 DataMatrix（通用桩）。"""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bar_times = [
        base.replace(hour=(i * 2) % 24, minute=(i * 5) % 60) for i in range(n)
    ]
    opens = [100.0 + i for i in range(n)]
    highs = [100.0 + i + 2.0 for i in range(n)]
    lows = [100.0 + i - 1.5 for i in range(n)]
    closes = [
        100.0 + i + (1.0 if i % 2 == 0 else -0.5) for i in range(n)
    ]
    # 人工制造部分 doji
    for idx in (5, 12, 20):
        if idx < n:
            opens[idx] = closes[idx]

    # Indicator series 必须含 atr14 供 range_expansion 使用；含 macd/rsi/stoch 供
    # momentum_consensus；含 partial None 供健壮性测试
    ind_series = {
        ("atr14", "atr"): [2.0 if i % 7 != 0 else None for i in range(n)],
        ("macd", "hist"): [
            (0.5 if i % 3 == 0 else -0.5) if i % 11 != 0 else None
            for i in range(n)
        ],
        ("rsi14", "rsi"): [
            (55.0 if i % 2 == 0 else 45.0) for i in range(n)
        ],
        ("stoch_rsi14", "stoch_rsi_k"): [
            (60.0 if i % 3 == 0 else 40.0) for i in range(n)
        ],
    }

    regimes = ["trending" if i < n // 2 else "ranging" for i in range(n)]
    soft_regimes = [
        {"trending": 0.6, "ranging": 0.3, "breakout": 0.1}
        if i % 4 == 0
        else None
        for i in range(n)
    ]

    events = ()
    if include_events:
        events = tuple(
            base.replace(hour=h) for h in (3, 10, 15, 20)
        )

    return DataMatrix(
        symbol="XAUUSD",
        timeframe="M15",
        n_bars=n,
        bar_times=bar_times,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        volumes=[100.0] * n,
        indicators=[{} for _ in range(n)],
        regimes=regimes,
        soft_regimes=soft_regimes,
        forward_returns={},
        indicator_series=ind_series,
        high_impact_event_times=events,
    )


def _nan_eq(a, b, tol: float = 1e-9) -> bool:
    """None/None 相等；float 对比容忍浮点误差。"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if math.isnan(float(a)) and math.isnan(float(b)):
        return True
    return abs(float(a) - float(b)) <= tol


@pytest.mark.parametrize(
    "feature_name",
    [
        # 必须全部覆盖 18 个特征中的 batch-enabled 项
        "close_in_range",
        "body_ratio",
        "upper_wick_ratio",
        "lower_wick_ratio",
        "oc_imbalance",
        "range_expansion",
        "close_to_close_3",
        "momentum_consensus",
        "bars_in_regime",
        "session_phase",
        "london_session",
        "ny_session",
        "day_progress",
        "consecutive_same_color",
        "regime_entropy",
    ],
)
def test_batch_matches_per_bar_no_events(feature_name: str) -> None:
    """batch_func 与 per-bar func 输出必须等价（不含事件依赖）。"""
    eng = build_default_engineer()
    defn = eng.definition(feature_name)
    assert defn is not None
    assert defn.batch_func is not None, f"{feature_name} missing batch_func"

    matrix = _make_matrix()
    per_bar = [defn.func(matrix, i) for i in range(matrix.n_bars)]
    batch = defn.batch_func(matrix)
    assert len(batch) == len(per_bar)
    for i, (a, b) in enumerate(zip(per_bar, batch)):
        assert _nan_eq(a, b), f"{feature_name}[{i}]: per_bar={a}, batch={b}"


@pytest.mark.parametrize(
    "feature_name",
    [
        "bars_to_next_high_impact_event",
        "bars_since_last_high_impact_event",
        "in_news_window",
    ],
)
def test_batch_matches_per_bar_with_events(feature_name: str) -> None:
    """事件类特征单独测（需要 high_impact_event_times）。"""
    eng = build_default_engineer()
    defn = eng.definition(feature_name)
    assert defn is not None
    assert defn.batch_func is not None

    matrix = _make_matrix(include_events=True)
    per_bar = [defn.func(matrix, i) for i in range(matrix.n_bars)]
    batch = defn.batch_func(matrix)
    for i, (a, b) in enumerate(zip(per_bar, batch)):
        assert _nan_eq(a, b), f"{feature_name}[{i}]: per_bar={a}, batch={b}"


def test_all_features_batch_vectorized() -> None:
    """纪律底线：所有注册特征都应提供 batch_func。"""
    eng = build_default_engineer()
    missing = [
        name for name, defn in eng._features.items() if defn.batch_func is None
    ]
    assert not missing, f"Features without batch_func: {missing}"


def test_enrich_uses_batch_path() -> None:
    """enrich() 走 batch 分支的 smoke test：确保集成链路无类型错误。"""
    eng = build_default_engineer()
    matrix = _make_matrix(include_events=True)
    enriched = eng.enrich(matrix)
    # 抽查几个已知会 populated 的特征
    assert enriched.indicator_series.get(("derived_flow", "close_in_range")) is not None
    assert enriched.indicator_series.get(("derived_time", "session_phase")) is not None
