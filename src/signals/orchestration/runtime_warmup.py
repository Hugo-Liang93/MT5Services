"""SignalRuntime warmup 屏障逻辑。

从 runtime.py 的 _on_snapshot() 提取：
- backfilling 阶段屏蔽
- bar staleness 校验（首根 confirmed bar）
- 必需指标完整性检查
- intrabar 放行前置条件（需先看到 confirmed bar）

所有函数为无副作用的纯判断函数（返回是否放行），
副作用（计数器递增、集合更新、日志）由调用方根据返回值决定。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from src.utils.common import timeframe_seconds

if TYPE_CHECKING:
    from .runtime import SignalRuntime

logger = logging.getLogger(__name__)


def check_warmup_barrier(
    runtime: "SignalRuntime",
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    indicators: dict[str, dict[str, float]],
    scope: str,
) -> bool:
    """检查 warmup 屏障，返回 True 表示放行，False 表示应跳过。

    副作用：更新 runtime 上的 warmup 计数器和首根 bar 跟踪集合。
    """
    warmup_ready = (
        runtime._warmup_ready_fn() if runtime._warmup_ready_fn is not None else True
    )
    with runtime._warmup_lock:
        if not warmup_ready:
            runtime._warmup_skipped += 1
            if runtime._warmup_skipped <= 5 or runtime._warmup_skipped % 200 == 0:
                logger.info(
                    "Warmup barrier (backfilling): %s/%s scope=%s bar_time=%s (total_skipped=%d)",
                    symbol,
                    timeframe,
                    scope,
                    bar_time.isoformat(),
                    runtime._warmup_skipped,
                )
            return False

        if scope == "confirmed":
            if not _check_confirmed_warmup(
                runtime, symbol, timeframe, bar_time, indicators
            ):
                return False

        if scope == "intrabar" and runtime._warmup_ready_fn is not None:
            if not _check_intrabar_warmup(
                runtime, symbol, timeframe, indicators
            ):
                return False

    return True


def _check_confirmed_warmup(
    runtime: "SignalRuntime",
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    indicators: dict[str, dict[str, float]],
) -> bool:
    """confirmed scope 的 warmup 检查：staleness + 必需指标。"""
    st_key = (symbol, timeframe)
    if st_key not in runtime._first_realtime_bar_seen:
        if runtime._warmup_ready_fn is not None:
            _bt = (
                bar_time
                if bar_time.tzinfo is not None
                else bar_time.replace(tzinfo=timezone.utc)
            )
            tf_secs = timeframe_seconds(timeframe)
            staleness = (
                datetime.now(timezone.utc) - _bt.astimezone(timezone.utc)
            ).total_seconds()
            max_age = tf_secs * 2 + 30
            if staleness > max_age:
                runtime._warmup_skipped += 1
                if runtime._warmup_skipped <= 10 or runtime._warmup_skipped % 100 == 0:
                    logger.info(
                        "Warmup skip (stale bar_time): %s/%s bar_time=%s staleness=%.0fs max_age=%.0fs (total_skipped=%d)",
                        symbol,
                        timeframe,
                        bar_time.isoformat(),
                        staleness,
                        max_age,
                        runtime._warmup_skipped,
                    )
                return False
        runtime._first_realtime_bar_seen.add(st_key)
        logger.info(
            "Warmup lifted: first realtime bar for %s/%s at %s",
            symbol,
            timeframe,
            bar_time.isoformat(),
        )

    required = runtime.policy.get_warmup_required_indicators()
    if required and any(ind not in indicators for ind in required):
        runtime._warmup_skipped += 1
        if runtime._warmup_skipped <= 10 or runtime._warmup_skipped % 100 == 0:
            missing = [ind for ind in required if ind not in indicators]
            logger.info(
                "Warmup skip (indicators missing: %s): %s/%s (total_skipped=%d)",
                missing,
                symbol,
                timeframe,
                runtime._warmup_skipped,
            )
        return False

    return True


def _check_intrabar_warmup(
    runtime: "SignalRuntime",
    symbol: str,
    timeframe: str,
    indicators: dict[str, dict[str, float]],
) -> bool:
    """intrabar scope 的 warmup 检查：需先看到 confirmed bar。"""
    st_key = (symbol, timeframe)
    if st_key not in runtime._first_realtime_bar_seen:
        return False
    if st_key not in runtime._first_intrabar_snapshot_seen:
        if not runtime._all_intrabar_strategies_satisfied(indicators):
            return False
        runtime._first_intrabar_snapshot_seen.add(st_key)
        logger.info(
            "Intrabar ready: first complete snapshot for %s/%s (%d indicators)",
            symbol,
            timeframe,
            len(indicators),
        )
    return True
