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
                        # HTF stale 的行为是 skip injection（让策略走 fallback 或被 regime 门控兜底），
                        # 不是 error。常见于周末/市场休市时 H4/D1 的 HTF bar 自然过时。
                        # 用 INFO 级别避免污染 errors.log，同时保持可审计性。
                        # Rate-limit: log first 3, then every 200th.
                        if count <= 3 or count % 200 == 0:
                            logger.info(
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


