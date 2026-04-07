from typing import Any, Callable


def same_listener_reference(
    left: Callable[..., Any], right: Callable[..., Any]
) -> bool:
    """Return True when two listener callables refer to the same target.

    Handles both regular functions and bound methods.
    """
    if left is right:
        return True
    left_func = getattr(left, "__func__", None)
    right_func = getattr(right, "__func__", None)
    left_self = getattr(left, "__self__", None)
    right_self = getattr(right, "__self__", None)
    return left_func is not None and left_func is right_func and left_self is right_self


def ohlc_key(symbol: str, timeframe: str) -> str:
    return f"{symbol}:{timeframe}"


def timeframe_seconds(timeframe: str) -> int:
    tf_upper = timeframe.upper()
    mapping = {
        "M1": 60,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
        "W1": 604800,
    }
    return mapping.get(tf_upper, 60)


def timeframe_interval(
    timeframe: str, default_interval: float, overrides: dict
) -> float:
    tf_upper = timeframe.upper()
    if tf_upper in overrides:
        return float(overrides[tf_upper])
    return float(default_interval)
