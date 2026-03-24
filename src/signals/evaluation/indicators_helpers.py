"""指标辅助函数（Indicators Helpers）。

集中处理跨模块共享的指标数据提取逻辑，避免相同代码散落在
signal_executor.py、signal_quality_tracker.py、strategies.py 等多处。

函数均为无副作用纯函数，可安全并发调用。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..models import SignalDecision


def extract_close_price(
    indicators: Dict[str, Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """从指标快照中提取收盘价，可选优先从 metadata 获取。

    优先级：
    1. ``metadata["close_price"]``（由 SignalRuntime 注入，精度最高）
    2. 委托 :func:`get_close` 扫描 indicators payload

    此函数整合了原 ``SignalQualityTracker._get_close_price`` 和
    ``runtime._extract_close_price`` 的逻辑，作为统一入口。
    """
    if metadata is not None:
        raw = metadata.get("close_price")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    return get_close(indicators)


def get_close(indicators: Dict[str, Dict[str, Any]]) -> Optional[float]:
    """从指标快照中提取当前 bar 的收盘价。

    扫描顺序（精确度由高到低）：
    1. 任意 payload 中的 ``close`` 字段（boll20、donchian 等直接附带原始 close）
    2. 任意 payload 中的 ``bb_mid``（Bollinger 中轨 ≈ SMA(close,20)，误差可接受）

    不使用 sma/ema 等滞后均线，它们不适合作为当根 bar 的价格代理。
    """
    bb_mid: Optional[float] = None
    for payload in indicators.values():
        if not isinstance(payload, dict):
            continue
        close = payload.get("close")
        if close is not None:
            try:
                return float(close)
            except (TypeError, ValueError):
                pass
        if bb_mid is None:
            mid = payload.get("bb_mid")
            if mid is not None:
                try:
                    bb_mid = float(mid)
                except (TypeError, ValueError):
                    pass
    return bb_mid


def get_atr(
    indicators: Dict[str, Dict[str, Any]],
    *,
    keys: tuple[str, ...] = ("atr14", "atr", "atr21", "atr7"),
) -> Optional[float]:
    """从指标快照中提取 ATR 值。

    按 ``keys`` 顺序查找，返回第一个有效的正数值。
    各指标 payload 字段名统一为 ``atr``（参见 indicators.json 定义）。

    参数
    ----
    keys:
        ATR 指标名称查找顺序，默认优先 atr14，再尝试通用 atr。
    """
    for key in keys:
        payload = indicators.get(key)
        if not isinstance(payload, dict):
            continue
        raw = payload.get("atr") or payload.get("value")
        if raw is not None:
            try:
                val = float(raw)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass
    return None


def hold_decision(
    strategy: str,
    symbol: str,
    timeframe: str,
    reason: str = "insufficient_indicators",
) -> "SignalDecision":
    """创建一个标准的 hold 决策，用于指标不足时的安全退出。

    避免各策略各自重复 ``return SignalDecision(action="hold", ...)`` 的样板代码。

    参数
    ----
    strategy:
        策略名称（写入 signal_id 标识）。
    symbol:
        交易品种。
    timeframe:
        时间框架。
    reason:
        human-readable 原因，默认 'insufficient_indicators'。
    """
    return SignalDecision(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        action="hold",
        confidence=0.0,
        reason=reason,
        used_indicators=[],
    )
