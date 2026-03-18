"""高时间框架（HTF）信号方向缓存（HTFStateCache）。

## 设计动机

MultiTimeframeConfirmStrategy 需要读取更高时间框架（HTF）上的信号方向，
以确认低时间框架（LTF）信号是否与大趋势一致。

原实现通过 metadata["htf_key"] 从 runtime._state_by_target 读取状态，
但 htf_key 永远不会被填充——因为 runtime 在处理每个 snapshot 时不知道
"对应的 HTF key 是什么"。导致策略始终返回 hold（已知 bug）。

HTFStateCache 通过监听 SignalRuntime 的 confirmed 信号事件，
缓存每个 (symbol, timeframe, strategy="consensus") 的最新方向，
并提供一个简洁的 `get_direction(symbol, htf_timeframe)` 接口，
由 MultiTimeframeConfirmStrategy 直接调用，不再依赖 metadata 注入。

## 数据来源

只缓存 strategy="consensus" 的 confirmed 信号（最高可信度），
用于代表该时间框架的整体方向。

## 时间框架映射

| LTF   | HTF（默认） |
|-------|------------|
| M1    | M5         |
| M5    | M15        |
| M15   | H1         |
| H1    | H4         |
| H4    | D1         |
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

# 默认 LTF → HTF 映射（可在构造时覆盖）
_DEFAULT_HTF_MAP: Dict[str, str] = {
    "M1": "M5",
    "M5": "M15",
    "M15": "H1",
    "H1": "H4",
    "H4": "D1",
}


class HTFStateCache:
    """缓存高时间框架 consensus 信号方向。

    参数
    ----
    htf_map:
        LTF → HTF 的映射表。若 None 则使用内置默认映射。
    max_age_seconds:
        超过此时间的缓存条目视为过期，返回 None（避免使用陈旧方向）。
        默认 4 小时（14400 秒）。
    """

    def __init__(
        self,
        *,
        htf_map: Optional[Dict[str, str]] = None,
        max_age_seconds: float = 14400.0,
    ) -> None:
        self._htf_map = htf_map or dict(_DEFAULT_HTF_MAP)
        self._max_age = timedelta(seconds=max(1.0, max_age_seconds))
        # key: (symbol, timeframe) → (direction, updated_at)
        self._cache: Dict[Tuple[str, str], Tuple[str, datetime]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_htf_direction(self, symbol: str, ltf_timeframe: str) -> Optional[str]:
        """返回 symbol 在 LTF 对应 HTF 上的最新方向（buy/sell/hold），
        若无缓存或已过期则返回 None。
        """
        htf = self._htf_map.get(ltf_timeframe)
        if htf is None:
            return None
        key = (symbol, htf)
        with self._lock:
            entry = self._cache.get(key)
        if entry is None:
            return None
        direction, updated_at = entry
        if datetime.now(timezone.utc) - updated_at > self._max_age:
            return None
        return direction

    def on_signal_event(self, event: Any) -> None:
        """SignalRuntime 信号监听器：缓存 consensus confirmed 信号方向。"""
        if event.strategy != "consensus":
            return
        signal_state = str(event.metadata.get("signal_state", ""))
        if not signal_state.startswith("confirmed_"):
            return
        if signal_state == "confirmed_cancelled":
            # 共识取消 → 清除缓存，避免影响后续 MTF 判断
            key = (event.symbol, event.timeframe)
            with self._lock:
                self._cache.pop(key, None)
            return
        # confirmed_buy / confirmed_sell
        direction = signal_state.replace("confirmed_", "")
        key = (event.symbol, event.timeframe)
        with self._lock:
            self._cache[key] = (direction, datetime.now(timezone.utc))

    def attach(self, runtime: Any) -> None:
        """将 HTFStateCache 注册到 SignalRuntime 作为信号监听器。"""
        runtime.add_signal_listener(self.on_signal_event)

    def describe(self) -> Dict[str, Any]:
        """返回当前缓存内容，用于监控端点。"""
        now = datetime.now(timezone.utc)
        with self._lock:
            snapshot = dict(self._cache)
        result = {}
        for (symbol, tf), (direction, updated_at) in snapshot.items():
            age_seconds = (now - updated_at).total_seconds()
            result[f"{symbol}/{tf}"] = {
                "direction": direction,
                "updated_at": updated_at.isoformat(),
                "age_seconds": round(age_seconds, 1),
                "expired": age_seconds > self._max_age.total_seconds(),
            }
        return result
