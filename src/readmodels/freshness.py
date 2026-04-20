"""P1.1: 读模型公用 freshness/source_kind 工具（消除 5 份重复）。

提供三个纯函数原语：
- `age_seconds(ts)` — 从 datetime / ISO str 计算距今秒数
- `freshness_state(age, stale_after, max_age)` — 三态判定 fresh/warn/stale/unknown
- `iso_or_none(ts)` — datetime → ISO str 归一化

以及一个 `build_freshness_block()` 组合工具：
按给定阈值组装 `{observed_at, data_updated_at, age_seconds, max_age_seconds,
stale_after_seconds, freshness_state, source_kind, fallback_applied, fallback_reason}`
——与 cockpit/intel/trades_workbench/lab_impact/workbench 各处 freshness 字段契约一致。

所有时间字段统一 ISO8601（P10.6 字段契约）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


__all__ = [
    "FRESHNESS_STATE_FRESH",
    "FRESHNESS_STATE_WARN",
    "FRESHNESS_STATE_STALE",
    "FRESHNESS_STATE_UNKNOWN",
    "age_seconds",
    "freshness_state",
    "iso_or_none",
    "build_freshness_block",
]


FRESHNESS_STATE_FRESH: str = "fresh"
FRESHNESS_STATE_WARN: str = "warn"
FRESHNESS_STATE_STALE: str = "stale"
FRESHNESS_STATE_UNKNOWN: str = "unknown"


def iso_or_none(value: Any) -> Optional[str]:
    """datetime → ISO8601 字符串；已是 str 原样返回；None 返回 None。"""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def age_seconds(value: Any) -> Optional[float]:
    """计算 value（datetime / ISO str）距今秒数。

    无法解析或 None 返回 None；naive datetime 按 UTC 处理。
    """
    if value is None:
        return None
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value)
        elif isinstance(value, datetime):
            dt = value
        else:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except (ValueError, TypeError):
        return None


def freshness_state(
    age: Optional[float],
    *,
    stale_after_seconds: float,
    max_age_seconds: float,
) -> str:
    """三态判定：age<stale_after → fresh；<max_age → warn；≥max_age → stale。

    `stale_after_seconds < max_age_seconds` 应由调用方保证；若 age 为 None 返回 unknown。
    """
    if age is None:
        return FRESHNESS_STATE_UNKNOWN
    if age < stale_after_seconds:
        return FRESHNESS_STATE_FRESH
    if age < max_age_seconds:
        return FRESHNESS_STATE_WARN
    return FRESHNESS_STATE_STALE


def build_freshness_block(
    *,
    observed_at: str,
    data_updated_at: Optional[Any],
    stale_after_seconds: float,
    max_age_seconds: float,
    source_kind: str = "native",
    fallback_applied: bool = False,
    fallback_reason: Optional[str] = None,
) -> dict[str, Any]:
    """组装统一 freshness payload。

    与 cockpit/intel/trades_workbench/workbench 各自原实现字段契约一致，
    统一收口到此函数。调用处只需给出业务阈值 + 数据源时间戳。
    """
    data_updated_iso = iso_or_none(data_updated_at)
    age = age_seconds(data_updated_iso)
    return {
        "observed_at": observed_at,
        "data_updated_at": data_updated_iso,
        "age_seconds": age,
        "stale_after_seconds": stale_after_seconds,
        "max_age_seconds": max_age_seconds,
        "freshness_state": freshness_state(
            age,
            stale_after_seconds=stale_after_seconds,
            max_age_seconds=max_age_seconds,
        ),
        "source_kind": source_kind,
        "fallback_applied": fallback_applied,
        "fallback_reason": fallback_reason,
    }
