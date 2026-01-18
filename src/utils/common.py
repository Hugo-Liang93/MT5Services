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
    }
    return mapping.get(tf_upper, 60)


def timeframe_interval(timeframe: str, default_interval: float, overrides: dict) -> float:
    tf_upper = timeframe.upper()
    if tf_upper in overrides:
        return float(overrides[tf_upper])
    return float(default_interval)
