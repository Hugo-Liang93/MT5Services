"""高时间框架（HTF）信号方向缓存（HTFStateCache）。

## 设计动机

MultiTimeframeConfirmStrategy 需要读取更高时间框架（HTF）上的信号方向，
以确认低时间框架（LTF）信号是否与大趋势一致。

原实现通过 metadata["htf_key"] 从 runtime._state_by_target 读取状态，
但 htf_key 永远不会被填充——因为 runtime 在处理每个 snapshot 时不知道
"对应的 HTF key 是什么"。导致策略始终返回 hold（已知 bug）。

HTFStateCache 通过监听 SignalRuntime 的 confirmed 信号事件，
缓存每个 (symbol, timeframe) 的最新方向，
并提供一个简洁的 `get_direction(symbol, htf_timeframe)` 接口，
由 MultiTimeframeConfirmStrategy 直接调用，不再依赖 metadata 注入。

## 数据来源

缓存 ``source_strategies`` 指定的策略的 confirmed 信号。
单 consensus 模式下默认 ``{"consensus"}``；多组投票模式下自动
使用各 voting group 名称（如 ``{"trend_vote", "breakout_vote"}``）。

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
from dataclasses import dataclass
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



@dataclass(frozen=True)
class HTFDirectionContext:
    """Enriched HTF direction record — carries strength information."""

    direction: str  # "buy" or "sell"
    confidence: float  # signal confidence at time of caching
    regime: str  # regime type (e.g. "trending", "ranging")
    stable_bars: int  # consecutive bars same direction was held
    updated_at: datetime


class HTFStateCache:
    """缓存高时间框架信号方向。

    参数
    ----
    htf_map:
        LTF → HTF 的映射表。若 None 则使用内置默认映射。
    max_age_seconds:
        超过此时间的缓存条目视为过期，返回 None（避免使用陈旧方向）。
        默认 4 小时（14400 秒）。
    source_strategies:
        从哪些策略名的 confirmed 信号中缓存方向。
        默认 ``{"consensus"}``（单 consensus 模式）；多组投票模式下
        应传入各 voting group 的名称（如 ``{"trend_vote", "breakout_vote"}``），
        否则 HTF 对齐检查将静默失效。
    """

    def __init__(
        self,
        *,
        htf_map: Optional[Dict[str, str]] = None,
        max_age_seconds: float = 14400.0,
        source_strategies: Optional[frozenset[str]] = None,
    ) -> None:
        self._htf_map = htf_map or dict(_DEFAULT_HTF_MAP)
        self._max_age = timedelta(seconds=max(1.0, max_age_seconds))
        self._source_strategies: frozenset[str] = (
            source_strategies if source_strategies is not None
            else frozenset({"consensus"})
        )
        # key: (symbol, timeframe) → HTFDirectionContext
        self._cache: Dict[Tuple[str, str], HTFDirectionContext] = {}
        self._lock = threading.Lock()
        # 命中率统计
        self._hit_count: int = 0
        self._miss_count: int = 0
        self._expired_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_htf_direction(self, symbol: str, ltf_timeframe: str) -> Optional[str]:
        """返回 symbol 在 LTF 对应 HTF 上的最新方向（buy/sell/hold），
        若无缓存或已过期则返回 None。
        """
        ctx = self.get_htf_context(symbol, ltf_timeframe)
        return ctx.direction if ctx is not None else None

    def get_htf_context(self, symbol: str, ltf_timeframe: str) -> Optional[HTFDirectionContext]:
        """返回 HTF 上的完整方向上下文（含 confidence/regime/stable_bars）。"""
        htf = self._htf_map.get(ltf_timeframe)
        if htf is None:
            self._miss_count += 1
            return None
        key = (symbol, htf)
        with self._lock:
            entry = self._cache.get(key)
        if entry is None:
            self._miss_count += 1
            return None
        if datetime.now(timezone.utc) - entry.updated_at > self._max_age:
            self._expired_count += 1
            return None
        self._hit_count += 1
        return entry

    def on_signal_event(self, event: Any) -> None:
        """SignalRuntime 信号监听器：缓存 source_strategies 的 confirmed 信号方向。"""
        if event.strategy not in self._source_strategies:
            return
        signal_state = str(event.metadata.get("signal_state", ""))
        if not signal_state.startswith("confirmed_"):
            return
        if signal_state == "confirmed_cancelled":
            key = (event.symbol, event.timeframe)
            with self._lock:
                self._cache.pop(key, None)
            return
        # confirmed_buy / confirmed_sell
        direction = signal_state.replace("confirmed_", "")
        key = (event.symbol, event.timeframe)
        now = datetime.now(timezone.utc)

        # Extract enriched context from event metadata
        confidence = getattr(event, "confidence", 0.5)
        regime = str(event.metadata.get("_regime", "uncertain"))

        with self._lock:
            prev = self._cache.get(key)
            # Track consecutive bars with same direction
            stable_bars = 1
            if prev is not None and prev.direction == direction:
                stable_bars = prev.stable_bars + 1

            self._cache[key] = HTFDirectionContext(
                direction=direction,
                confidence=confidence,
                regime=regime,
                stable_bars=stable_bars,
                updated_at=now,
            )

    def attach(self, runtime: Any) -> None:
        """将 HTFStateCache 注册到 SignalRuntime 作为信号监听器。"""
        runtime.add_signal_listener(self.on_signal_event)

    def describe(self) -> Dict[str, Any]:
        """返回当前缓存内容和命中率统计，用于监控端点。"""
        now = datetime.now(timezone.utc)
        with self._lock:
            snapshot = dict(self._cache)
        entries = {}
        for (symbol, tf), ctx in snapshot.items():
            age_seconds = (now - ctx.updated_at).total_seconds()
            entries[f"{symbol}/{tf}"] = {
                "direction": ctx.direction,
                "confidence": round(ctx.confidence, 4),
                "regime": ctx.regime,
                "stable_bars": ctx.stable_bars,
                "updated_at": ctx.updated_at.isoformat(),
                "age_seconds": round(age_seconds, 1),
                "expired": age_seconds > self._max_age.total_seconds(),
            }
        total = self._hit_count + self._miss_count + self._expired_count
        return {
            "entries": entries,
            "stats": {
                "hit": self._hit_count,
                "miss": self._miss_count,
                "expired": self._expired_count,
                "total": total,
                "hit_rate": round(self._hit_count / total, 4) if total > 0 else None,
            },
        }
