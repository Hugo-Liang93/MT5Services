"""HTF indicator resolution and alignment computation.

Extracted from SignalRuntime to isolate HTF-related logic into a
reusable module. All functions are pure or depend only on injected
callbacks — no thread/queue state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from src.utils.common import timeframe_seconds

logger = logging.getLogger(__name__)


def parse_htf_config(
    raw: Dict[str, str],
) -> dict[str, dict[str, list[str]]]:
    """解析 INI ``[strategy_htf]`` 为按策略分组的 {strategy: {tf: [indicators]}}。

    INI 格式: ``strategy.indicator = TF``
    例: ``supertrend.adx14 = H1`` → {"supertrend": {"H1": ["adx14"]}}
    """
    result: dict[str, dict[str, list[str]]] = {}
    for compound_key, tf_value in raw.items():
        parts = compound_key.split(".", 1)
        if len(parts) != 2:
            continue
        strategy_name, indicator_name = parts[0].strip(), parts[1].strip()
        tf = tf_value.strip().upper()
        if not strategy_name or not indicator_name or not tf:
            continue
        result.setdefault(strategy_name, {}).setdefault(tf, []).append(indicator_name)
    return result


def resolve_htf_indicators(
    symbol: str,
    current_tf: str,
    htf_spec: dict[str, list[str]],
    configured_timeframes: frozenset[str],
    get_indicator_fn: Callable[[str, str, str], Optional[Dict[str, Any]]],
    stale_counter: list[int],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """按 INI 配置按需查询 HTF 指标，返回 {tf: {ind: {field: val}}}。

    Staleness check: 若 HTF 指标的 bar_time 距当前超过 2x HTF 周期，
    视为陈旧数据，跳过注入并记 warning。

    Parameters
    ----------
    stale_counter:
        Single-element list used as mutable counter for stale warnings.
        Caller passes ``[0]`` or its own counter to track across calls.
    """
    now_utc = datetime.now(timezone.utc)
    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for tf, indicator_names in htf_spec.items():
        if tf == current_tf.upper():
            continue
        if tf not in configured_timeframes:
            continue
        max_age = timedelta(seconds=timeframe_seconds(tf) * 2)
        tf_indicators: Dict[str, Dict[str, Any]] = {}
        for ind_name in indicator_names:
            ind_data = get_indicator_fn(symbol, tf, ind_name)
            if ind_data is None:
                continue
            bar_time_str = ind_data.get("_bar_time")
            if bar_time_str:
                try:
                    bar_time_val = datetime.fromisoformat(bar_time_str)
                    if bar_time_val.tzinfo is None:
                        bar_time_val = bar_time_val.replace(tzinfo=timezone.utc)
                    age = now_utc - bar_time_val
                    if age > max_age:
                        stale_counter[0] += 1
                        count = stale_counter[0]
                        # Rate-limit: log first 5, then every 50th
                        if count <= 5 or count % 50 == 0:
                            logger.warning(
                                "HTF indicator %s/%s/%s stale: bar_time=%s age=%s > max=%s, skipping (total=%d)",
                                symbol, tf, ind_name, bar_time_str, age, max_age,
                                count,
                            )
                        continue
                except (ValueError, TypeError):
                    pass  # parse failure: inject anyway
            tf_indicators[ind_name] = ind_data
        if tf_indicators:
            result[tf] = tf_indicators
    return result


def compute_htf_alignment(
    symbol: str,
    timeframe: str,
    action: str,
    scope: str,
    *,
    htf_context_fn: Optional[Callable[[str, str], Any]] = None,
    htf_direction_fn: Optional[Callable[[str, str], Optional[str]]] = None,
    alignment_boost: float = 1.10,
    conflict_penalty: float = 0.70,
    strength_coeff: float = 0.30,
    stability_per_bar: float = 0.03,
    stability_cap: float = 1.15,
    intrabar_ratio: float = 0.50,
) -> tuple[Optional[float], Optional[str]]:
    """Compute HTF alignment multiplier with strength weighting.

    Returns ``(None, None)`` when no HTF data is available.
    Uses enriched context (confidence, regime, stable_bars) when
    ``htf_context_fn`` is provided; otherwise falls back to the
    simple direction-only ``htf_direction_fn``.
    """
    # Try enriched context first
    if htf_context_fn is not None:
        ctx = htf_context_fn(symbol, timeframe)
        if ctx is not None:
            aligned = ctx.direction == action
            base = alignment_boost if aligned else conflict_penalty

            # Strength weighting: high-confidence HTF signal amplifies effect
            strength = 1.0 + (ctx.confidence - 0.5) * strength_coeff
            # Stability weighting: direction held for more bars amplifies effect
            stability = min(
                1.0 + (ctx.stable_bars - 1) * stability_per_bar,
                stability_cap,
            )

            if aligned:
                # D1 fix: aligned 场景下 stability 放大 boost，但保证 >= 1.0
                # （低置信度 HTF 不应让对齐变成压制）
                raw_mul = max(1.0, base * strength * stability)
            else:
                # D2 fix: conflict 场景下 stability 越高惩罚越重
                # stability=1.0 → factor=1.0（不变）, stability=1.15 → factor=0.85（加重）
                raw_mul = base * strength * (2.0 - stability)

            # Intrabar: reduced-strength modification (signal not yet confirmed)
            if scope == "intrabar":
                raw_mul = 1.0 + (raw_mul - 1.0) * intrabar_ratio

            return raw_mul, ctx.direction

    # Fallback: simple direction-only
    if htf_direction_fn is not None:
        htf_dir = htf_direction_fn(symbol, timeframe)
        if htf_dir is not None:
            base = alignment_boost if htf_dir == action else conflict_penalty
            if scope == "intrabar":
                base = 1.0 + (base - 1.0) * intrabar_ratio
            return base, htf_dir

    return None, None
